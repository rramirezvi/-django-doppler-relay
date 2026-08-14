"""bulk-v2 real-send authorization + execution boundary (PR C, design
round 8).

`authorize_and_execute_real_send` is the ONE sanctioned internal entry
point to start a V2 real send. It centralizes the fourteen ordered checks
(design.md §8.2) that used to live entirely inside
`relay/management/commands/bulk_v2_real_send.py`: the kill switch,
BulkSend/engine/import-status validation, the existing-job defense-in-
depth check, the read-only ledger, the ambiguous/stale-sending/in-flight
aborts, the nothing-eligible clean no-op, the full `evaluate_real_send`
authorization gate, the dry-run short-circuit, and finally BackgroundJob
creation + the LOCKED claim path + dispatch via `run_claimed_job`. First
failure wins, same order, same codes, same returncodes as before -- only
relocated, not changed.

The management command is now a thin wrapper: parse arguments, call this
function, print the exact same messages it always has, translate the
returned `RealSendOutcome` into the same `CommandError`/exit-code
convention it always used. No business logic lives in the command
anymore.

No scheduling here: this function does not decide WHEN to run, only
WHETHER and HOW, exactly like the command it was extracted from. A future
scheduler calls this same function directly, in-process -- it does not
gain any capability the management command didn't already have, and the
management command gains none it didn't already have either.

Boundary (design round 8): zero references to scheduling,
process_bulk_scheduled, V1, bulk_processing.py, views.py,
RemoteQuotaState, Limit Status, X-Rate-Limit headers, or quota settings.
The quota guard (PR B) lives entirely inside `run_claimed_job` ->
`dispatch_background_job` -> `process_bulk_id_v2`, called here exactly as
the command always called it -- this module never reads
`DOPPLER_QUOTA_*` and never imports `bulk_quota`.
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
from relay.services.bulk_v2_send_state import LedgerRow, describe_send_ledger
from relay.services.jobs import run_claimed_job

logger = logging.getLogger(__name__)


def _request_fingerprint(client_request_id: str) -> str:
    # Reuses the existing sha256(client_request_id)[:12] convention
    # (relay/api.py:447-455), unchanged from the original command.
    return hashlib.sha256(
        str(client_request_id or "").encode("utf-8")
    ).hexdigest()[:12]


@dataclass(frozen=True)
class RealSendOutcome:
    """Structured result of `authorize_and_execute_real_send`.

    `executed` is True ONLY when check 14 ran (BackgroundJob created,
    claimed, and dispatched via `run_claimed_job`) -- dry-run and every
    refusal (including returncode=0 no-ops like "nothing to send") have
    `executed=False`, mirroring the original command's uniform
    CommandError-for-everything-except-final-success CLI convention.

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
    # bulk (defence in depth; NOT the safety boundary -- design §7,
    # jobs.py's comment, and PR2b-T28).
    existing_job = BackgroundJob.objects.filter(
        bulk_id=bulk.pk,
        job_type=BackgroundJob.TYPE_BULK_SEND_V2_REAL,
        state__in=[BackgroundJob.STATE_QUEUED, BackgroundJob.STATE_RUNNING],
    ).exists()
    if existing_job:
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

    # Check 14: execute. Create the BackgroundJob, claim it through the
    # LOCKED claim path (never run_background_job's bypass, design §7),
    # then dispatch via run_claimed_job.
    job = BackgroundJob.objects.create(
        job_type=BackgroundJob.TYPE_BULK_SEND_V2_REAL,
        bulk=bulk, triggered_by=None, state=BackgroundJob.STATE_QUEUED,
    )
    with transaction.atomic():
        claimed = (
            BackgroundJob.objects
            .select_for_update(skip_locked=True)
            .filter(pk=job.pk, state=BackgroundJob.STATE_QUEUED)
            .first()
        )
        if claimed is None:
            # Defensive: unreachable in a single-shell invocation (the job
            # was just created with a fresh pk in STATE_QUEUED immediately
            # above, in the same process). `command_error_message` is the
            # exact pre-PR-C literal -- deliberately WITHOUT the
            # `f"{code}: {message}"` prefix every other refusal uses, to
            # keep the command's observable output byte-for-byte
            # unchanged from before this extraction.
            return RealSendOutcome(
                executed=False, code="real_send_claim_failed",
                message="real_send_allowed: job claim failed unexpectedly",
                returncode=4, bulk_send_id=bulk_send_id,
                eligible_rows=eligible_row_count, max_rows=max_rows,
                dry_run=dry_run, authorization_code=decision.code,
                command_error_message="real_send_allowed: job claim failed unexpectedly",
            )
        claimed.state = BackgroundJob.STATE_RUNNING
        claimed.started_at = timezone.now()
        claimed.attempts = int(claimed.attempts or 0) + 1
        claimed.error = ""
        claimed.save(
            update_fields=["state", "started_at", "attempts", "error", "updated_at"]
        )

    run_claimed_job(claimed)
    claimed.refresh_from_db()

    return RealSendOutcome(
        executed=True, code=decision.code, message="", returncode=0,
        bulk_send_id=bulk_send_id, eligible_rows=eligible_row_count,
        max_rows=max_rows, dry_run=False, authorization_code=decision.code,
        job_id=claimed.pk, job_state=claimed.state, job_message=claimed.message,
    )
