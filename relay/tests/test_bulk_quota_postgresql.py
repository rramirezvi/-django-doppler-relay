"""bulk-v2 quota guard -- PR A genuinely concurrent locking tests
(design round 5/6, points 6/7). PostgreSQL-only.

Mirrors relay/tests/test_bulk_v2_real_send_postgresql.py's pattern
exactly: select_for_update()/FOR UPDATE has no real blocking guarantee on
SQLite, so correctness here rests entirely on real PostgreSQL row locks.
Skipped on this Windows/SQLite dev machine, as expected -- same as
PR2a/PR2b's own PostgreSQL-only test modules.

Real threads, real separate DB connections (close_old_connections() per
worker), no mocking of select_for_update/transaction.atomic() -- these
tests prove actual database locking behavior, not simulated behavior.
"""

from __future__ import annotations

import threading
from datetime import datetime
from datetime import timezone as dt_timezone
from unittest import skipUnless

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings

from relay.models import QuotaWindow
from relay.services.bulk_quota import (
    QuotaExhausted,
    _get_or_create_window_locked,
    _increment_windows,
    _lock_quota_windows,
    _resolve_window_starts,
    _validate_capacity,
)

POSTGRESQL = connection.vendor == "postgresql"

QUOTA_SETTINGS = dict(
    DOPPLER_QUOTA_GUARD_ENABLED=False,
    DOPPLER_QUOTA_MONTHLY_LIMIT=1_000_000,
    DOPPLER_QUOTA_DAILY_LIMIT=1_000_000,
    DOPPLER_QUOTA_HOURLY_LIMIT=1,  # deliberately the contended resource
    DOPPLER_QUOTA_SAFETY_MARGIN_RATIO=0.0,
    DOPPLER_QUOTA_OVERAGE_ENABLED=False,
)


@skipUnless(POSTGRESQL, "Requiere PostgreSQL")
class TwoWorkersRaceForLastUnitTests(TransactionTestCase):
    """#6 -- two workers competing for the last unit of hourly capacity.
    Exactly one must succeed; the other must observe QuotaExhausted, never
    a second increment past the limit."""

    @override_settings(**QUOTA_SETTINGS)
    def test_only_one_worker_wins_the_last_hourly_unit(self):
        # override_settings is process-global, not thread-local: it must
        # wrap the ENTIRE test (both threads' full lifetime) via the method
        # decorator above, never be entered/exited independently inside
        # each worker -- doing the latter is racy (whichever thread exits
        # its own `with override_settings(...)` block first reverts the
        # settings out from under the other, still-running thread).
        now = datetime(2026, 6, 1, 10, 30, 0, tzinfo=dt_timezone.utc)

        results: list[str] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(name: str):
            close_old_connections()
            barrier.wait(timeout=10)
            try:
                starts = _resolve_window_starts(now)
                with transaction.atomic():
                    windows = _lock_quota_windows(starts)
                    _validate_capacity(windows)
                    _increment_windows(windows, now=now)
                outcome = "won"
            except QuotaExhausted:
                outcome = "exhausted"
            finally:
                close_old_connections()
            with results_lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=worker, args=("A",)),
            threading.Thread(target=worker, args=("B",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(results), ["exhausted", "won"])

        hour_window = QuotaWindow.objects.get(window_type=QuotaWindow.WINDOW_HOUR)
        self.assertEqual(hour_window.consumed, 1)
        self.assertLessEqual(hour_window.consumed, hour_window.limit_value)


@skipUnless(POSTGRESQL, "Requiere PostgreSQL")
class ConcurrentFirstWindowCreationTests(TransactionTestCase):
    """#7 -- two workers racing to create the SAME not-yet-existing
    QuotaWindow. The (window_type, window_start) UniqueConstraint must
    turn this into a deterministic outcome: exactly one row, no
    unhandled IntegrityError escaping either worker."""

    def test_concurrent_creation_yields_exactly_one_row(self):
        start = datetime(2026, 7, 1, tzinfo=dt_timezone.utc)

        results: list[object] = []
        errors: list[BaseException] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker():
            close_old_connections()
            barrier.wait(timeout=10)
            try:
                with transaction.atomic():
                    window = _get_or_create_window_locked(
                        QuotaWindow.WINDOW_MONTH, start, 12345
                    )
                with results_lock:
                    results.append(window.pk)
            except BaseException as exc:  # noqa: BLE001 -- must prove NONE escape
                with results_lock:
                    errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(
            QuotaWindow.objects.filter(
                window_type=QuotaWindow.WINDOW_MONTH, window_start=start
            ).count(),
            1,
        )
