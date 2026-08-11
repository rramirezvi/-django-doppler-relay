"""bulk-v2-real-send-canary management command (design.md §8).

`python manage.py bulk_v2_real_send --bulk-send-id <int> [--dry-run]`

Argument surface is exactly two flags — nothing else. No `--force`, no
`--retry-ambiguous`, no `--yes`, no recipient argument of any kind, no
`--template`, no `--file`, no `--limit`. This module never imports `csv`,
never touches `bulk.recipients_file`, and never imports
`BulkImportService` (design §8.1, §6).

Fourteen ordered checks, first failure wins, each with its own refusal
code and exit code (design §8.2's table, implemented verbatim). Every
check 2-13 emits exactly one `bulk_v2_real_send_decision` INFO event and
terminates via `CommandError(message, returncode=N)`.
"""

from __future__ import annotations

import hashlib
import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from relay.models import BackgroundJob, BulkSend
from relay.services.bulk_v2_real_send import evaluate_real_send
from relay.services.bulk_v2_send_state import describe_send_ledger
from relay.services.jobs import run_claimed_job

logger = logging.getLogger(__name__)


def _request_fingerprint(client_request_id: str) -> str:
    # Reuses the existing sha256(client_request_id)[:12] convention
    # (relay/api.py:447-455) rather than inventing a second one.
    return hashlib.sha256(
        str(client_request_id or "").encode("utf-8")
    ).hexdigest()[:12]


class Command(BaseCommand):
    help = "Ejecuta el canary de envio real V2 (bulk-v2-real-send-canary)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--bulk-send-id",
            dest="bulk_send_id",
            type=int,
            required=True,
            help="ID del BulkSend objetivo. Obligatorio, sin valor 'latest' ni 'all'.",
        )
        parser.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            help="Ejecuta todos los checks y el reporte del ledger, y se detiene antes del claim.",
        )

    def _log_decision(
        self,
        *,
        decision: str,
        code: str,
        bulk_send_id: int,
        eligible_rows: int,
        max_rows,
    ) -> None:
        logger.info(
            "bulk_v2_real_send_decision decision=%s code=%s bulk_send_id=%s "
            "request=%s eligible_rows=%s max_rows=%s at=%s",
            decision,
            code,
            bulk_send_id,
            self._fingerprint,
            eligible_rows,
            max_rows,
            timezone.now().isoformat(),
        )

    def _refuse(
        self,
        *,
        code: str,
        message: str,
        returncode: int,
        bulk_send_id,
        eligible_rows=0,
        max_rows=None,
    ) -> None:
        self._log_decision(
            decision="refused",
            code=code,
            bulk_send_id=bulk_send_id,
            eligible_rows=eligible_rows,
            max_rows=max_rows if max_rows is not None else settings.BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS,
        )
        raise CommandError(f"{code}: {message}", returncode=returncode)

    def handle(self, *args, **options):
        bulk_send_id = options["bulk_send_id"]
        dry_run = bool(options["dry_run"])
        self._fingerprint = ""  # populated once the BulkSend is read

        # Check 2: kill switch. Zero DB queries executed before this
        # point or by this check itself (design §8.2 "the kill switch is
        # check 2, before any database access").
        if settings.BULK_PROCESSING_V2_REAL_SEND_ENABLED is not True:
            self._refuse(
                code="real_send_disabled",
                message="El envio real V2 no esta habilitado.",
                returncode=5,
                bulk_send_id=bulk_send_id,
            )

        # Check 3: BulkSend exists.
        bulk = BulkSend.objects.filter(pk=bulk_send_id).first()
        if bulk is None:
            self._refuse(
                code="real_send_bulk_not_found",
                message=f"No existe BulkSend {bulk_send_id}.",
                returncode=2,
                bulk_send_id=bulk_send_id,
            )

        self._fingerprint = _request_fingerprint(bulk.client_request_id)

        # Check 4: engine_version == v2.
        if bulk.engine_version != BulkSend.ENGINE_V2:
            self._refuse(
                code="real_send_engine_not_v2",
                message="El BulkSend no usa el motor v2.",
                returncode=2,
                bulk_send_id=bulk_send_id,
            )

        # Check 5: import_status ready or ready_with_errors.
        if bulk.import_status not in {
            BulkSend.IMPORT_READY,
            BulkSend.IMPORT_READY_WITH_ERRORS,
        }:
            self._refuse(
                code="real_send_import_not_ready",
                message="La importacion V2 no esta lista.",
                returncode=2,
                bulk_send_id=bulk_send_id,
            )

        # Check 6: no queued/running job of this type already exists for
        # this bulk (defence in depth; NOT the safety boundary — see
        # design §7, jobs.py's comment, and PR2b-T28).
        existing_job = BackgroundJob.objects.filter(
            bulk_id=bulk.pk,
            job_type=BackgroundJob.TYPE_BULK_SEND_V2_REAL,
            state__in=[BackgroundJob.STATE_QUEUED, BackgroundJob.STATE_RUNNING],
        ).exists()
        if existing_job:
            self._refuse(
                code="real_send_job_already_present",
                message="Ya existe un job bulk_send_v2_real en curso para este BulkSend.",
                returncode=4,
                bulk_send_id=bulk_send_id,
            )

        # Check 7: read-only ledger.
        ledger = describe_send_ledger(bulk.pk)
        eligible_row_count = len(ledger.eligible)
        max_rows = settings.BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS

        # Check 8: ambiguous rows abort everything, exit 3, touch nothing.
        if ledger.blocked_ambiguous:
            for row in ledger.blocked_ambiguous:
                self.stdout.write(
                    "AMBIGUOUS row: idempotency_key=%s send_started_at=%s "
                    "send_attempt_number=%s send_error_code=%s "
                    "send_message_id=%s send_job_id=%s"
                    % (
                        row.recipient_key,
                        row.send_started_at,
                        row.attempt_number,
                        row.error_code,
                        row.message_id,
                        row.job_id,
                    )
                )
            self._refuse(
                code="real_send_ambiguous_present",
                message="Existen filas ambiguous; requiere resolucion humana. Canary abortado.",
                returncode=3,
                bulk_send_id=bulk_send_id,
                eligible_rows=eligible_row_count,
                max_rows=max_rows,
            )

        # Check 9: stale sending rows abort, exit 4, touch nothing.
        if ledger.stale_sending:
            self._refuse(
                code="real_send_stale_sending_present",
                message="Existen filas sending envejecidas; requiere inspeccion humana.",
                returncode=4,
                bulk_send_id=bulk_send_id,
                eligible_rows=eligible_row_count,
                max_rows=max_rows,
            )

        # Check 10: in-flight rows abort, exit 4, touch nothing.
        if ledger.in_flight:
            self._refuse(
                code="real_send_in_flight_present",
                message="Existe una fila sending en curso; otro worker puede tenerla.",
                returncode=4,
                bulk_send_id=bulk_send_id,
                eligible_rows=eligible_row_count,
                max_rows=max_rows,
            )

        # Check 11: nothing eligible -> clean no-op, exit 0 (success).
        if not ledger.eligible:
            self._refuse(
                code="real_send_nothing_to_send",
                message="No hay filas elegibles para el envio real V2 (no-op limpio).",
                returncode=0,
                bulk_send_id=bulk_send_id,
                eligible_rows=eligible_row_count,
                max_rows=max_rows,
            )

        # Check 12: full authorization gate.
        #
        # NOTE (discovered gap, flagged explicitly — design.md does not
        # specify this plumbing): evaluate_real_send requires a user_id,
        # but BulkSend stores no "owner"/created-by field of its own — its
        # only User FK is `scheduled_by`, which V2 real-send scope
        # explicitly forbids populating (spec: "zero scheduling"). The
        # command's argument surface is closed by design (no --user-id
        # flag permitted), so the only available signal is
        # `bulk.scheduled_by_id`, which will be None for essentially every
        # real V2 bulk. This means the gate fails closed
        # (real_send_user_not_allowed) for any BulkSend that never went
        # through V1 scheduling, until an owner association is added to
        # BulkSend or this command's contract is revisited — a residual
        # design gap, not silently resolved. It does not weaken safety:
        # the gate simply refuses more often than the allowlist alone
        # would suggest.
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
            self._refuse(
                code=decision.code,
                message=decision.message,
                returncode=5,
                bulk_send_id=bulk_send_id,
                eligible_rows=eligible_row_count,
                max_rows=max_rows,
            )

        self._log_decision(
            decision="allowed",
            code=decision.code,
            bulk_send_id=bulk_send_id,
            eligible_rows=eligible_row_count,
            max_rows=max_rows,
        )

        # Check 13: --dry-run stops here, exit 0, nothing claimed.
        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f"DRY-RUN: {eligible_row_count} fila(s) elegible(s), autorizacion "
                f"OK (code={decision.code}). Nada fue reclamado ni enviado."
            ))
            self._refuse(
                code="real_send_dry_run",
                message="Dry-run: autorizado, nada ejecutado.",
                returncode=0,
                bulk_send_id=bulk_send_id,
                eligible_rows=eligible_row_count,
                max_rows=max_rows,
            )

        # Check 14: execute. Create the BackgroundJob, claim it through
        # the LOCKED claim path (never run_background_job's bypass, design
        # §7), then dispatch via run_claimed_job.
        job = BackgroundJob.objects.create(
            job_type=BackgroundJob.TYPE_BULK_SEND_V2_REAL,
            bulk=bulk,
            triggered_by=None,
            state=BackgroundJob.STATE_QUEUED,
        )
        with transaction.atomic():
            claimed = (
                BackgroundJob.objects
                .select_for_update(skip_locked=True)
                .filter(pk=job.pk, state=BackgroundJob.STATE_QUEUED)
                .first()
            )
            if claimed is None:  # pragma: no cover - defensive, unreachable in a single-shell invocation
                raise CommandError(
                    "real_send_allowed: job claim failed unexpectedly", returncode=4
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
        self.stdout.write(self.style.SUCCESS(
            f"real_send_allowed: job {claimed.pk} finalizado en estado "
            f"{claimed.state}: {claimed.message}"
        ))
