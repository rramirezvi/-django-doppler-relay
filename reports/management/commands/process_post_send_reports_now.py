from __future__ import annotations

from datetime import timedelta, timezone as dt_timezone

from django.core.management.base import BaseCommand
from django.utils import timezone

from relay.models import BulkSend
from reports.models import GeneratedReport
from reports.services.processor import process_pending_reports
from reports.services.loader import load_report_to_db


REPORT_TYPES = ["deliveries"]


class Command(BaseCommand):
    help = (
        "Procesa reportería post‑envío inmediatamente para BulkSend en 'done' "
        "(sin esperar 1 hora) y genera reportes para el día local y el día UTC "
        "para cubrir desfases por zona horaria."
    )

    def handle(self, *args, **options):
        # Tomar todos los bulks en done que aún no tengan post_reports_loaded_at
        qs = BulkSend.objects.filter(status="done", post_reports_loaded_at__isnull=True)

        created_total = 0
        processed_ok = 0

        for bulk in qs.iterator():
            # Determinar días objetivo: día local del envío y día UTC (para cubrir TZ)
            local_day = bulk.created_at.date()
            try:
                utc_day = bulk.created_at.astimezone(dt_timezone.utc).date()
            except Exception:
                utc_day = local_day
            days_to_request = {local_day, utc_day}

            # Crear GeneratedReport por tipo si no existe para cada día
            for day in days_to_request:
                for t in REPORT_TYPES:
                    if not GeneratedReport.objects.filter(report_type=t, start_date=day, end_date=day).exists():
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
            ready = GeneratedReport.objects.filter(
                start_date__in=list(days_to_request),
                end_date__in=list(days_to_request),
                state=GeneratedReport.STATE_READY,
            )
            # Cargar a BD solo los que aún no fueron cargados a alias default
            total_inserted = 0
            for rep in ready.iterator():
                if not rep.loaded_to_db:
                    try:
                        total_inserted += load_report_to_db(rep.pk, target_alias="default")
                    except Exception:
                        # lo dejamos para un siguiente intento
                        pass

            # Marcar trazabilidad en BulkSend para habilitar el botón de reporte
            if total_inserted > 0:
                bulk.post_reports_status = "done"
                bulk.post_reports_loaded_at = timezone.now()
                bulk.save(update_fields=["post_reports_status", "post_reports_loaded_at"])
                processed_ok += 1

        self.stdout.write(self.style.SUCCESS(
            f"Post-send reports NOW: created={created_total}, bulks processed={processed_ok}"
        ))

