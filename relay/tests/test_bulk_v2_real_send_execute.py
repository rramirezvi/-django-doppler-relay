"""bulk-v2 real-send authorization/execution boundary tests (PR C, design
round 8): relay.services.bulk_v2_real_send_execute.authorize_and_execute_real_send.

Every test that can reach the send path inherits NoRealDopplerCallTestCase
(PR2b-T35) and configures its own transport mock explicitly, exactly like
the existing PR2b command test suite.
"""

from __future__ import annotations

from unittest import mock

from django.core.management import CommandError, call_command
from django.test import override_settings

from relay.models import BackgroundJob, BulkSendRecipient, QuotaWindow
from relay.services.bulk_quota import _resolve_window_starts
from relay.services.bulk_v2_real_send_execute import authorize_and_execute_real_send
from relay.services.bulk_v2_send_state import claim_next_recipient
from relay.services.jobs import run_background_job
from relay.tests._bulk_v2_real_send_support import (
    FakeDopplerResponse,
    NoRealDopplerCallTestCase,
    RealSendFixtureMixin,
)


class CommandDelegationTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    def test_command_delegates_entirely_to_the_service(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        with mock.patch(
            "relay.management.commands.bulk_v2_real_send.authorize_and_execute_real_send"
        ) as mocked:
            from relay.services.bulk_v2_real_send_execute import RealSendOutcome

            mocked.return_value = RealSendOutcome(
                executed=False, code="real_send_disabled",
                message="El envio real V2 no esta habilitado.", returncode=5,
                bulk_send_id=bulk.pk, eligible_rows=0, max_rows=1, dry_run=False,
            )
            with self.assertRaises(CommandError):
                call_command("bulk_v2_real_send", bulk_send_id=bulk.pk)
        mocked.assert_called_once_with(bulk.pk, dry_run=False)


class AuthorizationRefusalsTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    def test_disabled_zero_job(self):
        bulk = self.make_bulk()
        self.make_occurrence(bulk)
        outcome = authorize_and_execute_real_send(bulk.pk)
        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.code, "real_send_disabled")
        self.assertEqual(BackgroundJob.objects.count(), 0)
        self._transport_mock.assert_not_called()

    def test_request_id_not_allowed_zero_job(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        self.make_occurrence(bulk)
        settings_kwargs = dict(
            self.authorized_settings(user=user, bulk=bulk),
            BULK_PROCESSING_V2_REAL_SEND_REQUEST_IDS="other-request",
        )
        with override_settings(**settings_kwargs):
            outcome = authorize_and_execute_real_send(bulk.pk)
        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.code, "real_send_request_not_allowed")
        self.assertEqual(BackgroundJob.objects.count(), 0)

    def test_template_not_allowed_zero_job(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        self.make_occurrence(bulk)
        settings_kwargs = dict(
            self.authorized_settings(user=user, bulk=bulk),
            BULK_PROCESSING_V2_REAL_SEND_TEMPLATE_IDS="other-template",
        )
        with override_settings(**settings_kwargs):
            outcome = authorize_and_execute_real_send(bulk.pk)
        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.code, "real_send_template_not_allowed")
        self.assertEqual(BackgroundJob.objects.count(), 0)

    def test_domain_not_allowed_zero_job(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        self.make_occurrence(bulk)
        settings_kwargs = dict(
            self.authorized_settings(user=user, bulk=bulk),
            BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS="evil.example",
        )
        with override_settings(**settings_kwargs):
            outcome = authorize_and_execute_real_send(bulk.pk)
        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.code, "real_send_domain_not_allowed")
        self.assertEqual(BackgroundJob.objects.count(), 0)

    def test_ambiguous_present_aborts(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk)
        claim_next_recipient(bulk.pk, job_id=1)
        from relay.services.bulk_v2_send_state import mark_ambiguous
        mark_ambiguous(row.pk, error_code="timeout", error_message="boom")

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            outcome = authorize_and_execute_real_send(bulk.pk)
        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.code, "real_send_ambiguous_present")
        self.assertEqual(len(outcome.blocked_ambiguous_rows), 1)
        self.assertEqual(BackgroundJob.objects.count(), 0)

    def test_stale_sending_aborts(self):
        from datetime import timedelta

        from django.utils import timezone

        from relay.services.bulk_v2_send_state import STALE_SENDING_AFTER

        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk)
        claim_next_recipient(bulk.pk, job_id=1)
        stale_at = timezone.now() - STALE_SENDING_AFTER - timedelta(seconds=1)
        BulkSendRecipient.objects.filter(pk=row.pk).update(send_started_at=stale_at)

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            outcome = authorize_and_execute_real_send(bulk.pk)
        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.code, "real_send_stale_sending_present")
        self.assertEqual(BackgroundJob.objects.count(), 0)

    def test_allowed_plus_dry_run_zero_job_zero_transport(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        self.make_occurrence(bulk)
        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            outcome = authorize_and_execute_real_send(bulk.pk, dry_run=True)
        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.code, "real_send_dry_run")
        self.assertEqual(outcome.returncode, 0)
        self.assertEqual(BackgroundJob.objects.count(), 0)
        self._transport_mock.assert_not_called()


class AllowedExecutionTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    def test_allowed_real_path_creates_exactly_one_job(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse())
        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            outcome = authorize_and_execute_real_send(bulk.pk)
        self.assertTrue(outcome.executed)
        self.assertEqual(BackgroundJob.objects.count(), 1)
        self.assertEqual(outcome.job_id, BackgroundJob.objects.get().pk)

    def test_happy_path_job_ends_done(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse())
        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            outcome = authorize_and_execute_real_send(bulk.pk)
        self.assertEqual(outcome.job_state, BackgroundJob.STATE_DONE)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENT)

    def test_template_gate_failure_job_ends_error(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        self.make_occurrence(bulk)
        self.mock_template_transport(return_value=FakeDopplerResponse(
            status_code=200,
            json_data={"id": "tpl-real", "name": "Template real", "subject": "Asunto", "bodyType": "rawHtml"},
        ))
        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            outcome = authorize_and_execute_real_send(bulk.pk)
        self.assertTrue(outcome.executed)
        self.assertEqual(outcome.job_state, BackgroundJob.STATE_ERROR)
        self._send_mock.assert_not_called()

    def test_double_invocation_does_not_create_a_second_effective_job(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse())
        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            first = authorize_and_execute_real_send(bulk.pk)
            second = authorize_and_execute_real_send(bulk.pk)
        self.assertTrue(first.executed)
        self.assertFalse(second.executed)
        self.assertEqual(second.code, "real_send_nothing_to_send")
        self.assertEqual(BackgroundJob.objects.count(), 1)
        self.assertEqual(self._send_mock.call_count, 1)

    def test_run_background_job_bypass_still_protected_by_recipient_cas(self):
        """PR2b-T28's invariant, reconfirmed after the PR C extraction:
        job-level bypass (calling run_background_job(job_id) directly,
        skipping claim_next_job's own lock) is safe only because the
        per-recipient compare-and-set is the real boundary -- unaffected
        by moving the authorization/dispatch logic into a service."""
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse())
        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            outcome = authorize_and_execute_real_send(bulk.pk)
            self.assertTrue(outcome.executed)
            # Bypass: re-run the exact same already-dispatched job again.
            run_background_job(outcome.job_id)
        self.assertLessEqual(self._send_mock.call_count, 1)
        row = bulk.recipient_occurrences.get()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENT)

    def test_claim_failed_branch_preserves_exact_original_message_and_returncode(self):
        """Forces the defensive `claimed is None` branch (check 14) via a
        mock, with no real DB race and no transport -- converts the
        former `pragma: no cover` branch into specified, tested
        behavior. Must reproduce, byte-for-byte, the exact pre-PR-C
        CommandError the monolithic command used to raise for this case:
        'real_send_allowed: job claim failed unexpectedly', returncode=4
        -- deliberately WITHOUT the `f"{code}: {message}"` prefix every
        other refusal uses."""
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        self.make_occurrence(bulk)

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            with mock.patch(
                "relay.services.bulk_v2_real_send_execute.BackgroundJob.objects.select_for_update"
            ) as mocked_select_for_update:
                mocked_select_for_update.return_value.filter.return_value.first.return_value = None
                with mock.patch(
                    "relay.services.bulk_v2_real_send_execute.run_claimed_job"
                ) as mocked_dispatch:
                    outcome = authorize_and_execute_real_send(bulk.pk)

        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.returncode, 4)
        self.assertEqual(
            outcome.command_error_message,
            "real_send_allowed: job claim failed unexpectedly",
        )
        mocked_dispatch.assert_not_called()
        self._transport_mock.assert_not_called()
        # The job WAS created (check 14's create() runs before the claim
        # attempt) but never transitions past STATE_QUEUED -- identical
        # to the pre-PR-C command's behavior for this exact branch.
        self.assertEqual(BackgroundJob.objects.count(), 1)
        self.assertEqual(BackgroundJob.objects.get().state, BackgroundJob.STATE_QUEUED)

        # Command-level: the CommandError raised by the thin wrapper must
        # carry this exact message, unprefixed, with returncode=4.
        with self.assertRaises(CommandError) as ctx:
            with mock.patch(
                "relay.management.commands.bulk_v2_real_send.authorize_and_execute_real_send",
                return_value=outcome,
            ):
                call_command("bulk_v2_real_send", bulk_send_id=bulk.pk)
        self.assertEqual(str(ctx.exception), "real_send_allowed: job claim failed unexpectedly")
        self.assertEqual(ctx.exception.returncode, 4)


class QuotaIntegrationTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    """PR B's quota guard lives entirely inside process_bulk_id_v2, called
    unchanged via run_claimed_job -- these tests reconfirm the PR C
    extraction did not disturb that boundary."""

    _DOPPLER_RELAY_TEST_CFG = dict(
        API_KEY="test-key", ACCOUNT_ID=1, AUTH_SCHEME="Bearer",
        BASE_URL="https://api.dopplerrelay.com/",
        DEFAULT_FROM_EMAIL="noreply@example.com", DEFAULT_FROM_NAME="Quota Test",
        TIMEOUT=30,
    )

    def test_quota_exhausted_with_guard_on_no_additional_post(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk)

        from django.utils import timezone as django_timezone

        starts = _resolve_window_starts(django_timezone.now())
        QuotaWindow.objects.create(
            window_type=QuotaWindow.WINDOW_HOUR, window_start=starts[QuotaWindow.WINDOW_HOUR],
            limit_value=1, consumed=1,
        )

        settings_kwargs = dict(
            self.authorized_settings(user=user, bulk=bulk),
            DOPPLER_QUOTA_GUARD_ENABLED=True,
            DOPPLER_QUOTA_MONTHLY_LIMIT=1000,
            DOPPLER_QUOTA_DAILY_LIMIT=100,
            DOPPLER_QUOTA_HOURLY_LIMIT=1,
            DOPPLER_QUOTA_SAFETY_MARGIN_RATIO=0.0,
            DOPPLER_QUOTA_OVERAGE_ENABLED=False,
        )
        with override_settings(**settings_kwargs):
            outcome = authorize_and_execute_real_send(bulk.pk)

        self.assertTrue(outcome.executed)  # authorization succeeded; the SEND loop hit quota
        self.assertEqual(outcome.job_state, BackgroundJob.STATE_DONE)
        self._send_mock.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)

    def test_quota_off_uses_legacy_v2_claim_path(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse())

        settings_kwargs = dict(
            self.authorized_settings(user=user, bulk=bulk),
            DOPPLER_QUOTA_GUARD_ENABLED=False,
        )
        with override_settings(**settings_kwargs):
            outcome = authorize_and_execute_real_send(bulk.pk)

        self.assertTrue(outcome.executed)
        self.assertEqual(QuotaWindow.objects.count(), 0)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENT)
