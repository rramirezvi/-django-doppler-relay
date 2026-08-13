"""PR2b-T9..T21: management command verification tests (design.md §8).

Every test that can reach the send path inherits NoRealDopplerCallTestCase
(PR2b-T35) and configures its own transport mock explicitly.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import timedelta

from django.core.management import CommandError, call_command
from django.core.management.base import BaseCommand
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from relay.management.commands.bulk_v2_real_send import Command
from relay.models import BulkSend, BulkSendRecipient
from relay.services.bulk_v2_send_state import STALE_SENDING_AFTER, claim_next_recipient, mark_ambiguous
from relay.tests._bulk_v2_real_send_support import (
    FakeDopplerResponse,
    NoRealDopplerCallTestCase,
    RealSendFixtureMixin,
)


class BulkV2RealSendCommandTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    # --- PR2b-T9: explicit --bulk-send-id, no --latest/--all/positional ---

    def test_missing_bulk_send_id_fails_before_any_db_read(self):
        with CaptureQueriesContext(connection) as ctx:
            with self.assertRaises(CommandError) as cm:
                call_command("bulk_v2_real_send")
        self.assertIn("--bulk-send-id", str(cm.exception))
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_no_latest_or_all_or_positional_fallback(self):
        parser = Command().create_parser("manage.py", "bulk_v2_real_send")
        dests = {a.dest for a in parser._actions}
        self.assertNotIn("latest", dests)
        self.assertNotIn("all", dests)

    # --- PR2b-T10: engine_version == v2 only -------------------------------

    def test_legacy_engine_refused_before_gate_or_doppler_call(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user, engine_version=BulkSend.ENGINE_LEGACY, template_name="")
        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            with self.assertRaises(CommandError) as ctx:
                call_command("bulk_v2_real_send", bulk_send_id=bulk.pk)
        self.assertIn("real_send_engine_not_v2", str(ctx.exception))
        self.assertEqual(ctx.exception.returncode, 2)
        self._transport_mock.assert_not_called()

    # --- PR2b-T11: kill switch is a zero-query check; full gate is not ----

    def test_kill_switch_refusal_executes_zero_db_queries(self):
        with CaptureQueriesContext(connection) as ctx:
            with self.assertRaises(CommandError) as cm:
                call_command("bulk_v2_real_send", bulk_send_id=999999)
        self.assertIn("real_send_disabled", str(cm.exception))
        self.assertEqual(cm.exception.returncode, 5)
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_full_gate_refusal_reads_db_but_still_refuses(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        self.make_occurrence(bulk)
        settings_kwargs = self.authorized_settings(user=user, bulk=bulk)
        settings_kwargs["BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS"] = "not-example.com"
        with override_settings(**settings_kwargs):
            with CaptureQueriesContext(connection) as ctx:
                with self.assertRaises(CommandError) as cm:
                    call_command("bulk_v2_real_send", bulk_send_id=bulk.pk)
        self.assertEqual(cm.exception.returncode, 5)
        self.assertIn("real_send_domain_not_allowed", str(cm.exception))
        self.assertGreater(len(ctx.captured_queries), 0)

    # --- PR2b-T12: exact-match allowlist dimensions ------------------------

    def test_each_allowlist_dimension_mismatch_is_named_precisely(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        self.make_occurrence(bulk)
        base = self.authorized_settings(user=user, bulk=bulk)

        cases = [
            (dict(base, BULK_PROCESSING_V2_REAL_SEND_USER_IDS="999999"), "real_send_user_not_allowed"),
            (dict(base, BULK_PROCESSING_V2_REAL_SEND_REQUEST_IDS="other-request"), "real_send_request_not_allowed"),
            (dict(base, BULK_PROCESSING_V2_REAL_SEND_TEMPLATE_IDS="other-template"), "real_send_template_not_allowed"),
            (dict(base, BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS="evil.example"), "real_send_domain_not_allowed"),
        ]
        for settings_kwargs, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with override_settings(**settings_kwargs):
                    with self.assertRaises(CommandError) as cm:
                        call_command("bulk_v2_real_send", bulk_send_id=bulk.pk)
                self.assertIn(expected_code, str(cm.exception))

    # --- PR2b-T13: MAX_ROWS == 1 exactness ---------------------------------

    def test_two_eligible_rows_refused_with_row_limit_exceeded(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        self.make_occurrence(bulk, source_row_number=1, idempotency_key=uuid.uuid4())
        self.make_occurrence(
            bulk,
            source_row_number=2,
            recipient="b@example.com",
            normalized_recipient="b@example.com",
            idempotency_key=uuid.uuid4(),
        )
        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            with self.assertRaises(CommandError) as cm:
                call_command("bulk_v2_real_send", bulk_send_id=bulk.pk)
        self.assertIn("real_send_row_limit_exceeded", str(cm.exception))
        self._transport_mock.assert_not_called()

    # --- PR2b-T14/T15/T16: closed argument surface, no override flags -----

    def test_no_force_dest(self):
        parser = Command().create_parser("manage.py", "bulk_v2_real_send")
        self.assertNotIn("force", {a.dest for a in parser._actions})

    def test_no_retry_ambiguous_dest(self):
        parser = Command().create_parser("manage.py", "bulk_v2_real_send")
        self.assertNotIn("retry_ambiguous", {a.dest for a in parser._actions})

    def test_no_recipient_wildcard_or_literal_dest(self):
        parser = Command().create_parser("manage.py", "bulk_v2_real_send")
        dests = {a.dest for a in parser._actions}
        for forbidden in ("recipient", "email", "recipients"):
            self.assertNotIn(forbidden, dests)

    # --- PR2b-T17: never reads the CSV -------------------------------------

    def test_identical_behavior_whether_csv_present_or_deleted(self):
        user = self.make_user()

        def run_once(*, delete_csv: bool):
            bulk = self.make_bulk(user=user)
            self.make_occurrence(bulk)
            if delete_csv:
                bulk.recipients_file.delete(save=False)
            self.mock_transport(return_value=FakeDopplerResponse())
            with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
                call_command("bulk_v2_real_send", bulk_send_id=bulk.pk)
            row = bulk.recipient_occurrences.get()
            return row.send_status

        status_with_file = run_once(delete_csv=False)
        status_without_file = run_once(delete_csv=True)
        self.assertEqual(status_with_file, BulkSendRecipient.SEND_SENT)
        self.assertEqual(status_without_file, BulkSendRecipient.SEND_SENT)

    def test_command_module_has_no_csv_or_bulk_import_reference(self):
        # Executable-code check: strip the module's own top-of-file
        # docstring (which documents, in prose, that the command never
        # touches these — that documentary mention is not an executable
        # reference) before grepping for a real import/attribute-access
        # occurrence, per design §8.1/§6.
        import ast

        import relay.management.commands.bulk_v2_real_send as module

        source = inspect.getsource(module)
        tree = ast.parse(source)
        docstring = ast.get_docstring(tree) or ""
        executable_source = source.replace(docstring, "", 1)

        self.assertNotIn("import csv", executable_source)
        self.assertNotIn("recipients_file", executable_source)
        self.assertNotIn("BulkImportService", executable_source)

    # --- PR2b-T18: ambiguous row aborts, exit 3, touches nothing -----------

    def test_ambiguous_row_aborts_untouched(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk)
        claim_next_recipient(bulk.pk, job_id=1)
        mark_ambiguous(row.pk, error_code="timeout", error_message="boom")
        row.refresh_from_db()
        before = (row.send_status, row.send_error_code, row.send_message_id, row.send_location)

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            with self.assertRaises(CommandError) as cm:
                call_command("bulk_v2_real_send", bulk_send_id=bulk.pk)
        self.assertIn("real_send_ambiguous_present", str(cm.exception))
        self.assertEqual(cm.exception.returncode, 3)

        row.refresh_from_db()
        after = (row.send_status, row.send_error_code, row.send_message_id, row.send_location)
        self.assertEqual(before, after)
        self._transport_mock.assert_not_called()

    # --- PR2b-T19: stale sending row aborts, exit 4, touches nothing -------

    def test_stale_sending_row_aborts_untouched(self):
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk)
        claim_next_recipient(bulk.pk, job_id=1)
        stale_at = timezone.now() - STALE_SENDING_AFTER - timedelta(seconds=1)
        BulkSendRecipient.objects.filter(pk=row.pk).update(send_started_at=stale_at)
        row.refresh_from_db()
        before = (row.send_status, row.send_started_at)

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            with self.assertRaises(CommandError) as cm:
                call_command("bulk_v2_real_send", bulk_send_id=bulk.pk)
        self.assertIn("real_send_stale_sending_present", str(cm.exception))
        self.assertEqual(cm.exception.returncode, 4)

        row.refresh_from_db()
        after = (row.send_status, row.send_started_at)
        self.assertEqual(before, after)
        self._transport_mock.assert_not_called()

    # --- PR2b-T20: at most one SEND call per invocation (fix-bulk-v2-
    # template-variable-validation: distinct from the read-only, per-
    # BulkSend template-discovery GET, which is not a send attempt) -------

    def test_at_most_one_doppler_call_across_every_branch(self):
        user = self.make_user()

        # Branch: success path.
        bulk = self.make_bulk(user=user)
        self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse())
        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            call_command("bulk_v2_real_send", bulk_send_id=bulk.pk)
        # fix-bulk-v2-template-variable-validation: `_send_mock` (the SEND
        # call specifically), not `_transport_mock` (all traffic, which now
        # also includes the read-only template-discovery GET).
        self.assertLessEqual(self._send_mock.call_count, 1)

        # Branch: dry-run (must claim/call nothing — dry-run stops before
        # `process_bulk_id_v2`/the gate are ever reached, so this remains a
        # zero-Doppler-calls-of-any-kind branch, `_transport_mock` unchanged).
        calls_before = self._transport_mock.call_count
        bulk2 = self.make_bulk(user=user)
        self.make_occurrence(bulk2)
        with override_settings(**self.authorized_settings(user=user, bulk=bulk2)):
            with self.assertRaises(CommandError):
                call_command("bulk_v2_real_send", bulk_send_id=bulk2.pk, dry_run=True)
        self.assertEqual(self._transport_mock.call_count, calls_before)

    # --- PR2b-T21: exact closed argument surface ---------------------------

    def test_exact_argument_surface(self):
        parser = Command().create_parser("manage.py", "bulk_v2_real_send")
        actual_dests = {a.dest for a in parser._actions}

        baseline_parser = BaseCommand().create_parser("manage.py", "bulk_v2_real_send")
        baseline_dests = {a.dest for a in baseline_parser._actions}

        expected = baseline_dests | {"bulk_send_id", "dry_run"}
        self.assertEqual(actual_dests, expected)
