from __future__ import annotations

from django.core.management.base import BaseCommand
from relay.models import BulkSend


class Command(BaseCommand):
    help = "Rellena template_name en BulkSend cuando está vacío, con mejor esfuerzo; fallback al template_id"

    def handle(self, *args, **options):
        qs = BulkSend.objects.filter(template_name__isnull=True).exclude(template_id__isnull=True)
        total = 0
        for b in qs.iterator():
            try:
                # Disparar save() para que resuelva template_name centralizado
                b.save(update_fields=["template_name"])  # save override completa
                total += 1
            except Exception:
                # Fallback último: usar template_id como nombre legible
                b.template_name = str(b.template_id)
                b.save(update_fields=["template_name"]) 
                total += 1
        self.stdout.write(self.style.SUCCESS(f"Backfill template_name completado: {total} registros actualizados"))

