"""Fail-closed worker gate for the TD-02C import-only canary.

A changed PID is evidence to classify, not an error by itself. This module
contains no mutating systemd operation; callers capture snapshots around the
separately approved restart and pass them to ``evaluate_worker_restart``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


EXPECTED_WORKER_COMMAND = "process_background_jobs"


@dataclass(frozen=True)
class WorkerSnapshot:
    main_pid: int
    active_state: str
    sub_state: str
    nrestarts: int
    active_enter_timestamp: str
    exec_main_start_timestamp: str
    command: str
    requires: tuple[str, ...] = ()
    queued_jobs: int = 0
    running_jobs: int = 0
    running_job_ids: tuple[int, ...] = ()
    orphaned_job_ids: tuple[int, ...] = ()
    jobs_total: int = 0
    v2_jobs_total: int = 0
    critical_messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerGateResult:
    allowed: bool
    classification: str
    reasons: tuple[str, ...] = field(default_factory=tuple)
    pid_changed: bool = False


def _healthy_snapshot(snapshot: WorkerSnapshot) -> list[str]:
    reasons: list[str] = []
    if snapshot.active_state != "active":
        reasons.append("worker_not_active")
    if snapshot.main_pid <= 0:
        reasons.append("worker_main_pid_missing")
    if EXPECTED_WORKER_COMMAND not in snapshot.command:
        reasons.append("worker_command_unexpected")
    if snapshot.queued_jobs or snapshot.running_jobs:
        reasons.append("worker_has_unexpected_work")
    if snapshot.running_job_ids:
        reasons.append("worker_has_running_jobs")
    if snapshot.orphaned_job_ids:
        reasons.append("worker_has_orphaned_jobs")
    if snapshot.critical_messages:
        reasons.append("worker_has_critical_messages")
    return reasons


def evaluate_worker_precondition(snapshot: WorkerSnapshot) -> WorkerGateResult:
    """Require an idle, healthy worker before restarting the web service."""
    reasons = _healthy_snapshot(snapshot)
    return WorkerGateResult(
        allowed=not reasons,
        classification="worker_precondition_pass" if not reasons else "worker_precondition_fail",
        reasons=tuple(reasons),
    )


def evaluate_worker_restart(
    before: WorkerSnapshot,
    observations: Iterable[WorkerSnapshot],
    *,
    web_unit: str = "django.service",
) -> WorkerGateResult:
    """Classify an indirect worker restart over a bounded stability window."""
    samples = tuple(observations)
    reasons: list[str] = []
    pre = evaluate_worker_precondition(before)
    if not pre.allowed:
        reasons.extend(f"pre:{reason}" for reason in pre.reasons)
    if web_unit not in before.requires:
        reasons.append(f"dependency_missing:{web_unit}")
    if len(samples) < 2:
        reasons.append("stability_window_insufficient")
    for index, sample in enumerate(samples, start=1):
        reasons.extend(f"sample_{index}:{reason}" for reason in _healthy_snapshot(sample))
        if web_unit not in sample.requires:
            reasons.append(f"sample_{index}:dependency_missing:{web_unit}")
        if sample.jobs_total != before.jobs_total:
            reasons.append(f"sample_{index}:unexpected_job_count_change")
        if sample.v2_jobs_total != before.v2_jobs_total:
            reasons.append(f"sample_{index}:unexpected_v2_job_change")

    pid_changed = bool(samples and samples[-1].main_pid != before.main_pid)
    if samples:
        if len(samples) >= 2 and samples[-1].main_pid != samples[-2].main_pid:
            reasons.append("worker_pid_not_stable")
        restart_values = [before.nrestarts, *(sample.nrestarts for sample in samples)]
        if any(later < earlier for earlier, later in zip(restart_values, restart_values[1:])):
            reasons.append("nrestarts_decreased")
        increases = sum(
            later > earlier for earlier, later in zip(restart_values, restart_values[1:])
        )
        if increases > 1 or (
            len(samples) >= 2 and samples[-1].nrestarts != samples[-2].nrestarts
        ):
            reasons.append("worker_restart_loop")

    if reasons:
        return WorkerGateResult(
            allowed=False,
            classification="worker_restart_unsafe",
            reasons=tuple(dict.fromkeys(reasons)),
            pid_changed=pid_changed,
        )
    return WorkerGateResult(
        allowed=True,
        classification=(
            "worker_restart_expected_and_healthy"
            if pid_changed
            else "worker_remained_healthy"
        ),
        pid_changed=pid_changed,
    )
