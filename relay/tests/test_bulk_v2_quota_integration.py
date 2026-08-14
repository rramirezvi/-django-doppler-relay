"""bulk-v2 quota integration (PR B, design round 6/7): template gate ->
atomic quota+recipient claim -> single-attempt Doppler -> state machine.

Portable (SQLite-compatible) tests. Genuinely concurrent PostgreSQL races
live in test_bulk_v2_quota_integration_postgresql.py, per the existing
repo convention (see test_bulk_quota_postgresql.py's module docstring).
"""

from __future__ import annotations

import uuid
from unittest import mock

import requests
from django.test import override_settings

from relay.models import BulkSendRecipient, QuotaWindow
from relay.services.bulk_quota import _resolve_window_starts, reserve_and_claim
from relay.services.bulk_v2_send import (
    RealSendTemplateDiscoveryFailed,
    RealSendTemplateVariablesMissing,
    process_bulk_id_v2,
)
from relay.tests._bulk_v2_real_send_support import (
    FakeDopplerResponse,
    NoRealDopplerCallTestCase,
    RealSendFixtureMixin,
)

# _build_recipients_model falls back to DOPPLER_RELAY's
# DEFAULT_FROM_EMAIL/DEFAULT_FROM_NAME (see that function's docstring);
# the real .env-driven test environment leaves these empty, which would
# make send_template_message raise ValueError("from_email es requerido")
# BEFORE the mocked transport is ever reached -- unrelated to quota, but
# it would still misclassify every "positive" test as send_failed/
# payload_invalid. Every override dict below supplies a minimal, complete
# DOPPLER_RELAY so that never happens (same fallback RealSendFixtureMixin.
# authorized_settings applies for the existing PR2b test suite).
_DOPPLER_RELAY_TEST_CFG = dict(
    API_KEY="test-key",
    ACCOUNT_ID=1,
    AUTH_SCHEME="Bearer",
    BASE_URL="https://api.dopplerrelay.com/",
    DEFAULT_FROM_EMAIL="noreply@example.com",
    DEFAULT_FROM_NAME="Quota Test",
    TIMEOUT=30,
)

QUOTA_ON = dict(
    DOPPLER_QUOTA_GUARD_ENABLED=True,
    DOPPLER_QUOTA_MONTHLY_LIMIT=1000,
    DOPPLER_QUOTA_DAILY_LIMIT=100,
    DOPPLER_QUOTA_HOURLY_LIMIT=10,
    DOPPLER_QUOTA_SAFETY_MARGIN_RATIO=0.0,
    DOPPLER_QUOTA_OVERAGE_ENABLED=False,
    DOPPLER_RELAY=_DOPPLER_RELAY_TEST_CFG,
)
QUOTA_OFF = dict(DOPPLER_QUOTA_GUARD_ENABLED=False, DOPPLER_RELAY=_DOPPLER_RELAY_TEST_CFG)


def _seed_exhausted_window(window_type: str, *, limit: int = 5):
    from django.utils import timezone as django_timezone

    starts = _resolve_window_starts(django_timezone.now())
    return QuotaWindow.objects.create(
        window_type=window_type, window_start=starts[window_type],
        limit_value=limit, consumed=limit,
    )


# --- Flag OFF: byte-identical to pre-PR-B behavior --------------------------

class FlagOffTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    @override_settings(**QUOTA_OFF)
    def test_quota_guard_off_zero_quotawindow_activity(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse())
        process_bulk_id_v2(bulk.pk, job_id=1)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENT)
        self.assertEqual(QuotaWindow.objects.count(), 0)

    @override_settings(**QUOTA_OFF)
    def test_quota_guard_off_positive_flow_still_sent(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse())
        result = process_bulk_id_v2(bulk.pk, job_id=1)
        self.assertIn("1 fila", result)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENT)
        self.assertEqual(self._send_mock.call_count, 1)

    @override_settings(**QUOTA_OFF)
    def test_quota_guard_off_never_calls_reserve_and_claim(self):
        bulk = self.make_bulk()
        self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse())
        with mock.patch(
            "relay.services.bulk_v2_send.reserve_and_claim",
            side_effect=AssertionError(
                "reserve_and_claim must not be called when the flag is OFF"
            ),
        ):
            process_bulk_id_v2(bulk.pk, job_id=1)


# --- Flag ON: positive flow, exact deltas -----------------------------------

class FlagOnPositiveTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    @override_settings(**QUOTA_ON)
    def test_quota_available_increments_month_day_hour_by_exactly_one(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse())
        process_bulk_id_v2(bulk.pk, job_id=1)

        windows = {w.window_type: w for w in QuotaWindow.objects.all()}
        self.assertEqual(set(windows), {"month", "day", "hour"})
        for window in windows.values():
            self.assertEqual(window.consumed, 1)

        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENT)


# --- Flag ON: quota conserved regardless of transport outcome --------------

class FlagOnOutcomeConservesQuotaTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    @override_settings(**QUOTA_ON)
    def test_sent_conserves_quota(self):
        bulk = self.make_bulk()
        self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse())
        process_bulk_id_v2(bulk.pk, job_id=1)
        self.assertEqual(QuotaWindow.objects.get(window_type="hour").consumed, 1)

    @override_settings(**QUOTA_ON)
    def test_send_failed_conserves_quota(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse(
            status_code=400, json_data={"title": "Bad Request"}
        ))
        process_bulk_id_v2(bulk.pk, job_id=1)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_FAILED)
        self.assertEqual(QuotaWindow.objects.get(window_type="hour").consumed, 1)

    @override_settings(**QUOTA_ON)
    def test_ambiguous_conserves_quota(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse(
            status_code=500, json_data={"title": "Internal Error"}
        ))
        process_bulk_id_v2(bulk.pk, job_id=1)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_AMBIGUOUS)
        self.assertEqual(QuotaWindow.objects.get(window_type="hour").consumed, 1)

    @override_settings(**QUOTA_ON)
    def test_timeout_conserves_quota(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        self.mock_transport(side_effect=requests.Timeout("simulated timeout"))
        process_bulk_id_v2(bulk.pk, job_id=1)
        row.refresh_from_db()
        # Absorbed via the pre-existing V1 defect (single-attempt client +
        # requests.Timeout -> AttributeError -> dispatch_exception), same
        # as PR2b's own crash-scenario tests -- the exact error_code is
        # not this module's concern, only that the row lands in a
        # terminal-but-not-sent state and quota is never reverted.
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_AMBIGUOUS)
        self.assertEqual(QuotaWindow.objects.get(window_type="hour").consumed, 1)

    @override_settings(**QUOTA_ON)
    def test_429_conserves_quota(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse(
            status_code=429, json_data={"title": "Too Many Requests"}
        ))
        process_bulk_id_v2(bulk.pk, job_id=1)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_AMBIGUOUS)
        self.assertEqual(row.send_error_code, "doppler_http_429")
        self.assertEqual(QuotaWindow.objects.get(window_type="hour").consumed, 1)


# --- Exhaustion --------------------------------------------------------------

class ExhaustionTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    @override_settings(**QUOTA_ON)
    def test_month_exhausted_zero_claim_zero_post(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        _seed_exhausted_window(QuotaWindow.WINDOW_MONTH)
        process_bulk_id_v2(bulk.pk, job_id=1)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)
        self._send_mock.assert_not_called()

    @override_settings(**QUOTA_ON)
    def test_day_exhausted_zero_claim_zero_post(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        _seed_exhausted_window(QuotaWindow.WINDOW_DAY)
        process_bulk_id_v2(bulk.pk, job_id=1)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)
        self._send_mock.assert_not_called()

    @override_settings(**QUOTA_ON)
    def test_hour_exhausted_zero_claim_zero_post(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        _seed_exhausted_window(QuotaWindow.WINDOW_HOUR)
        process_bulk_id_v2(bulk.pk, job_id=1)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)
        self._send_mock.assert_not_called()

    @override_settings(**{**QUOTA_ON, "DOPPLER_QUOTA_HOURLY_LIMIT": 1})
    def test_last_unit_available_claims_exactly_one_recipient(self):
        bulk = self.make_bulk()
        row1 = self.make_occurrence(
            bulk, source_row_number=1, idempotency_key=uuid.uuid4()
        )
        row2 = self.make_occurrence(
            bulk, source_row_number=2, recipient="b@example.com",
            normalized_recipient="b@example.com", idempotency_key=uuid.uuid4(),
        )
        self.mock_transport(return_value=FakeDopplerResponse())
        process_bulk_id_v2(bulk.pk, job_id=1)

        row1.refresh_from_db()
        row2.refresh_from_db()
        statuses = sorted([row1.send_status, row2.send_status])
        self.assertEqual(
            statuses,
            sorted([BulkSendRecipient.SEND_SENT, BulkSendRecipient.SEND_NOT_STARTED]),
        )
        self.assertEqual(QuotaWindow.objects.get(window_type="hour").consumed, 1)


# --- Gates: template checks always run before any quota touch --------------

class GateTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    @override_settings(**QUOTA_ON)
    def test_template_variables_missing_zero_quota_delta(self):
        bulk = self.make_bulk()
        self.make_occurrence(bulk, payload={"other": "x"})
        with self.assertRaises(RealSendTemplateVariablesMissing):
            process_bulk_id_v2(bulk.pk, job_id=1)
        self.assertEqual(QuotaWindow.objects.count(), 0)
        self._send_mock.assert_not_called()

    @override_settings(**QUOTA_ON)
    def test_template_discovery_failed_zero_quota_delta(self):
        bulk = self.make_bulk()
        self.make_occurrence(bulk)
        self.mock_template_transport(return_value=FakeDopplerResponse(
            status_code=200,
            json_data={"id": "tpl-real", "name": "Template real", "subject": "Asunto", "bodyType": "rawHtml"},
        ))
        with self.assertRaises(RealSendTemplateDiscoveryFailed):
            process_bulk_id_v2(bulk.pk, job_id=1)
        self.assertEqual(QuotaWindow.objects.count(), 0)
        self._send_mock.assert_not_called()

    @override_settings(**QUOTA_ON)
    def test_no_eligible_recipients_zero_quota_delta(self):
        bulk = self.make_bulk()
        process_bulk_id_v2(bulk.pk, job_id=1)
        self.assertEqual(QuotaWindow.objects.count(), 0)
        self._transport_mock.assert_not_called()


# --- Crash / recovery --------------------------------------------------------

class CrashRecoveryTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    @override_settings(**QUOTA_ON)
    def test_crash_before_commit_leaves_quota_and_recipient_untouched(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)

        class _Boom(Exception):
            pass

        with mock.patch(
            "relay.services.bulk_quota._increment_windows",
            side_effect=_Boom("simulated crash before commit"),
        ):
            with self.assertRaises(_Boom):
                reserve_and_claim(bulk.pk, job_id=1)

        self.assertEqual(QuotaWindow.objects.count(), 0)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)
        self._transport_mock.assert_not_called()

    @override_settings(**QUOTA_ON)
    def test_crash_after_commit_before_post_leaves_quota_consumed_and_recipient_sending(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)

        claimed_pk = reserve_and_claim(bulk.pk, job_id=1)

        self.assertEqual(claimed_pk, row.pk)
        self._transport_mock.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENDING)
        for window in QuotaWindow.objects.all():
            self.assertEqual(window.consumed, 1)

    @override_settings(**QUOTA_ON)
    def test_reinvocation_does_not_reserve_again_for_the_same_sending_row(self):
        bulk = self.make_bulk()
        self.make_occurrence(bulk)

        first = reserve_and_claim(bulk.pk, job_id=1)
        second = reserve_and_claim(bulk.pk, job_id=2)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        for window in QuotaWindow.objects.all():
            self.assertEqual(window.consumed, 1)
