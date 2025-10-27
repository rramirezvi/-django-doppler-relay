from __future__ import annotations

import logging
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from reports.models import GeneratedReport
from .doppler_reports import (
    create_report_request,
    wait_until_processed,
    download_report_csv,
    build_report_filename,
    ATTACHMENTS_ROOT,
    ReportError,
)

logger = logging.getLogger(__name__)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def process_pending_reports() -> None:
    """Procesa en lote los reportes PENDING/PROCESSING sin depender de requests web."""
    # 1) Pedir IDs a Doppler para los PENDING
    pending = GeneratedReport.objects.filter(state=GeneratedReport.STATE_PENDING)
    for rep in pending.iterator():
        try:
            report_id = create_report_request(rep.start_date, rep.end_date, rep.report_type)
            rep.report_request_id = report_id
            rep.state = GeneratedReport.STATE_PROCESSING
            rep.error_details = ""
            rep.save(update_fields=["report_request_id", "state", "error_details", "updated_at"])
            logger.info("Reporte %s marcado como PROCESSING con id=%s", rep.pk, report_id)
        except ReportError as exc:
            rep.state = GeneratedReport.STATE_ERROR
            rep.error_details = str(exc)
            rep.save(update_fields=["state", "error_details", "updated_at"])
            logger.exception("Error solicitando reporte (id=%s): %s", rep.pk, exc)
        except Exception as exc:  # safety net
            rep.state = GeneratedReport.STATE_ERROR
            rep.error_details = str(exc)
            rep.save(update_fields=["state", "error_details", "updated_at"])
            logger.exception("Error inesperado solicitando reporte (id=%s): %s", rep.pk, exc)

    # 2) Poll para los PROCESSING
    processing = GeneratedReport.objects.filter(state=GeneratedReport.STATE_PROCESSING).exclude(report_request_id="")
    for rep in processing.iterator():
        try:
            _ = wait_until_processed(rep.report_request_id)
            csv_bytes = download_report_csv(rep.report_request_id)

            ensure_dir(ATTACHMENTS_ROOT)
            filename = build_report_filename(rep.report_type)
            target = ATTACHMENTS_ROOT / filename
            with open(target, "wb") as fh:
                fh.write(csv_bytes)

            rep.file_path = str(target)
            rep.state = GeneratedReport.STATE_READY
            rep.error_details = ""
            rep.save(update_fields=["file_path", "state", "error_details", "updated_at"])
            logger.info("Reporte %s listo en %s", rep.pk, target)
        except ReportError as exc:
            rep.state = GeneratedReport.STATE_ERROR
            rep.error_details = str(exc)
            rep.save(update_fields=["state", "error_details", "updated_at"])
            logger.exception("Error procesando reporte (id=%s): %s", rep.pk, exc)
        except Exception as exc:
            rep.state = GeneratedReport.STATE_ERROR
            rep.error_details = str(exc)
            rep.save(update_fields=["state", "error_details", "updated_at"])
            logger.exception("Error inesperado procesando reporte (id=%s): %s", rep.pk, exc)

