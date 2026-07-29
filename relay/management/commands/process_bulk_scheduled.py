from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from relay.models import BackgroundJob, BulkSend

BATCH_SIZE = 50


class Command(BaseCommand):
    help = "Encola envíos masivos programados (scheduled_at <= now) para el worker de BackgroundJob."

    def handle(self, *args, **options):
        now = timezone.now()
        # Seleccionar candidatos programados (scheduled_at no nulo y en el pasado)
        qs = (
            BulkSend.objects.filter(
                engine_version=BulkSend.ENGINE_LEGACY,
                status="pending",
                scheduled_at__isnull=False,
                scheduled_at__lte=now,
            )
            .order_by("scheduled_at")
        )

        enqueued = 0
        for bulk in qs[:BATCH_SIZE]:
            if self._enqueue(bulk.id):
                enqueued += 1

        self.stdout.write(self.style.SUCCESS(f"Scheduler encoló {enqueued} envíos"))

    def _enqueue(self, bulk_id: int) -> bool:
        try:
            with transaction.atomic():
                row = (
                    BulkSend.objects.select_for_update(skip_locked=True)
                    .filter(
                        id=bulk_id,
                        engine_version=BulkSend.ENGINE_LEGACY,
                        status="pending",
                        scheduled_at__lte=timezone.now(),
                    )
                    .first()
                )
                if not row:
                    return False
                has_active_job = BackgroundJob.objects.filter(
                    bulk=row,
                    job_type=BackgroundJob.TYPE_BULK_SEND,
                    state__in=[BackgroundJob.STATE_QUEUED, BackgroundJob.STATE_RUNNING],
                ).exists()
                if has_active_job:
                    return False
                row.processing_started_at = timezone.now()
                row.log = ((row.log or "") + "\n[Scheduler] Envio programado encolado").strip()
                row.save(update_fields=["processing_started_at", "log"])
                BackgroundJob.objects.create(
                    job_type=BackgroundJob.TYPE_BULK_SEND,
                    bulk=row,
                    triggered_by=row.scheduled_by,
                    message="Envio programado en cola",
                )
                return True
        except Exception:
            return False
