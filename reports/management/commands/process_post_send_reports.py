from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction

from relay.models import BulkSend
from reports.models import GeneratedReport
from reports.services.processor import process_pending_reports
from reports.services.loader import load_report_to_db


REPORT_TYPES = ["deliveries"]


class Command(BaseCommand):
    help = "Crea y carga reportería post-envío para BulkSend (>=1h), sin llamadas en vivo desde la vista"

    def handle(self, *args, **options):
        now = timezone.now()
        cutoff = now - timedelta(hours=1)
        qs = BulkSend.objects.filter(
            status="done", created_at__lte=cutoff, post_reports_loaded_at__isnull=True)

        created_total = 0
        processed_ok = 0
        for bulk in qs.iterator():
            # Día local y UTC para cubrir desfases por zona horaria
            local_day = bulk.created_at.date()
            try:
                utc_day = bulk.created_at.astimezone(
                    __import__('datetime').timezone.utc).date()
            except Exception:
                utc_day = local_day
            days_to_request = {local_day, utc_day}

            # Crear GeneratedReport deliveries para ambos días
            for day in days_to_request:
                for t in REPORT_TYPES:
                    exists = GeneratedReport.objects.filter(
                        report_type=t, start_date=day, end_date=day).exists()
                    if not exists:
                        GeneratedReport.objects.create(
                            report_type=t,
                            start_date=day,
                            end_date=day,
                            state=GeneratedReport.STATE_PENDING,
                            requested_by=None,
                        )
                        created_total += 1
            # Refresco: si todos los GR del día están READY y cargados, crear uno nuevo para re-generar CSV
            for day in list(days_to_request):
                qs_day = GeneratedReport.objects.filter(
                    report_type__in=REPORT_TYPES, start_date=day, end_date=day)
                if qs_day.exists() and qs_day.filter(state=GeneratedReport.STATE_READY, loaded_to_db=True).count() == qs_day.count():
                    for t in REPORT_TYPES:
                        GeneratedReport.objects.create(
                            report_type=t,
                            start_date=day,
                            end_date=day,
                            state=GeneratedReport.STATE_PENDING,
                            requested_by=None,
                        )
                        created_total += 1

            # Resetear reportes en ERROR para reintento automático
            err_qs = GeneratedReport.objects.filter(
                report_type__in=REPORT_TYPES,
                start_date__in=list(days_to_request),
                end_date__in=list(days_to_request),
                state=GeneratedReport.STATE_ERROR,
            )
            for rep in err_qs.iterator():
                rep.state = GeneratedReport.STATE_PENDING
                rep.report_request_id = ""
                rep.file_path = ""
                rep.error_details = ""
                rep.save(update_fields=[
                    "state", "report_request_id", "file_path", "error_details", "updated_at"
                ])

            # Procesar pendientes y luego cargar a BD
            process_pending_reports()
            ready = GeneratedReport.objects.filter(
                start_date__in=list(days_to_request),
                end_date__in=list(days_to_request),
                state=GeneratedReport.STATE_READY,
            )

            total_inserted = 0
            for rep in ready.iterator():
                if not rep.loaded_to_db:
                    try:
                        total_inserted += load_report_to_db(
                            rep.pk, target_alias="default")
                    except Exception:
                        pass

            # Marcar trazabilidad solo si inserta filas
            if total_inserted > 0:
                bulk.post_reports_status = "done"
                bulk.post_reports_loaded_at = timezone.now()
                bulk.save(update_fields=[
                          "post_reports_status", "post_reports_loaded_at"])
                processed_ok += 1

        self.stdout.write(self.style.SUCCESS(
            f"Post-send reports: created={created_total}, bulks processed={processed_ok}"))
