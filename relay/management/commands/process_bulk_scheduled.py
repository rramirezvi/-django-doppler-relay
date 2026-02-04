from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from relay.models import BulkSend
from relay.services.bulk_processing import process_bulk_id

BATCH_SIZE = 50


class Command(BaseCommand):
    help = "Procesa envíos masivos programados (scheduled_at <= now) sin Celery"

    def handle(self, *args, **options):
        now = timezone.now()
        # Seleccionar candidatos programados (scheduled_at no nulo y en el pasado)
        qs = (
            BulkSend.objects.filter(status="pending", scheduled_at__isnull=False, scheduled_at__lte=now)
            .order_by("scheduled_at")
        )

        processed = 0
        for bulk in qs[:BATCH_SIZE]:
            # Intentar tomar lock/flag para evitar solapes
            acquired = self._acquire(bulk.id)
            if not acquired:
                continue
            try:
                self._process_bulk(bulk)
                processed += 1
            except Exception as exc:
                bulk.status = "error"
                bulk.log = (bulk.log or "") + f"\n[Scheduler] Error: {exc}"
                bulk.save(update_fields=["status", "log"])

        self.stdout.write(self.style.SUCCESS(f"Scheduler procesó {processed} envíos"))

    def _acquire(self, bulk_id: int) -> bool:
        try:
            with transaction.atomic():
                row = (
                    BulkSend.objects.select_for_update(skip_locked=True)
                    .filter(id=bulk_id, status="pending")
                    .first()
                )
                if not row:
                    return False
                row.processing_started_at = timezone.now()
                row.save(update_fields=["processing_started_at"])
                return True
        except Exception:
            return False

    def _process_bulk(self, bulk: BulkSend) -> None:
        # Delegar el procesamiento completo al helper unificado
        process_bulk_id(bulk.id)
