from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction

from relay.models import BulkSend
from reports.models import GeneratedReport
from reports.services.processor import process_pending_reports
from reports.services.loader import load_report_to_db


REPORT_TYPES = ["deliveries", "bounces", "opens", "clicks", "spam", "unsubscribed", "sent"]


class Command(BaseCommand):
    help = "Crea y carga reportería post-envío para BulkSend (>=1h), sin llamadas en vivo desde la vista"

    def handle(self, *args, **options):
        now = timezone.now()
        cutoff = now - timedelta(hours=1)
        qs = BulkSend.objects.filter(status="done", created_at__lte=cutoff, post_reports_loaded_at__isnull=True)

        created_total = 0
        processed_ok = 0
        for bulk in qs.iterator():
            day = bulk.created_at.date()
            # Crear GeneratedReport por tipo si no existe para ese día
            for t in REPORT_TYPES:
                exists = GeneratedReport.objects.filter(report_type=t, start_date=day, end_date=day).exists()
                if not exists:
                    GeneratedReport.objects.create(
                        report_type=t,
                        start_date=day,
                        end_date=day,
                        state=GeneratedReport.STATE_PENDING,
                        requested_by=None,
                    )
                    created_total += 1

            # Procesar pendientes y cargar a BD
            process_pending_reports()
            ready = GeneratedReport.objects.filter(start_date=day, end_date=day, state=GeneratedReport.STATE_READY)
            # Cargar a BD solo los que aún no fueron cargados a alias default
            for rep in ready:
                if not rep.loaded_to_db:
                    try:
                        load_report_to_db(rep.pk, target_alias="default")
                    except Exception:
                        # dejamos que el siguiente ciclo los intente cargar
                        pass

            # Marcar trazabilidad en BulkSend
            bulk.post_reports_status = "done"
            bulk.post_reports_loaded_at = timezone.now()
            bulk.save(update_fields=["post_reports_status", "post_reports_loaded_at"])
            processed_ok += 1

        self.stdout.write(self.style.SUCCESS(f"Post-send reports: created={created_total}, bulks processed={processed_ok}"))

