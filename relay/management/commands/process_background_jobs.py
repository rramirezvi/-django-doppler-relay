from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from relay.models import BackgroundJob
from relay.services.jobs import claim_next_job, run_claimed_job


class Command(BaseCommand):
    help = "Procesa BackgroundJob en estado queued."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10, help="Cantidad máxima de jobs a procesar por ejecución.")
        parser.add_argument("--loop", action="store_true", help="Corre indefinidamente, esperando nuevos jobs.")
        parser.add_argument("--sleep", type=float, default=3.0, help="Segundos de espera cuando no hay jobs en loop.")
        parser.add_argument(
            "--types",
            default="",
            help="Lista separada por coma de job_type permitidos, por ejemplo bulk_send,post_report.",
        )

    def handle(self, *args, **options):
        limit = max(int(options["limit"] or 1), 1)
        loop = bool(options["loop"])
        sleep_seconds = max(float(options["sleep"] or 1), 0.5)
        allowed_types = {
            item.strip()
            for item in str(options.get("types") or "").split(",")
            if item.strip()
        }

        processed = 0
        while True:
            if allowed_types:
                job = (
                    BackgroundJob.objects
                    .filter(state=BackgroundJob.STATE_QUEUED, job_type__in=allowed_types)
                    .order_by("created_at")
                    .first()
                )
                if job:
                    # claim_next_job is generic; when type filtering is active, claim manually.
                    from django.db import transaction
                    from django.utils import timezone

                    with transaction.atomic():
                        job = BackgroundJob.objects.select_for_update(skip_locked=True).filter(
                            pk=job.pk,
                            state=BackgroundJob.STATE_QUEUED,
                        ).first()
                        if job:
                            job.state = BackgroundJob.STATE_RUNNING
                            job.started_at = timezone.now()
                            job.attempts = int(job.attempts or 0) + 1
                            job.error = ""
                            job.save(update_fields=["state", "started_at", "attempts", "error", "updated_at"])
            else:
                job = claim_next_job()

            if not job:
                if not loop:
                    break
                time.sleep(sleep_seconds)
                continue

            self.stdout.write(f"Procesando job {job.pk} ({job.job_type})")
            run_claimed_job(job)
            processed += 1
            if not loop and processed >= limit:
                break

        self.stdout.write(self.style.SUCCESS(f"Background jobs procesados: {processed}"))
