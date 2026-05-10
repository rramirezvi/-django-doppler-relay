from __future__ import annotations

import logging
from datetime import timedelta, timezone as dt_timezone

from django.core.management.base import BaseCommand
from django.utils import timezone

from relay.models import BulkSend
from reports.models import GeneratedReport
from reports.services.loader import load_report_to_db
from reports.services.processor import process_pending_reports


REPORT_TYPES = ["deliveries"]
logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Crea y carga reportería post-envío para BulkSend, sin llamadas en vivo desde la vista."

    def add_arguments(self, parser):
        parser.add_argument("--bulk-id", type=int, dest="bulk_id", help="Procesa solo un BulkSend.")
        parser.add_argument("--force", action="store_true", help="No espera la regla de 1 hora.")
        parser.add_argument("--verbose-report", action="store_true", help="Imprime detalle operativo por consola.")

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(hours=1)
        bulk_id = options.get("bulk_id")
        force = bool(options.get("force"))
        verbose_report = bool(options.get("verbose_report"))

        qs = BulkSend.objects.filter(status="done")
        if bulk_id:
            qs = qs.filter(pk=bulk_id)
        if not force:
            qs = qs.filter(post_reports_loaded_at__isnull=True)
            qs = qs.filter(created_at__lte=cutoff)

        created_total = 0
        processed_ok = 0
        for bulk in qs.iterator():
            if verbose_report:
                self.stdout.write(f"Bulk {bulk.pk}: preparando reportería post-envío")

            local_day = bulk.created_at.date()
            try:
                utc_day = bulk.created_at.astimezone(dt_timezone.utc).date()
            except Exception:
                utc_day = local_day
            days_to_request = {local_day, utc_day}

            for day in days_to_request:
                for report_type in REPORT_TYPES:
                    existing_qs = GeneratedReport.objects.filter(
                        report_type=report_type,
                        start_date=day,
                        end_date=day,
                    )
                    rep = existing_qs.order_by("-id").first()
                    created = False
                    if rep is None:
                        rep = GeneratedReport.objects.create(
                            report_type=report_type,
                            start_date=day,
                            end_date=day,
                            state=GeneratedReport.STATE_PENDING,
                            requested_by=None,
                        )
                        created = True
                    if created:
                        created_total += 1
                        if verbose_report:
                            self.stdout.write(f"  creado GeneratedReport {report_type} {day}")
                    elif force and rep.state in {GeneratedReport.STATE_READY, GeneratedReport.STATE_ERROR}:
                        rep.state = GeneratedReport.STATE_PENDING
                        rep.report_request_id = ""
                        rep.file_path = ""
                        rep.error_details = ""
                        rep.loaded_to_db = False
                        rep.loaded_at = None
                        rep.rows_inserted = 0
                        rep.last_loaded_alias = ""
                        rep.save(update_fields=[
                            "state",
                            "report_request_id",
                            "file_path",
                            "error_details",
                            "loaded_to_db",
                            "loaded_at",
                            "rows_inserted",
                            "last_loaded_alias",
                            "updated_at",
                        ])
                        if verbose_report:
                            self.stdout.write(f"  refresco GeneratedReport {rep.pk}: READY/ERROR -> PENDING")

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
                    "state",
                    "report_request_id",
                    "file_path",
                    "error_details",
                    "updated_at",
                ])
                if verbose_report:
                    self.stdout.write(f"  reintento GeneratedReport {rep.pk}: ERROR -> PENDING")

            process_pending_reports()
            ready = GeneratedReport.objects.filter(
                report_type__in=REPORT_TYPES,
                start_date__in=list(days_to_request),
                end_date__in=list(days_to_request),
                state=GeneratedReport.STATE_READY,
            )

            total_inserted = 0
            for rep in ready.iterator():
                if rep.loaded_to_db:
                    continue
                try:
                    inserted = load_report_to_db(rep.pk, target_alias="default")
                    total_inserted += inserted
                    if verbose_report:
                        self.stdout.write(self.style.SUCCESS(
                            f"  loaded GeneratedReport {rep.pk}: rows={inserted}"
                        ))
                except Exception as exc:
                    message = f"Error cargando GeneratedReport {rep.pk} para BulkSend {bulk.pk}: {exc}"
                    logger.exception(message)
                    rep.error_details = message
                    rep.save(update_fields=["error_details", "updated_at"])
                    bulk.post_reports_status = "error"
                    bulk.log = ((bulk.log or "") + f"\n[REPORT] {message}").strip()
                    bulk.save(update_fields=["post_reports_status", "log"])
                    if verbose_report:
                        self.stdout.write(self.style.ERROR(f"  {message}"))

            ready_loaded = ready.filter(loaded_to_db=True).exists()
            if total_inserted > 0 or ready_loaded:
                bulk.post_reports_status = "done"
                bulk.post_reports_loaded_at = timezone.now()
                bulk.save(update_fields=["post_reports_status", "post_reports_loaded_at"])
                processed_ok += 1
            elif bulk.post_reports_status != "error":
                bulk.post_reports_status = "pending"
                bulk.log = (
                    (bulk.log or "")
                    + "\n[REPORT] Sin filas nuevas cargadas; se reintentará en la próxima pasada."
                ).strip()
                bulk.save(update_fields=["post_reports_status", "log"])
                if verbose_report:
                    self.stdout.write(self.style.WARNING(f"  Bulk {bulk.pk}: 0 filas insertadas"))

        self.stdout.write(self.style.SUCCESS(
            f"Post-send reports: created={created_total}, bulks processed={processed_ok}"
        ))
