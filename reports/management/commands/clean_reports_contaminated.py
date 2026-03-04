from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand, CommandParser
from django.db import connection
from django.utils.dateparse import parse_date

from reports.models import GeneratedReport


TABLES = [
    "reports_deliveries",
    "reports_bounces",
    "reports_opens",
    "reports_clicks",
    "reports_spam",
    "reports_unsubscribed",
    "reports_sent",
]


class Command(BaseCommand):
    help = "Elimina filas contaminadas en reports_* (por generated_report_id del día) o hace TRUNCATE controlado"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--day", type=str, help="Día (YYYY-MM-DD) para eliminar por generated_report_id")
        parser.add_argument("--truncate", action="store_true", help="Truncar todas las reports_* (peligroso)")

    def handle(self, *args, **options):
        if options.get("truncate"):
            with connection.cursor() as cur:
                for t in TABLES:
                    try:
                        cur.execute(f"TRUNCATE TABLE {t}")
                        self.stdout.write(self.style.WARNING(f"TRUNCATE {t}"))
                    except Exception as exc:
                        self.stdout.write(self.style.ERROR(f"No se pudo truncar {t}: {exc}"))
            return

        day_str = options.get("day")
        d: date | None = parse_date(day_str) if day_str else None
        if not d:
            self.stdout.write(self.style.ERROR("Debe indicar --day YYYY-MM-DD o --truncate"))
            return

        reps = list(GeneratedReport.objects.filter(start_date=d, end_date=d).values_list("id", flat=True))
        if not reps:
            self.stdout.write(self.style.WARNING("No hay GeneratedReport para ese día"))
            return

        id_list = ",".join(str(i) for i in reps)
        with connection.cursor() as cur:
            total = 0
            for t in TABLES:
                try:
                    cur.execute(f"DELETE FROM {t} WHERE generated_report_id IN ({id_list})")
                    deleted = cur.rowcount if hasattr(cur, "rowcount") else 0
                    total += max(deleted, 0)
                    self.stdout.write(self.style.SUCCESS(f"{t}: {deleted} filas borradas"))
                except Exception as exc:
                    self.stdout.write(self.style.ERROR(f"{t}: error eliminando filas: {exc}"))
        self.stdout.write(self.style.SUCCESS(f"Total filas borradas: {total}"))

