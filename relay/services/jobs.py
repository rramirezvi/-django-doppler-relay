from __future__ import annotations

import logging
import traceback
from typing import Callable

from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from relay.models import BackgroundJob
from relay.services.bulk_processing import process_bulk_id


logger = logging.getLogger(__name__)


def run_tracked_job(job_id: int, func: Callable[[], object]) -> None:
    job = BackgroundJob.objects.get(pk=job_id)
    job.state = BackgroundJob.STATE_RUNNING
    job.started_at = timezone.now()
    job.attempts = int(job.attempts or 0) + 1
    job.error = ""
    job.save(update_fields=["state", "started_at", "attempts", "error", "updated_at"])

    try:
        result = func()
    except Exception as exc:
        error = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        logger.exception("BackgroundJob %s failed", job.pk)
        job.state = BackgroundJob.STATE_ERROR
        job.finished_at = timezone.now()
        job.error = error[-8000:]
        job.message = str(exc)[:255]
        job.save(update_fields=["state", "finished_at", "error", "message", "updated_at"])
        return

    job.state = BackgroundJob.STATE_DONE
    job.finished_at = timezone.now()
    if result is not None:
        job.message = str(result)[:255]
    elif not job.message:
        job.message = "Completado"
    job.save(update_fields=["state", "finished_at", "message", "updated_at"])


def dispatch_background_job(job: BackgroundJob) -> object:
    if job.job_type == BackgroundJob.TYPE_BULK_SEND:
        if not job.bulk_id:
            raise ValueError("BackgroundJob bulk_send sin bulk_id")
        process_bulk_id(job.bulk_id)
        return "Envío procesado"

    if job.job_type == BackgroundJob.TYPE_POST_REPORT:
        if not job.bulk_id:
            raise ValueError("BackgroundJob post_report sin bulk_id")
        call_command(
            "process_post_send_reports",
            bulk_id=job.bulk_id,
            force=True,
            verbose_report=True,
        )
        return "Reporte procesado"

    # bulk-v2-real-send-canary (design.md §7/§14, PR2b-T4): one additive
    # elif branch. Function-local import so the existing module-level
    # `process_bulk_id` import above is untouched. This job type MUST only
    # ever reach dispatch through the LOCKED claim path (management
    # command -> select_for_update -> run_claimed_job), never through
    # run_background_job's bypass (design §7) — but the per-recipient
    # compare-and-set inside claim_next_recipient is the actual safety
    # boundary regardless of which path calls in here (design §7's central
    # point, proven by PR2b-T28's same-job-executed-twice test).
    if job.job_type == BackgroundJob.TYPE_BULK_SEND_V2_REAL:
        if not job.bulk_id:
            raise ValueError("BackgroundJob bulk_send_v2_real sin bulk_id")
        from relay.services.bulk_v2_send import process_bulk_id_v2
        return process_bulk_id_v2(job.bulk_id, job_id=job.id)

    raise ValueError(f"Tipo de job no soportado: {job.job_type}")


def run_background_job(job_id: int) -> None:
    run_tracked_job(job_id, lambda: dispatch_background_job(BackgroundJob.objects.get(pk=job_id)))


def claim_next_job() -> BackgroundJob | None:
    with transaction.atomic():
        job = (
            BackgroundJob.objects.select_for_update(skip_locked=True)
            .filter(state=BackgroundJob.STATE_QUEUED)
            .order_by("created_at")
            .first()
        )
        if not job:
            return None
        job.state = BackgroundJob.STATE_RUNNING
        job.started_at = timezone.now()
        job.attempts = int(job.attempts or 0) + 1
        job.error = ""
        job.save(update_fields=["state", "started_at", "attempts", "error", "updated_at"])
        return job


def claim_specific_job(job_id: int) -> BackgroundJob | None:
    """Same CAS shape as `claim_next_job`, scoped to one exact `job_id`
    instead of "the oldest queued job" -- used when a caller (design
    round 9/10/11, PR C2) already knows which job it wants to run
    synchronously, immediately after durably creating it as `queued`.

    Returns None if the row is no longer `queued` (someone else --
    typically the continuous worker, `process_background_jobs --loop`
    -- already claimed it first). The caller MUST treat None as
    "delegated to whoever else claimed it", never re-create a job or
    retry the claim itself.
    """
    with transaction.atomic():
        job = (
            BackgroundJob.objects
            .select_for_update(skip_locked=True)
            .filter(pk=job_id, state=BackgroundJob.STATE_QUEUED)
            .first()
        )
        if job is None:
            return None
        job.state = BackgroundJob.STATE_RUNNING
        job.started_at = timezone.now()
        job.attempts = int(job.attempts or 0) + 1
        job.error = ""
        job.save(update_fields=["state", "started_at", "attempts", "error", "updated_at"])
        return job


def execute_specific_queued_job(job_id: int) -> BackgroundJob | None:
    """Claims exactly one specific queued job and, if this call wins the
    claim, runs it synchronously via `run_claimed_job` -- returning the
    job in its terminal state (`done`/`error`). Returns None if another
    caller already claimed it first (design round 11: "delegated", not a
    failure -- the job WILL be, or already is being, processed by
    whoever else claimed it).

    Does NOT eliminate the gap between the claim's COMMIT and the start
    of `dispatch_background_job` inside `run_claimed_job` -- no
    code-level restructuring can (design round 11's analysis: a database
    commit and a subsequent Python function call belong to different
    failure domains connected by network latency that can never be
    proven to be zero). This function exists to give every caller (the
    management command via `authorize_and_execute_real_send`, and a
    future scheduler) ONE shared, tested claim-then-dispatch sequence
    instead of duplicating it -- not to close that window.
    """
    job = claim_specific_job(job_id)
    if job is None:
        return None
    run_claimed_job(job)
    job.refresh_from_db()
    return job


def run_claimed_job(job: BackgroundJob) -> None:
    try:
        result = dispatch_background_job(job)
    except Exception as exc:
        error = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        logger.exception("BackgroundJob %s failed", job.pk)
        job.state = BackgroundJob.STATE_ERROR
        job.finished_at = timezone.now()
        job.error = error[-8000:]
        job.message = str(exc)[:255]
        job.save(update_fields=["state", "finished_at", "error", "message", "updated_at"])
        return

    job.state = BackgroundJob.STATE_DONE
    job.finished_at = timezone.now()
    job.message = str(result or "Completado")[:255]
    job.save(update_fields=["state", "finished_at", "message", "updated_at"])
