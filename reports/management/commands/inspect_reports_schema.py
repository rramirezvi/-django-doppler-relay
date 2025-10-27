from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone

from reports.models import GeneratedReport
from reports.services.processor import process_pending_reports
from reports.utils.schema_infer import infer_csv_schema, save_schema_json


VALID_TYPES = [
    "deliveries",
    "bounces",
    "opens",
    "clicks",
    "spam",
    "unsubscribed",
    "sent",
]


class Command(BaseCommand):
    help = "Solicita y procesa reportes por tipo y genera esquemas inferidos de sus CSVs."

    def add_arguments(self, parser):
        parser.add_argument("--types", nargs="*", default=VALID_TYPES, help="Tipos a inspeccionar")
        parser.add_argument("--days", type=int, default=1, help="Rango de días hacia atrás para generar muestras")
        parser.add_argument("--only-existing", action="store_true", help="No solicitar, solo inferir de CSVs existentes")
        parser.add_argument("--out", default="attachments/reports/schemas", help="Directorio de salida JSON")

    def handle(self, *args, **opts):
        types = [t.strip().lower() for t in opts["types"] if t.strip()]
        days = int(opts["days"]) or 1
        out_dir = Path(opts["out"]).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        if not opts["only_existing"]:
            start = timezone.localdate() - timedelta(days=days)
            end = timezone.localdate()
            # Crear solicitudes PENDING
            for t in types:
                GeneratedReport.objects.create(
                    report_type=t, start_date=start, end_date=end, state=GeneratedReport.STATE_PENDING
                )
            # Procesar hasta READY y descargar CSV
            process_pending_reports()

        # Inferir esquemas para los últimos READY por tipo
        for t in types:
            rep = (
                GeneratedReport.objects.filter(report_type=t, state=GeneratedReport.STATE_READY)
                .order_by("-id")
                .first()
            )
            if not rep or not rep.file_path:
                self.stdout.write(self.style.WARNING(f"No hay CSV para tipo {t}"))
                continue
            schema = infer_csv_schema(Path(rep.file_path))
            out_path = out_dir / f"schema_{t}.json"
            save_schema_json(schema, out_path)
            self.stdout.write(self.style.SUCCESS(f"Esquema {t} → {out_path}"))

