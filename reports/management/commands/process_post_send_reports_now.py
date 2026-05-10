from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Procesa reportería post-envío inmediatamente, sin esperar 1 hora."

    def add_arguments(self, parser):
        parser.add_argument("--bulk-id", type=int, dest="bulk_id", help="Procesa solo un BulkSend.")

    def handle(self, *args, **options):
        kwargs = {"force": True, "verbose_report": True}
        if options.get("bulk_id"):
            kwargs["bulk_id"] = options["bulk_id"]
        call_command("process_post_send_reports", **kwargs)
