"""bulk-v2-real-send-canary management command (design.md §8, PR C thin
wrapper -- design round 8).

`python manage.py bulk_v2_real_send --bulk-send-id <int> [--dry-run]`

Argument surface is exactly two flags -- nothing else, unchanged. All
authorization/execution logic (the fourteen ordered checks) now lives in
`relay.services.bulk_v2_real_send_execute.authorize_and_execute_real_send`
-- this module only parses arguments, delegates the entire decision to
that function, and reproduces the exact same stdout messages and exit
codes the original monolithic command produced. No business logic lives
here anymore. This module never imports `csv`, never touches
`bulk.recipients_file`, and never imports `BulkImportService`.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from relay.services.bulk_v2_real_send_execute import authorize_and_execute_real_send


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

    def handle(self, *args, **options):
        bulk_send_id = options["bulk_send_id"]
        dry_run = bool(options["dry_run"])

        outcome = authorize_and_execute_real_send(bulk_send_id, dry_run=dry_run)

        for row in outcome.blocked_ambiguous_rows:
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

        if outcome.code == "real_send_dry_run":
            self.stdout.write(self.style.SUCCESS(
                f"DRY-RUN: {outcome.eligible_rows} fila(s) elegible(s), autorizacion "
                f"OK (code={outcome.authorization_code}). Nada fue reclamado ni enviado."
            ))

        if not outcome.executed:
            raise CommandError(outcome.command_error_message, returncode=outcome.returncode)

        self.stdout.write(self.style.SUCCESS(
            f"real_send_allowed: job {outcome.job_id} finalizado en estado "
            f"{outcome.job_state}: {outcome.job_message}"
        ))
