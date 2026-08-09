# bulk-v2-real-send-canary (design.md §2.5, §3, §4, §5, §6): the single
# module through which every `send_status` write in the codebase happens.
#
# Structural constraints, verified by PR2a-T9's grep test and by the
# mechanical firewall check:
#   - Zero import of `requests`, `DopplerRelayClient`, `doppler_relay`, or
#     anything Doppler-related. This module only touches the database.
#   - Zero import of `csv`, zero reference to `recipients_file` or
#     `BulkImportService` (design §6 "Zero CSV dependency is structural").
#   - `BackgroundJob` is never imported as an executable dependency —
#     `send_job_id` is a plain integer field on the model, not an FK.
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from relay.models import BulkSendRecipient

# design.md §5 — derived from the existing 30s Doppler timeout setting
# (config/settings.py DOPPLER_RELAY["TIMEOUT"]), NOT a new Django setting.
# It authorizes nothing; it only changes how a stale-sending report is
# worded, so it must not blur the six-flag real-send authorization surface.
STALE_SENDING_AFTER = timedelta(seconds=10 * settings.DOPPLER_RELAY["TIMEOUT"])

# design.md §2.5 point 2 — the only edges this module will ever issue an
# UPDATE for. Asserted by PR2a-T9's structural test and reused by
# PR2b-T33's "no ambiguous -> sent / ambiguous -> retry" structural test.
LEGAL_TRANSITIONS = frozenset({
    ("not_started", "sending"),
    ("sending", "sent"),
    ("sending", "send_failed"),
    ("sending", "ambiguous"),
})


class SendStateError(Exception):
    """Raised when a caller violates a hard runtime invariant of the send
    state machine (design.md §4) — e.g. attempting to claim/transition
    outside an autocommit context."""


class SendStateTransitionError(Exception):
    """Raised when a compare-and-set transition matches zero rows: the
    row was not in the expected `FROM` state (lost race, already
    terminal, or never reached `sending`)."""


def ensure_autocommit_context() -> None:
    """Runtime guard per design.md §4: the outbound Doppler call must never
    be made against an attempt that has not yet been committed. Called at
    the top of `process_bulk_id_v2` (PR2b-T1), converting "committed before
    the call" from a review promise into a tested runtime invariant.
    """
    if not transaction.get_autocommit():
        raise SendStateError(
            "real send must not run inside an open transaction"
        )


def claim_next_recipient(bulk_send_id: int, *, job_id: int) -> int | None:
    """Atomically move exactly one eligible row not_started -> sending.

    Returns the claimed row pk, or None. The transition is COMMITTED when
    this function returns; no network call has been made inside it.
    """
    now = timezone.now()
    with transaction.atomic():
        row = (
            BulkSendRecipient.objects
            .select_for_update(skip_locked=True)
            .filter(
                bulk_send_id=bulk_send_id,
                status=BulkSendRecipient.STATUS_PENDING,        # invariant 1, layer 1
                send_status=BulkSendRecipient.SEND_NOT_STARTED,
            )
            .order_by("import_version", "source_row_number")    # deterministic
            .values_list("pk", flat=True)
            .first()
        )
        if row is None:
            return None
        updated = (
            BulkSendRecipient.objects
            .filter(
                pk=row,
                status=BulkSendRecipient.STATUS_PENDING,
                send_status=BulkSendRecipient.SEND_NOT_STARTED,  # compare-and-set
            )
            .update(
                send_status=BulkSendRecipient.SEND_SENDING,
                send_started_at=now,
                send_attempt_number=F("send_attempt_number") + 1,
                send_job_id=job_id,
                updated_at=now,
            )
        )
        if updated != 1:
            return None          # lost the race; defensive, unreachable under a real lock
    return row                   # <- commit happened at the end of the with-block


def mark_sent(
    row_pk: int, *, message_id: str, location: str, now: datetime | None = None
) -> None:
    """Compare-and-set `sending -> sent`. Raises SendStateTransitionError
    if the row was not `sending` (design §2.5, §2.3 invariant 2)."""
    now = now or timezone.now()
    updated = (
        BulkSendRecipient.objects
        .filter(pk=row_pk, send_status=BulkSendRecipient.SEND_SENDING)
        .update(
            send_status=BulkSendRecipient.SEND_SENT,
            sent_at=now,
            send_message_id=message_id or "",
            send_location=location or "",
            updated_at=now,
        )
    )
    if updated != 1:
        raise SendStateTransitionError(
            "sending -> sent rejected: row not in sending"
        )


def mark_send_failed(
    row_pk: int, *, error_code: str, error_message: str, now: datetime | None = None
) -> None:
    """Compare-and-set `sending -> send_failed`. `error_code` MUST be
    non-empty (design §2.3 invariant 5, enforced additionally by the DB
    constraint `bulk_recipient_send_outcome_requires_error_code`)."""
    if not error_code:
        raise SendStateTransitionError(
            "send_failed requires a non-empty error_code"
        )
    now = now or timezone.now()
    updated = (
        BulkSendRecipient.objects
        .filter(pk=row_pk, send_status=BulkSendRecipient.SEND_SENDING)
        .update(
            send_status=BulkSendRecipient.SEND_FAILED,
            send_error_code=error_code,
            send_error_message=(error_message or "")[:255],
            updated_at=now,
        )
    )
    if updated != 1:
        raise SendStateTransitionError(
            "sending -> send_failed rejected: row not in sending"
        )


def mark_ambiguous(
    row_pk: int, *, error_code: str, error_message: str, now: datetime | None = None
) -> None:
    """Compare-and-set `sending -> ambiguous`. Only reachable from
    `sending` (design §2.3 invariant 4) — never directly from
    `not_started`, which is why the WHERE clause names `sending` as the
    only legal FROM state, exactly like `mark_sent`/`mark_send_failed`.
    `error_code` MUST be non-empty (design §2.4's constraint covers
    `ambiguous` too, "at zero cost")."""
    if not error_code:
        raise SendStateTransitionError(
            "ambiguous requires a non-empty error_code"
        )
    now = now or timezone.now()
    updated = (
        BulkSendRecipient.objects
        .filter(pk=row_pk, send_status=BulkSendRecipient.SEND_SENDING)
        .update(
            send_status=BulkSendRecipient.SEND_AMBIGUOUS,
            send_error_code=error_code,
            send_error_message=(error_message or "")[:255],
            updated_at=now,
        )
    )
    if updated != 1:
        raise SendStateTransitionError(
            "sending -> ambiguous rejected: row not in sending"
        )


@dataclass(frozen=True)
class LedgerRow:
    pk: int
    recipient_key: str          # str(idempotency_key)
    domain: str                 # normalized_recipient split on the last "@"
    klass: str                  # one of the seven classification buckets
    send_status: str
    send_started_at: datetime | None
    age_seconds: float | None
    attempt_number: int
    error_code: str
    message_id: str
    job_id: int | None


@dataclass(frozen=True)
class SendLedger:
    bulk_send_id: int
    rows: tuple[LedgerRow, ...]
    excluded: tuple[LedgerRow, ...]
    eligible: tuple[LedgerRow, ...]
    in_flight: tuple[LedgerRow, ...]
    stale_sending: tuple[LedgerRow, ...]
    terminal_sent: tuple[LedgerRow, ...]
    terminal_failed: tuple[LedgerRow, ...]
    blocked_ambiguous: tuple[LedgerRow, ...]


def _domain_of(normalized_recipient: str) -> str:
    text = normalized_recipient or ""
    if "@" not in text:
        return ""
    return text.rsplit("@", 1)[-1]


def describe_send_ledger(bulk_send_id: int, *, now: datetime | None = None) -> SendLedger:
    """Read-only recovery/inspection function (design §6). Classifies every
    recipient row for `bulk_send_id` into exactly one of seven mutually
    exclusive buckets, using persisted database state alone.

    Zero CSV dependency is structural: this function never imports `csv`,
    never opens `bulk.recipients_file`, and never imports
    `BulkImportService`. Behaviour is identical whether the original CSV
    is present, missing, or corrupted, because nothing here reads it.
    """
    now = now or timezone.now()
    rows: list[LedgerRow] = []
    excluded: list[LedgerRow] = []
    eligible: list[LedgerRow] = []
    in_flight: list[LedgerRow] = []
    stale_sending: list[LedgerRow] = []
    terminal_sent: list[LedgerRow] = []
    terminal_failed: list[LedgerRow] = []
    blocked_ambiguous: list[LedgerRow] = []

    queryset = (
        BulkSendRecipient.objects
        .filter(bulk_send_id=bulk_send_id)
        .values(
            "pk", "idempotency_key", "normalized_recipient", "status",
            "send_status", "send_started_at", "sent_at",
            "send_attempt_number", "send_error_code", "send_message_id",
            "send_job_id",
        )
        .order_by("import_version", "source_row_number")
    )

    for record in queryset:
        send_started_at = record["send_started_at"]
        age_seconds = (
            (now - send_started_at).total_seconds()
            if send_started_at is not None
            else None
        )
        send_status = record["send_status"]

        if record["status"] == BulkSendRecipient.STATUS_INVALID:
            klass = "excluded"
        elif send_status == BulkSendRecipient.SEND_NOT_STARTED:
            klass = "eligible"
        elif send_status == BulkSendRecipient.SEND_SENDING:
            is_stale = (
                age_seconds is not None
                and age_seconds > STALE_SENDING_AFTER.total_seconds()
            )
            klass = "stale_sending" if is_stale else "in_flight"
        elif send_status == BulkSendRecipient.SEND_SENT:
            klass = "terminal_sent"
        elif send_status == BulkSendRecipient.SEND_FAILED:
            klass = "terminal_failed"
        elif send_status == BulkSendRecipient.SEND_AMBIGUOUS:
            klass = "blocked_ambiguous"
        else:  # pragma: no cover - unreachable given the DB CHECK constraint
            klass = "eligible"

        ledger_row = LedgerRow(
            pk=record["pk"],
            recipient_key=str(record["idempotency_key"]),
            domain=_domain_of(record["normalized_recipient"]),
            klass=klass,
            send_status=send_status,
            send_started_at=send_started_at,
            age_seconds=age_seconds,
            attempt_number=record["send_attempt_number"],
            error_code=record["send_error_code"],
            message_id=record["send_message_id"],
            job_id=record["send_job_id"],
        )
        rows.append(ledger_row)
        {
            "excluded": excluded,
            "eligible": eligible,
            "in_flight": in_flight,
            "stale_sending": stale_sending,
            "terminal_sent": terminal_sent,
            "terminal_failed": terminal_failed,
            "blocked_ambiguous": blocked_ambiguous,
        }[klass].append(ledger_row)

    return SendLedger(
        bulk_send_id=bulk_send_id,
        rows=tuple(rows),
        excluded=tuple(excluded),
        eligible=tuple(eligible),
        in_flight=tuple(in_flight),
        stale_sending=tuple(stale_sending),
        terminal_sent=tuple(terminal_sent),
        terminal_failed=tuple(terminal_failed),
        blocked_ambiguous=tuple(blocked_ambiguous),
    )


# design.md §6 — the ordered 5-rule recovery decision. Pure function over an
# already-computed SendLedger (zero DB access of its own), consumed by the
# management command (PR2b). No branch here ever transitions a row; it only
# names which branch a caller should take.
RECOVERY_ABORT_AMBIGUOUS = "abort_ambiguous"
RECOVERY_ABORT_STALE_SENDING = "abort_stale_sending"
RECOVERY_ABORT_IN_FLIGHT = "abort_in_flight"
RECOVERY_CLEAN_NO_OP = "clean_no_op"
RECOVERY_PROCEED_TO_GATE = "proceed_to_gate"


def next_recovery_step(ledger: SendLedger) -> str:
    """(1) blocked_ambiguous non-empty -> abort. (2) stale_sending non-empty
    -> abort. (3) in_flight non-empty -> abort. (4) eligible empty -> clean
    no-op (already-completed case, a success). (5) otherwise -> proceed to
    the authorization gate. Evaluated in this exact order; nothing here
    acts on a row, it only reports what the caller should do next."""
    if ledger.blocked_ambiguous:
        return RECOVERY_ABORT_AMBIGUOUS
    if ledger.stale_sending:
        return RECOVERY_ABORT_STALE_SENDING
    if ledger.in_flight:
        return RECOVERY_ABORT_IN_FLIGHT
    if not ledger.eligible:
        return RECOVERY_CLEAN_NO_OP
    return RECOVERY_PROCEED_TO_GATE
