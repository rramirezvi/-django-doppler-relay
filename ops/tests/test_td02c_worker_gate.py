from __future__ import annotations

import unittest
from dataclasses import replace

from ops.td02c_worker_gate import (
    WorkerSnapshot,
    evaluate_worker_precondition,
    evaluate_worker_restart,
)


def snapshot(**changes) -> WorkerSnapshot:
    base = WorkerSnapshot(
        main_pid=100,
        active_state="active",
        sub_state="running",
        nrestarts=0,
        active_enter_timestamp="before",
        exec_main_start_timestamp="before",
        command="python manage.py process_background_jobs --loop --sleep 3",
        requires=("django.service",),
        jobs_total=678,
        v2_jobs_total=0,
    )
    return replace(base, **changes)


class Td02cWorkerGateTests(unittest.TestCase):
    def test_changed_pid_healthy_service_passes(self):
        result = evaluate_worker_restart(
            snapshot(), [snapshot(main_pid=200), snapshot(main_pid=200)]
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.classification, "worker_restart_expected_and_healthy")

    def test_changed_pid_and_nrestarts_stabilizes_passes(self):
        result = evaluate_worker_restart(
            snapshot(),
            [snapshot(main_pid=200, nrestarts=1), snapshot(main_pid=200, nrestarts=1)],
        )
        self.assertTrue(result.allowed)

    def test_changed_pid_with_restart_loop_fails(self):
        result = evaluate_worker_restart(
            snapshot(),
            [snapshot(main_pid=200, nrestarts=1), snapshot(main_pid=201, nrestarts=2)],
        )
        self.assertFalse(result.allowed)
        self.assertIn("worker_restart_loop", result.reasons)

    def test_worker_not_active_fails(self):
        result = evaluate_worker_restart(
            snapshot(),
            [snapshot(active_state="failed"), snapshot(active_state="failed")],
        )
        self.assertFalse(result.allowed)
        self.assertIn("sample_1:worker_not_active", result.reasons)

    def test_empty_main_pid_fails(self):
        result = evaluate_worker_restart(
            snapshot(), [snapshot(main_pid=0), snapshot(main_pid=0)]
        )
        self.assertFalse(result.allowed)
        self.assertIn("sample_1:worker_main_pid_missing", result.reasons)

    def test_running_job_before_restart_fails(self):
        result = evaluate_worker_precondition(
            snapshot(running_jobs=1, running_job_ids=(665,))
        )
        self.assertFalse(result.allowed)
        self.assertIn("worker_has_running_jobs", result.reasons)

    def test_orphaned_job_after_restart_fails(self):
        result = evaluate_worker_restart(
            snapshot(),
            [snapshot(main_pid=200, orphaned_job_ids=(665,)), snapshot(main_pid=200)],
        )
        self.assertFalse(result.allowed)
        self.assertIn("sample_1:worker_has_orphaned_jobs", result.reasons)

    def test_worker_creates_unexpected_work_fails(self):
        result = evaluate_worker_restart(
            snapshot(),
            [snapshot(main_pid=200, jobs_total=679), snapshot(main_pid=200, jobs_total=679)],
        )
        self.assertFalse(result.allowed)
        self.assertIn("sample_1:unexpected_job_count_change", result.reasons)

    def test_unchanged_pid_is_also_healthy(self):
        result = evaluate_worker_restart(snapshot(), [snapshot(), snapshot()])
        self.assertTrue(result.allowed)
        self.assertEqual(result.classification, "worker_remained_healthy")

    def test_requires_dependency_is_confirmed(self):
        result = evaluate_worker_restart(
            snapshot(), [snapshot(main_pid=200), snapshot(main_pid=200)]
        )
        self.assertTrue(result.allowed)

    def test_missing_dependency_has_explicit_diagnostic(self):
        before = snapshot(requires=())
        result = evaluate_worker_restart(
            before,
            [snapshot(main_pid=200, requires=()), snapshot(main_pid=200, requires=())],
        )
        self.assertFalse(result.allowed)
        self.assertIn("dependency_missing:django.service", result.reasons)

    def test_changed_command_fails(self):
        bad = snapshot(main_pid=200, command="python unexpected.py")
        result = evaluate_worker_restart(snapshot(), [bad, bad])
        self.assertFalse(result.allowed)
        self.assertIn("sample_1:worker_command_unexpected", result.reasons)

    def test_critical_journal_message_fails(self):
        bad = snapshot(main_pid=200, critical_messages=("database unavailable",))
        result = evaluate_worker_restart(snapshot(), [bad, bad])
        self.assertFalse(result.allowed)
        self.assertIn("sample_1:worker_has_critical_messages", result.reasons)
