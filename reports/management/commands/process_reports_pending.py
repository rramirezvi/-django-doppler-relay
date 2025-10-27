from django.core.management.base import BaseCommand

from reports.services.processor import process_pending_reports


class Command(BaseCommand):
    help = "Procesa los reportes en estado PENDING/PROCESSING (no bloquea requests web)"

    def handle(self, *args, **options):
        process_pending_reports()
        self.stdout.write(self.style.SUCCESS("Reportes pendientes procesados"))

