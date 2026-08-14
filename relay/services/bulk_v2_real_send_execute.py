"""bulk-v2 real-send authorization + execution boundary (PR C design
round 8; PR C2 design rounds 9-12: atomic job creation + crash-safe
claim).

`authorize_and_execute_real_send` is the ONE sanctioned internal entry
point to start a V2 real send. It centralizes the fourteen ordered checks
(design.md §8.2) that used to live entirely inside
`relay/management/commands/bulk_v2_real_send.py`: the kill switch,
BulkSend/engine/import-status validation, the existing-job defense-in-
depth check, the read-only ledger, the ambiguous/stale-sending/in-flight
aborts, the nothing-eligible clean no-op, the full `evaluate_real_send`
authorization gate, the dry-run short-circuit, and finally durable
BackgroundJob creation + a crash-recoverable claim + dispatch.

Check 14 (PR C2, design round 9-11): creating the BackgroundJob and
deciding "is one already active" both happen under a `select_for_update`
lock on the BulkSend row itself -- the single mechanism that closes the
Check-6/Check-14 TOCTOU (two concurrent authorize calls for the SAME
BulkSend can never both create a job). The job is committed durably
`queued` BEFORE any claim is attempted, and the lock is released the
instant that transaction commits -- `execute_specific_queued_job`
(relay/services/jobs.py) then claims and runs it OUTSIDE any lock, so no
Doppler I/O ever happens while a row is locked. If the process dies
between that commit and the claim, the job stays `queued` and is
recovered by the already-running continuous worker
(`process_background_jobs --loop`) without any new machinery. The
remaining gap -- between the claim's own commit and the start of
`dispatch_background_job` -- is structurally unavoidable without lease/
fencing (design round 11's proof) and is deliberately deferred, not
built around: the existing `BulkSendRecipient` state machine (Check 9's
stale_sending detection) is what prevents an unsafe second POST, not the
job's own state.

`RealSendOutcome.result` distinguishes `"executed_here"` (this call
claimed and ran the job) from `"delegated"` (this call created the job
but another caller -- typically the continuous worker -- claimed it
first; the job WILL be/was processed, just not by this call) from
`"refused"` (no job exists). `executed` (bool) is preserved for the
command's existing CommandError-vs-success branching and is True for
both `executed_here` and `delegated`.

The management command is now a thin wrapper: parse arguments, call this
function, print the exact same messages it always has (plus one new,
additive branch for `delegated`), translate the returned `RealSendOutcome`
into the same `CommandError`/exit-code convention it always used. No
business logic lives in the command.

No scheduling here: this function does not decide WHEN to run, only
WHETHER and HOW. A future scheduler calls this same function directly,
in-process.

Boundary: zero references to scheduling, process_bulk_scheduled, V1,
bulk_processing.py, views.py, RemoteQuotaState, Limit Status,
X-Rate-Limit headers, or quota settings. The quota guard (PR B) lives
entirely inside `run_claimed_job` -> `dispatch_background_job` ->
`process_bulk_id_v2`, unaffected by this module -- this module never
reads `DOPPLER_QUOTA_*` and never imports `bulk_quota`.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from relay.models import BackgroundJob, BulkSend
from relay.services.bulk_v2_real_send import evaluate_real_send
from relay.services.bulk_v2_send_state import (
    STALE_SENDING_AFTER,
    LedgerRow,
    describe_send_ledger,
)
from relay.services.jobs import execute_specific_queued_job

logger = logging.getLogger(__name__)

# design round 12 (PR C2): observability-only threshold for a
# TYPE_BULK_SEND_V2_REAL BackgroundJob stuck in `running` -- never used to
# auto-recover/reset/reclaim anything, only to emit a distinct, more
# alarming log line than the ordinary "already present" refusal. Not a
# new Django setting (mirrors STALE_SENDING_AFTER's own precedent:
# derived, not configured). Deliberately larger than STALE_SENDING_AFTER
# (a per-recipient HTTP-timeout-derived threshold): a job-level stall
# must look wrong at a coarser level than "one recipient's request is
# slow" -- that narrower case is already Check 9's job via
# STALE_SENDING_AFTER. Today MAX_ROWS is hard-gated to exactly 1
# (evaluate_real_send), so a legitimate job's total runtime is bounded by
# roughly one recipient's worth of work -- doubling the per-recipient
# threshold gives headroom above that without inventing an unrelated
# number. MUST be revisited if MAX_ROWS is ever allowed above 1 (a
# legitimately busy multi-recipient job could then run longer than this
# without being stuck).
#
# BACKGROUND_JOB_STALE_THRESHOLD_REVIEW_BEFORE_SCALE: named debt marker
# (design round 13) -- this derivation is appropriate for today's
# MAX_ROWS=1 state only. No new setting, no behavior change here; this
# comment is the deliberate, conceptual registration of that debt.
BACKGROUND_JOB_STALE_RUNNING_AFTER = STALE_SENDING_AFTER * 2


def _request_fingerprint(client_request_id: str) -> str:
    # Reuses the existing sha256(client_request_id)[:12] convention
    # (relay/api.py:447-455), unchanged from the original command.
    return hashlib.sha256(
        str(client_request_id or "").encode("utf-8")
    ).hexdigest()[:12]


REAL_SEND_RESULT_REFUSED = "refused"
REAL_SEND_RESULT_EXECUTED_HERE = "executed_here"
REAL_SEND_RESULT_DELEGATED = "delegated"


@dataclass(frozen=True)
class RealSendOutcome:
    """Structured result of `authorize_and_execute_real_send`.

    `executed` is True whenever a BackgroundJob durably exists as a
    consequence of THIS call's authorization succeeding -- true for both
    `result="executed_here"` and `result="delegated"` (design round 12).
    Every refusal (including returncode=0 no-ops like "nothing to send")
    has `executed=False`. Preserved for the command's existing
    CommandError-vs-success branching -- unchanged meaning from PR C for
    every scenario that could occur before PR C2 (`delegated` did not
    exist until this round).

    `result` (design round 12) is the precise, three-way signal a
    careful caller (a future scheduler, in particular) should branch on:
      - "refused": no job exists because of this call.
      - "executed_here": this call created AND ran the job to a
        terminal state (`done`/`error`) -- `job_state`/`job_message`
        reflect that terminal outcome.
      - "delegated": this call created the job, but another caller
        (typically the continuous worker, `process_background_jobs
        --loop`) claimed it first. The job WILL be, or already is being,
        processed -- just not by this call. `job_state` is a best-effort,
        unlocked, purely informational read of the job's current state
        at the moment of delegation, never used for any decision.

    `command_error_message` is the exact string the command must pass to
    `CommandError` when `executed` is False -- computed here, not by the
    command, so the command never needs its own formatting convention
    (the ordinary `f"{code}: {message}"` shape for every refusal, and the
    one deliberate exception: the defensive "claim failed" branch, whose
    pre-PR-C message never had that prefix and must not gain one now).
    """

    executed: bool
    code: str
    message: str
    returncode: int
    bulk_send_id: int
    eligible_rows: int
    max_rows: int
    dry_run: bool
    result: str = REAL_SEND_RESULT_REFUSED
    authorization_code: str = ""
    blocked_ambiguous_rows: tuple[LedgerRow, ...] = ()
    job_id: int | None = None
    job_state: str | None = None
    job_message: str | None = None
    command_error_message: str = ""


def _log_decision(
    *, decision: str, code: str, bulk_send_id: int, request_fingerprint: str,
    eligible_rows: int, max_rows: int,
) -> None:
    logger.info(
        "bulk_v2_real_send_decision decision=%s code=%s bulk_send_id=%s "
        "request=%s eligible_rows=%s max_rows=%s at=%s",
        decision, code, bulk_send_id, request_fingerprint, eligible_rows,
        max_rows, timezone.now().isoformat(),
    )


def authorize_and_execute_real_send(
    bulk_send_id: int, *, dry_run: bool = False
) -> RealSendOutcome:
    max_rows = settings.BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS
    fingerprint = ""

    def _refused(
        *, code: str, message: str, returncode: int, eligible_rows: int = 0,
        authorization_code: str = "", blocked_ambiguous_rows: tuple = (),
    ) -> RealSendOutcome:
        _log_decision(
            decision="refused", code=code, bulk_send_id=bulk_send_id,
            request_fingerprint=fingerprint, eligible_rows=eligible_rows,
            max_rows=max_rows,
        )
        return RealSendOutcome(
            executed=False, code=code, message=message, returncode=returncode,
            bulk_send_id=bulk_send_id, eligible_rows=eligible_rows,
            max_rows=max_rows, dry_run=dry_run,
            authorization_code=authorization_code,
            blocked_ambiguous_rows=blocked_ambiguous_rows,
            command_error_message=f"{code}: {message}",
        )

    # Check 2: kill switch. Zero DB queries executed before this point or
    # by this check itself (design §8.2).
    if settings.BULK_PROCESSING_V2_REAL_SEND_ENABLED is not True:
        return _refused(
            code="real_send_disabled",
            message="El envio real V2 no esta habilitado.",
            returncode=5,
        )

    # Check 3: BulkSend exists.
    bulk = BulkSend.objects.filter(pk=bulk_send_id).first()
    if bulk is None:
        return _refused(
            code="real_send_bulk_not_found",
            message=f"No existe BulkSend {bulk_send_id}.",
            returncode=2,
        )

    fingerprint = _request_fingerprint(bulk.client_request_id)

    # Check 4: engine_version == v2.
    if bulk.engine_version != BulkSend.ENGINE_V2:
        return _refused(
            code="real_send_engine_not_v2",
            message="El BulkSend no usa el motor v2.",
            returncode=2,
        )

    # Check 5: import_status ready or ready_with_errors.
    if bulk.import_status not in {
        BulkSend.IMPORT_READY, BulkSend.IMPORT_READY_WITH_ERRORS,
    }:
        return _refused(
            code="real_send_import_not_ready",
            message="La importacion V2 no esta lista.",
            returncode=2,
        )

    # Check 6: no queued/running job of this type already exists for this
    # bulk. This is a cheap, UNLOCKED fast path only -- NOT the safety
    # boundary against duplicate jobs (that is Check 14's locked
    # re-verification, design round 9). It still exists here because it
    # avoids doing the ledger read + authorization gate below when a job
    # is obviously already active.
    existing_job = (
        BackgroundJob.objects.filter(
            bulk_id=bulk.pk,
            job_type=BackgroundJob.TYPE_BULK_SEND_V2_REAL,
            state__in=[BackgroundJob.STATE_QUEUED, BackgroundJob.STATE_RUNNING],
        )
        .only("pk", "state", "started_at")
        .first()
    )
    if existing_job is not None:
        # design round 12 (PR C2): observability-only. Never resets,
        # reclaims, or otherwise mutates the job or any recipient --
        # purely a more alarming log line than the ordinary refusal
        # below, for the case where the existing job has been `running`
        # long enough that it is very unlikely to still be healthy.
        if (
            existing_job.state == BackgroundJob.STATE_RUNNING
            and existing_job.started_at is not None
        ):
            age = timezone.now() - existing_job.started_at
            if age > BACKGROUND_JOB_STALE_RUNNING_AFTER:
                logger.warning(
                    "bulk_v2_real_send_job_stale_detected job_id=%s "
                    "bulk_send_id=%s age_seconds=%s",
                    existing_job.pk, bulk.pk, int(age.total_seconds()),
                )
        return _refused(
            code="real_send_job_already_present",
            message="Ya existe un job bulk_send_v2_real en curso para este BulkSend.",
            returncode=4,
        )

    # Check 7: read-only ledger.
    ledger = describe_send_ledger(bulk.pk)
    eligible_row_count = len(ledger.eligible)

    # Check 8: ambiguous rows abort everything, exit 3, touch nothing.
    if ledger.blocked_ambiguous:
        return _refused(
            code="real_send_ambiguous_present",
            message="Existen filas ambiguous; requiere resolucion humana. Canary abortado.",
            returncode=3, eligible_rows=eligible_row_count,
            blocked_ambiguous_rows=ledger.blocked_ambiguous,
        )

    # Check 9: stale sending rows abort, exit 4, touch nothing.
    if ledger.stale_sending:
        return _refused(
            code="real_send_stale_sending_present",
            message="Existen filas sending envejecidas; requiere inspeccion humana.",
            returncode=4, eligible_rows=eligible_row_count,
        )

    # Check 10: in-flight rows abort, exit 4, touch nothing.
    if ledger.in_flight:
        return _refused(
            code="real_send_in_flight_present",
            message="Existe una fila sending en curso; otro worker puede tenerla.",
            returncode=4, eligible_rows=eligible_row_count,
        )

    # Check 11: nothing eligible -> clean no-op, exit 0 (success).
    if not ledger.eligible:
        return _refused(
            code="real_send_nothing_to_send",
            message="No hay filas elegibles para el envio real V2 (no-op limpio).",
            returncode=0, eligible_rows=eligible_row_count,
        )

    # Check 12: full authorization gate.
    #
    # NOTE (discovered gap, flagged explicitly, unchanged from the
    # original command -- design.md does not specify this plumbing):
    # evaluate_real_send requires a user_id, but BulkSend stores no
    # "owner"/created-by field of its own -- its only User FK is
    # `scheduled_by`, which V2 real-send scope explicitly forbids
    # populating. The only available signal is `bulk.scheduled_by_id`,
    # which will be None for essentially every real V2 bulk. This means
    # the gate fails closed (real_send_user_not_allowed) for any BulkSend
    # that never went through V1 scheduling. It does not weaken safety:
    # the gate simply refuses more often than the allowlist alone would
    # suggest.
    user_id = bulk.scheduled_by_id
    recipient_domains = tuple(row.domain for row in ledger.eligible)
    decision = evaluate_real_send(
        real_send_enabled=settings.BULK_PROCESSING_V2_REAL_SEND_ENABLED,
        user_allowlist=settings.BULK_PROCESSING_V2_REAL_SEND_USER_IDS,
        request_allowlist=settings.BULK_PROCESSING_V2_REAL_SEND_REQUEST_IDS,
        template_allowlist=settings.BULK_PROCESSING_V2_REAL_SEND_TEMPLATE_IDS,
        recipient_domain_allowlist=settings.BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS,
        max_rows=max_rows,
        user_id=user_id,
        client_request_id=bulk.client_request_id,
        template_id=bulk.template_id,
        recipient_domains=recipient_domains,
        eligible_row_count=eligible_row_count,
    )
    if not decision.allowed:
        return _refused(
            code=decision.code, message=decision.message, returncode=5,
            eligible_rows=eligible_row_count,
        )

    _log_decision(
        decision="allowed", code=decision.code, bulk_send_id=bulk_send_id,
        request_fingerprint=fingerprint, eligible_rows=eligible_row_count,
        max_rows=max_rows,
    )

    # Check 13: --dry-run stops here, exit 0, nothing claimed.
    if dry_run:
        return _refused(
            code="real_send_dry_run",
            message="Dry-run: autorizado, nada ejecutado.",
            returncode=0, eligible_rows=eligible_row_count,
            authorization_code=decision.code,
        )

    # Check 14a (design round 9-12, PR C2): lock the BulkSend row itself
    # for the remainder of this decision -- the single mechanism that
    # closes the Check-6/Check-14 TOCTOU window. Re-verifies job
    # existence UNDER the lock (THIS is the actual job-level safety
    # boundary now -- Check 6 above is only the cheap early exit), then
    # creates the BackgroundJob DURABLY QUEUED in the same short
    # transaction. Deliberately does NOT claim/transition it to running
    # here: the lock must release before any claim is attempted, so a
    # crash between this commit and the claim below leaves the job
    # `queued` -- recoverable by the already-running continuous worker
    # (`process_background_jobs --loop`), not orphaned in `running`
    # (design round 11's crash-window analysis).
    with transaction.atomic():
        locked_bulk = (
            BulkSend.objects.select_for_update().filter(pk=bulk.pk).first()
        )
        if locked_bulk is None:  # pragma: no cover - defensive, bulk deleted mid-flight
            return _refused(
                code="real_send_bulk_not_found",
                message=f"No existe BulkSend {bulk_send_id}.",
                returncode=2, eligible_rows=eligible_row_count,
            )

        existing_job_locked = BackgroundJob.objects.filter(
            bulk_id=locked_bulk.pk,
            job_type=BackgroundJob.TYPE_BULK_SEND_V2_REAL,
            state__in=[BackgroundJob.STATE_QUEUED, BackgroundJob.STATE_RUNNING],
        ).exists()
        if existing_job_locked:
            return _refused(
                code="real_send_job_already_present",
                message="Ya existe un job bulk_send_v2_real en curso para este BulkSend.",
                returncode=4, eligible_rows=eligible_row_count,
            )

        job = BackgroundJob.objects.create(
            job_type=BackgroundJob.TYPE_BULK_SEND_V2_REAL,
            bulk=locked_bulk, triggered_by=None, state=BackgroundJob.STATE_QUEUED,
        )
    # BulkSend lock released here (transaction committed). Job durable,
    # visible to the continuous worker and to any other caller. No I/O
    # of any kind has occurred yet.

    # Check 14b: claim THIS specific job and run it synchronously, via
    # the one shared primitive (relay/services/jobs.py) also usable by a
    # future scheduler. If another caller (typically the continuous
    # worker) claims it first, this call delegates cleanly -- it never
    # re-creates a job, never retries the claim, and never dispatches
    # anything itself.
    claimed = execute_specific_queued_job(job.pk)
    if claimed is None:
        current_state = (
            BackgroundJob.objects.filter(pk=job.pk).values_list("state", flat=True).first()
        )
        return RealSendOutcome(
            executed=True, result=REAL_SEND_RESULT_DELEGATED,
            code=decision.code, message="", returncode=0,
            bulk_send_id=bulk_send_id, eligible_rows=eligible_row_count,
            max_rows=max_rows, dry_run=False, authorization_code=decision.code,
            job_id=job.pk, job_state=current_state, job_message=None,
        )

    return RealSendOutcome(
        executed=True, result=REAL_SEND_RESULT_EXECUTED_HERE,
        code=decision.code, message="", returncode=0,
        bulk_send_id=bulk_send_id, eligible_rows=eligible_row_count,
        max_rows=max_rows, dry_run=False, authorization_code=decision.code,
        job_id=claimed.pk, job_state=claimed.state, job_message=claimed.message,
    )
