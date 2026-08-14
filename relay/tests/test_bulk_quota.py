"""bulk-v2 quota guard -- PR A infrastructure tests (design round 5/6).

Portable (SQLite-compatible) tests only: model constraints, window-boundary
math, config resolution, and the all-or-nothing composition proven via
real transaction rollback (rollback is standard SQL, not Postgres-
specific). Genuinely concurrent locking races
(select_for_update/FOR UPDATE blocking behavior) require real PostgreSQL
and live in test_bulk_quota_postgresql.py, per the existing repo
convention (see test_bulk_v2_real_send_postgresql.py's module docstring).

PR A is dormant infrastructure: nothing here touches BulkSendRecipient,
BackgroundJob, BulkSend, or any Doppler transport.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime
from datetime import timezone as dt_timezone

from django.db import IntegrityError, transaction
from django.db.models import F
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from relay.models import QuotaWindow
from relay.services import bulk_quota
from relay.services.bulk_quota import (
    QuotaConfigurationError,
    QuotaExhausted,
    _get_or_create_window_locked,
    _increment_windows,
    _lock_quota_windows,
    _resolve_effective_limit,
    _resolve_window_starts,
    _validate_capacity,
)

def _executable_source(module) -> str:
    """Module source with its own top-level docstring stripped, so
    structural assertions grep real code, not the prose that documents
    what the code deliberately does NOT do."""
    source = inspect.getsource(module)
    tree = ast.parse(source)
    docstring = ast.get_docstring(tree) or ""
    return source.replace(docstring, "", 1)


QUOTA_SETTINGS = dict(
    DOPPLER_QUOTA_GUARD_ENABLED=False,
    DOPPLER_QUOTA_MONTHLY_LIMIT=1000,
    DOPPLER_QUOTA_DAILY_LIMIT=100,
    DOPPLER_QUOTA_HOURLY_LIMIT=10,
    DOPPLER_QUOTA_SAFETY_MARGIN_RATIO=0.0,
    DOPPLER_QUOTA_OVERAGE_ENABLED=False,
)


# --- UTC window boundaries (#21, #22, #23, #24) -----------------------------

class WindowStartsTests(TestCase):
    def test_hour_boundary(self):
        before = datetime(2026, 3, 5, 13, 59, 59, tzinfo=dt_timezone.utc)
        after = datetime(2026, 3, 5, 14, 0, 0, tzinfo=dt_timezone.utc)
        self.assertNotEqual(
            _resolve_window_starts(before)[QuotaWindow.WINDOW_HOUR],
            _resolve_window_starts(after)[QuotaWindow.WINDOW_HOUR],
        )

    def test_day_boundary(self):
        before = datetime(2026, 3, 5, 23, 59, 59, tzinfo=dt_timezone.utc)
        after = datetime(2026, 3, 6, 0, 0, 0, tzinfo=dt_timezone.utc)
        self.assertNotEqual(
            _resolve_window_starts(before)[QuotaWindow.WINDOW_DAY],
            _resolve_window_starts(after)[QuotaWindow.WINDOW_DAY],
        )

    def test_month_boundary_short_february(self):
        before = datetime(2026, 2, 28, 23, 59, 59, tzinfo=dt_timezone.utc)
        after = datetime(2026, 3, 1, 0, 0, 0, tzinfo=dt_timezone.utc)
        starts_before = _resolve_window_starts(before)
        starts_after = _resolve_window_starts(after)
        self.assertNotEqual(
            starts_before[QuotaWindow.WINDOW_MONTH], starts_after[QuotaWindow.WINDOW_MONTH]
        )
        self.assertEqual(starts_after[QuotaWindow.WINDOW_MONTH].day, 1)

    def test_dst_irrelevant_result_independent_of_settings_time_zone(self):
        now = datetime(2026, 3, 8, 10, 30, 0, tzinfo=dt_timezone.utc)  # US DST transition week
        with override_settings(TIME_ZONE="America/New_York", USE_TZ=True):
            result_ny = _resolve_window_starts(now)
        with override_settings(TIME_ZONE="UTC", USE_TZ=True):
            result_utc = _resolve_window_starts(now)
        self.assertEqual(result_ny, result_utc)

    def test_module_never_reads_settings_time_zone(self):
        # Executable-code check: strip the module's own docstring (which
        # documents, in prose, that TIME_ZONE is deliberately never read)
        # before grepping for a real code reference, per the existing
        # convention (test_bulk_v2_real_send_command.py's
        # test_command_module_has_no_csv_or_bulk_import_reference).
        executable_source = _executable_source(bulk_quota)
        self.assertNotIn("TIME_ZONE", executable_source)


# --- effective limit resolution / fail-closed config (#25, #26) ------------

class ResolveEffectiveLimitTests(TestCase):
    @override_settings(**QUOTA_SETTINGS)
    def test_no_margin_returns_configured_limit(self):
        self.assertEqual(_resolve_effective_limit(QuotaWindow.WINDOW_MONTH), 1000)

    @override_settings(**{**QUOTA_SETTINGS, "DOPPLER_QUOTA_SAFETY_MARGIN_RATIO": 0.1})
    def test_safety_margin_applied_and_floored(self):
        self.assertEqual(_resolve_effective_limit(QuotaWindow.WINDOW_MONTH), 900)

    @override_settings(**{**QUOTA_SETTINGS, "DOPPLER_QUOTA_MONTHLY_LIMIT": 0})
    def test_zero_limit_fails_closed(self):
        with self.assertRaises(QuotaConfigurationError):
            _resolve_effective_limit(QuotaWindow.WINDOW_MONTH)

    @override_settings(**{**QUOTA_SETTINGS, "DOPPLER_QUOTA_MONTHLY_LIMIT": -5})
    def test_negative_limit_fails_closed(self):
        with self.assertRaises(QuotaConfigurationError):
            _resolve_effective_limit(QuotaWindow.WINDOW_MONTH)

    @override_settings(**{**QUOTA_SETTINGS, "DOPPLER_QUOTA_OVERAGE_ENABLED": True})
    def test_overage_enabled_fails_closed(self):
        with self.assertRaises(QuotaConfigurationError):
            _resolve_effective_limit(QuotaWindow.WINDOW_MONTH)

    @override_settings(**{**QUOTA_SETTINGS, "DOPPLER_QUOTA_SAFETY_MARGIN_RATIO": 1.0})
    def test_margin_of_one_fails_closed(self):
        with self.assertRaises(QuotaConfigurationError):
            _resolve_effective_limit(QuotaWindow.WINDOW_MONTH)

    @override_settings(**{**QUOTA_SETTINGS, "DOPPLER_QUOTA_SAFETY_MARGIN_RATIO": -0.1})
    def test_negative_margin_fails_closed(self):
        with self.assertRaises(QuotaConfigurationError):
            _resolve_effective_limit(QuotaWindow.WINDOW_MONTH)

    @override_settings(
        **{**QUOTA_SETTINGS, "DOPPLER_QUOTA_HOURLY_LIMIT": 1, "DOPPLER_QUOTA_SAFETY_MARGIN_RATIO": 0.9999}
    )
    def test_margin_collapsing_limit_to_zero_fails_closed(self):
        with self.assertRaises(QuotaConfigurationError):
            _resolve_effective_limit(QuotaWindow.WINDOW_HOUR)


# --- window creation / limit_value immutability (design round 6, point 5) --

class GetOrCreateWindowLockedTests(TestCase):
    def test_creates_with_resolved_limit(self):
        start = datetime(2026, 6, 1, tzinfo=dt_timezone.utc)
        with transaction.atomic():
            window = _get_or_create_window_locked(QuotaWindow.WINDOW_MONTH, start, 500)
        self.assertEqual(window.limit_value, 500)
        self.assertEqual(window.consumed, 0)

    def test_returns_existing_without_creating_duplicate_and_limit_is_immutable(self):
        start = datetime(2026, 6, 1, tzinfo=dt_timezone.utc)
        with transaction.atomic():
            first = _get_or_create_window_locked(QuotaWindow.WINDOW_MONTH, start, 500)
        with transaction.atomic():
            # A different limit_value_if_created is passed on purpose: since
            # the row already exists, it MUST be ignored -- proves
            # limit_value is fixed at creation time and never rewritten.
            second = _get_or_create_window_locked(QuotaWindow.WINDOW_MONTH, start, 999)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.limit_value, 500)
        self.assertEqual(
            QuotaWindow.objects.filter(
                window_type=QuotaWindow.WINDOW_MONTH, window_start=start
            ).count(),
            1,
        )


# --- capacity validation: monthly/daily/hourly exhausted (#1, #2, #3, #4) --

class ValidateCapacityTests(TestCase):
    def _windows(self, *, month_consumed=5, day_consumed=5, hour_consumed=5):
        start = datetime(2026, 6, 1, tzinfo=dt_timezone.utc)
        return {
            QuotaWindow.WINDOW_MONTH: QuotaWindow.objects.create(
                window_type=QuotaWindow.WINDOW_MONTH, window_start=start,
                limit_value=1000, consumed=month_consumed,
            ),
            QuotaWindow.WINDOW_DAY: QuotaWindow.objects.create(
                window_type=QuotaWindow.WINDOW_DAY, window_start=start,
                limit_value=100, consumed=day_consumed,
            ),
            QuotaWindow.WINDOW_HOUR: QuotaWindow.objects.create(
                window_type=QuotaWindow.WINDOW_HOUR, window_start=start,
                limit_value=10, consumed=hour_consumed,
            ),
        }

    def test_none_exhausted_passes(self):
        _validate_capacity(self._windows())  # must not raise

    def test_monthly_exhausted_raises(self):
        windows = self._windows(month_consumed=1000)
        with self.assertRaises(QuotaExhausted) as cm:
            _validate_capacity(windows)
        self.assertEqual(cm.exception.window_type, QuotaWindow.WINDOW_MONTH)

    def test_daily_exhausted_raises(self):
        windows = self._windows(day_consumed=100)
        with self.assertRaises(QuotaExhausted) as cm:
            _validate_capacity(windows)
        self.assertEqual(cm.exception.window_type, QuotaWindow.WINDOW_DAY)

    def test_hourly_exhausted_raises(self):
        windows = self._windows(hour_consumed=10)
        with self.assertRaises(QuotaExhausted) as cm:
            _validate_capacity(windows)
        self.assertEqual(cm.exception.window_type, QuotaWindow.WINDOW_HOUR)


# --- all-or-nothing composition + real rollback (#5) ------------------------

class AllOrNothingTests(TransactionTestCase):
    def _windows(self):
        start = datetime(2026, 6, 1, tzinfo=dt_timezone.utc)
        return {
            QuotaWindow.WINDOW_MONTH: QuotaWindow.objects.create(
                window_type=QuotaWindow.WINDOW_MONTH, window_start=start,
                limit_value=1000, consumed=0,
            ),
            QuotaWindow.WINDOW_DAY: QuotaWindow.objects.create(
                window_type=QuotaWindow.WINDOW_DAY, window_start=start,
                limit_value=100, consumed=0,
            ),
            QuotaWindow.WINDOW_HOUR: QuotaWindow.objects.create(
                window_type=QuotaWindow.WINDOW_HOUR, window_start=start,
                limit_value=10, consumed=0,
            ),
        }

    def test_increment_all_three(self):
        windows = self._windows()
        now = timezone.now()
        with transaction.atomic():
            _increment_windows(windows, now=now)
        for window in windows.values():
            window.refresh_from_db()
            self.assertEqual(window.consumed, 1)

    def test_rollback_reverts_every_increment_even_if_some_already_ran(self):
        """Proves genuine DB-level atomicity, not just that the calling
        code happens to avoid partial increments by ordering: two of the
        three windows are incremented, then the transaction is aborted --
        the DB must revert ALL of them, including the ones that already
        executed their UPDATE."""
        windows = self._windows()
        now = timezone.now()

        class _Boom(Exception):
            pass

        try:
            with transaction.atomic():
                QuotaWindow.objects.filter(pk=windows[QuotaWindow.WINDOW_MONTH].pk).update(
                    consumed=F("consumed") + 1, updated_at=now
                )
                QuotaWindow.objects.filter(pk=windows[QuotaWindow.WINDOW_DAY].pk).update(
                    consumed=F("consumed") + 1, updated_at=now
                )
                raise _Boom("simulated failure before the HOUR increment")
        except _Boom:
            pass

        for window in windows.values():
            window.refresh_from_db()
            self.assertEqual(window.consumed, 0)

    def test_exhausted_window_blocks_before_any_increment(self):
        """End-to-end composition, as PR B will use these primitives:
        lock -> validate -> increment inside one transaction. When
        validate raises, increment must never run and nothing changes."""
        now = timezone.now()
        with override_settings(**{**QUOTA_SETTINGS, "DOPPLER_QUOTA_HOURLY_LIMIT": 1}):
            starts = _resolve_window_starts(now)
            with transaction.atomic():
                windows = _lock_quota_windows(starts)
            QuotaWindow.objects.filter(pk=windows[QuotaWindow.WINDOW_HOUR].pk).update(consumed=1)

            month_before = QuotaWindow.objects.get(pk=windows[QuotaWindow.WINDOW_MONTH].pk).consumed
            day_before = QuotaWindow.objects.get(pk=windows[QuotaWindow.WINDOW_DAY].pk).consumed

            with self.assertRaises(QuotaExhausted):
                with transaction.atomic():
                    windows2 = _lock_quota_windows(starts)
                    _validate_capacity(windows2)
                    _increment_windows(windows2, now=now)

            self.assertEqual(
                QuotaWindow.objects.get(pk=windows[QuotaWindow.WINDOW_MONTH].pk).consumed,
                month_before,
            )
            self.assertEqual(
                QuotaWindow.objects.get(pk=windows[QuotaWindow.WINDOW_DAY].pk).consumed, day_before
            )
            self.assertEqual(
                QuotaWindow.objects.get(pk=windows[QuotaWindow.WINDOW_HOUR].pk).consumed, 1
            )


# --- model constraints, evidenced directly at the DB level -----------------

class ConstraintTests(TestCase):
    def test_unique_constraint_rejects_duplicate_window(self):
        start = datetime(2026, 6, 1, tzinfo=dt_timezone.utc)
        QuotaWindow.objects.create(
            window_type=QuotaWindow.WINDOW_MONTH, window_start=start, limit_value=100, consumed=0
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                QuotaWindow.objects.create(
                    window_type=QuotaWindow.WINDOW_MONTH, window_start=start,
                    limit_value=999, consumed=0,
                )

    def test_consumed_lte_limit_check_constraint(self):
        start = datetime(2026, 6, 1, tzinfo=dt_timezone.utc)
        window = QuotaWindow.objects.create(
            window_type=QuotaWindow.WINDOW_MONTH, window_start=start, limit_value=10, consumed=5
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                QuotaWindow.objects.filter(pk=window.pk).update(consumed=11)

    def test_window_type_check_constraint_rejects_invalid_value(self):
        start = datetime(2026, 6, 1, tzinfo=dt_timezone.utc)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                QuotaWindow.objects.create(
                    window_type="year", window_start=start, limit_value=10, consumed=0
                )


# --- isolation: no Doppler/HTTP/send-path dependency, no public wrapper ----

class IsolationTests(TestCase):
    def test_no_forbidden_imports_in_bulk_quota_module(self):
        # fix-bulk-v2-quota-integration (PR B): AST-based, checking actual
        # `import`/`from ... import` statements only -- not a blind
        # substring search. reserve_and_claim's own docstring legitimately
        # names relay/services/bulk_v2_send.py in prose (to document who
        # wires it in), which a substring check would misfire on; a real
        # import of any of these modules would still be caught here.
        tree = ast.parse(inspect.getsource(bulk_quota))
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

        forbidden_modules = (
            "relay.services.doppler_relay",
            "relay.services.bulk_v2_send",
            "relay.services.bulk_v2_send_state",
            "relay.services.jobs",
            "requests",
        )
        for forbidden in forbidden_modules:
            self.assertFalse(
                any(m == forbidden or m.startswith(forbidden + ".") for m in imported_modules),
                f"unexpected import of {forbidden!r}: {imported_modules}",
            )
        self.assertFalse(any(m.startswith("relay.management.commands") for m in imported_modules))

    def test_no_bypassable_reservation_wrapper_exists(self):
        # fix-bulk-v2-quota-integration (PR B): reserve_and_claim is now
        # the ONE sanctioned public entry point (design round 6/7) --
        # PR A's original "nothing public yet" assertion is superseded by
        # design, not broken by accident. What must still never exist is
        # a wrapper that lets a caller reserve quota WITHOUT the recipient
        # claim fused into the same transaction.
        self.assertTrue(hasattr(bulk_quota, "reserve_and_claim"))
        self.assertFalse(hasattr(bulk_quota, "reserve_quota"))
        self.assertFalse(hasattr(bulk_quota, "reserve_quota_only"))
        self.assertFalse(hasattr(bulk_quota, "reserve_quota_only"))
