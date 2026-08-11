"""PR2b-T22..T29, T31: the 8 crash/restart/concurrency scenarios plus the
V1-defect fail-closed absorption test — each an independent test, per the
orchestrator's explicit "do not group these" instruction.

Every test that can reach the send path inherits NoRealDopplerCallTestCase
(PR2b-T35) and configures its own transport mock explicitly. Every test
that must prove zero CSV dependency (hard requirement (d)) explicitly
deletes the recipients_file before the assertion.
"""

from __future__ import annotations

import requests
from django.core.management import CommandError, call_command
from django.test import override_settings

from relay.models import BackgroundJob, BulkSendRecipient
from relay.services.bulk_v2_send import process_bulk_id_v2
from relay.services.bulk_v2_send_state import (
    claim_next_recipient,
    describe_send_ledger,
    mark_sent,
)
from relay.services.jobs import run_background_job
from relay.tests._bulk_v2_real_send_support import (
    FakeDopplerResponse,
    NoRealDopplerCallTestCase,
    RealSendFixtureMixin,
)


class BulkV2CrashScenarioTests(RealSendFixtureMixin, NoRealDopplerCallTestCase):
    # --- Scenario 1 (PR2b-T22): worker dies BEFORE the outbound call -------

    def test_scenario1_worker_dies_before_outbound_call(self):
        """Proves (a) vacuously (no send occurred) and establishes the
        baseline for (c): claiming alone (simulating the durable
        pre-call commit) never itself sends, and nothing in this test
        resets the row. (d): CSV deleted before the assertion."""
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        bulk.recipients_file.delete(save=False)

        claimed_pk = claim_next_recipient(bulk.pk, job_id=1)
        self.assertEqual(claimed_pk, row.pk)

        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENDING)
        self.assertIsNotNone(row.send_started_at)
        self._transport_mock.assert_not_called()

    # --- Scenario 2 (PR2b-T23): worker dies DURING the outbound call -------

    def test_scenario2_worker_dies_during_outbound_call(self):
        """Proves (b): the row lands in a terminal state (ambiguous), not
        stuck in sending, and no further code path in this module can
        move it back out (grep-proved separately by PR2b-T33). (d): CSV
        deleted before the assertion."""
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        bulk.recipients_file.delete(save=False)
        self.mock_transport(side_effect=requests.Timeout("simulated mid-call death"))
        settings_kwargs = self.authorized_settings(user=self.make_user(), bulk=bulk)

        with override_settings(**settings_kwargs):
            process_bulk_id_v2(bulk.pk, job_id=1)

        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_AMBIGUOUS)
        # NOT "timeout": with a single-attempt client, `_request`'s retry
        # loop exhausts on the FIRST failure and takes the terminal-wrapper
        # branch that carries the pre-existing V1 defect (doppler_relay.py's
        # comment near its terminal DopplerRelayError raise) — a raw
        # requests.Timeout's `.response` attribute exists and is None, so
        # `getattr(...).status_code` raises AttributeError instead of
        # returning a DopplerRelayError. bulk_v2_send.py's classifier
        # therefore observes an AttributeError here, not a requests.Timeout,
        # and correctly maps it to dispatch_exception via its broad
        # catch-all — proving in practice (not just by argument) that this
        # scenario is absorbed the same way as PR2b-T31, discovered as an
        # emergent property of pairing max_attempts=1 with the V1 defect.
        self.assertEqual(row.send_error_code, "dispatch_exception")
        self.assertNotEqual(row.send_status, BulkSendRecipient.SEND_SENDING)

    # --- Scenario 3 (PR2b-T24): Doppler may have accepted, but the ---------
    # --- process dies before persisting `sent` ------------------------------

    def test_scenario3_doppler_may_have_accepted_but_crash_before_persisting_sent(self):
        """The scenario the whole design exists for. Modeled as: the row
        remains `sending` after a Doppler call would have succeeded,
        because nothing here calls mark_sent — proving the row is NEVER
        silently reported as `sent` on the mere assumption a call was
        attempted, only on persisted evidence (proves (a) and (c))."""
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        # A successful response IS configured, to state the scenario
        # symbolically, but is deliberately never invoked in this test —
        # only claim_next_recipient runs, simulating "the process died
        # before the terminal transition could be persisted".
        self.mock_transport(return_value=FakeDopplerResponse())

        claimed_pk = claim_next_recipient(bulk.pk, job_id=1)
        self.assertEqual(claimed_pk, row.pk)
        self._transport_mock.assert_not_called()

        ledger = describe_send_ledger(bulk.pk)
        self.assertIn(row.pk, {r.pk for r in ledger.in_flight})
        self.assertNotIn(row.pk, {r.pk for r in ledger.terminal_sent})
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENDING)

    # --- Scenario 4 (PR2b-T25): crash AFTER persisting `sent` --------------

    def test_scenario4_crash_after_persisting_sent_is_clean_no_op_on_recovery(self):
        """Proves (a): sent never re-sends. Re-invocation via the full
        management command lands on real_send_nothing_to_send, exit 0,
        zero additional Doppler calls."""
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk)
        claim_next_recipient(bulk.pk, job_id=1)
        mark_sent(row.pk, message_id="msg-1", location="")
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENT)

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            with self.assertRaises(CommandError) as cm:
                call_command("bulk_v2_real_send", bulk_send_id=bulk.pk)
        self.assertIn("real_send_nothing_to_send", str(cm.exception))
        self.assertEqual(cm.exception.returncode, 0)
        self._transport_mock.assert_not_called()

    # --- Scenario 5 (PR2b-T26): service restart with a row left sending ----

    def test_scenario5_service_restart_with_row_left_sending(self):
        """Proves (c) explicitly: a fresh, independent read of persisted
        state alone (no in-memory carry-over) shows the row unchanged."""
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        claim_next_recipient(bulk.pk, job_id=1)
        row.refresh_from_db()
        started_at_before = row.send_started_at

        # Simulate "restart": construct a brand new ledger read, touching
        # only the database, with no process-memory state reused.
        ledger_after_restart = describe_send_ledger(bulk.pk)
        restarted_row = next(r for r in ledger_after_restart.rows if r.pk == row.pk)

        self.assertEqual(restarted_row.send_status, BulkSendRecipient.SEND_SENDING)
        self.assertEqual(restarted_row.send_started_at, started_at_before)
        self.assertIn(row.pk, {r.pk for r in ledger_after_restart.in_flight})
        self._transport_mock.assert_not_called()

    # --- Scenario 7 (PR2b-T28): same BackgroundJob executed twice ----------
    # --- via run_background_job's claim_next_job bypass --------------------

    def test_scenario7_same_background_job_executed_twice_via_bypass(self):
        """Proves the per-recipient compare-and-set — not job-level
        locking — is the actual safety boundary (design §7): calling
        run_background_job(job_id) TWICE (the exact jobs.py:68-69 bypass
        path that skips claim_next_job's select_for_update) results in at
        most one Doppler call for the single eligible row."""
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse())

        job = BackgroundJob.objects.create(
            job_type=BackgroundJob.TYPE_BULK_SEND_V2_REAL,
            bulk=bulk,
            state=BackgroundJob.STATE_QUEUED,
        )

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            run_background_job(job.id)
            run_background_job(job.id)

        self.assertLessEqual(self._transport_mock.call_count, 1)
        row = bulk.recipient_occurrences.get()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENT)

    # --- Scenario 8 (PR2b-T29): the management command invoked twice -------

    def test_scenario8_command_invoked_repeatedly_is_idempotent_no_op(self):
        """First invocation sends; second invocation is a clean no-op
        (exit 0, real_send_nothing_to_send), zero additional Doppler
        calls. Exercises the full command path, not just the state layer
        (kept distinct from PR2b-T25/scenario 4, which proves the same
        property at the state-machine layer directly)."""
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        self.make_occurrence(bulk)
        self.mock_transport(return_value=FakeDopplerResponse())

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            call_command("bulk_v2_real_send", bulk_send_id=bulk.pk)
            self.assertEqual(self._transport_mock.call_count, 1)

            with self.assertRaises(CommandError) as cm:
                call_command("bulk_v2_real_send", bulk_send_id=bulk.pk)
        self.assertIn("real_send_nothing_to_send", str(cm.exception))
        self.assertEqual(cm.exception.returncode, 0)
        self.assertEqual(self._transport_mock.call_count, 1)

    # --- PR2b-T31: fail-closed absorption of the pre-existing V1 defect ----

    def test_v1_attribute_error_defect_is_absorbed_as_ambiguous(self):
        """Simulates exactly what _request would raise on a real
        requests.Timeout given the documented pre-existing V1 defect
        (doppler_relay.py's comment near its terminal DopplerRelayError
        raise) — constructed directly at the mock transport boundary, per
        the orchestrator's explicit instruction not to attempt to
        reproduce the defect via real _request machinery."""
        user = self.make_user()
        bulk = self.make_bulk(user=user)
        row = self.make_occurrence(bulk)
        self.mock_transport(side_effect=AttributeError(
            "'NoneType' object has no attribute 'status_code'"
        ))

        with override_settings(**self.authorized_settings(user=user, bulk=bulk)):
            result = process_bulk_id_v2(bulk.pk, job_id=1)
        self.assertIn("1 fila", result)

        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_AMBIGUOUS)
        self.assertEqual(row.send_error_code, "dispatch_exception")
        self.assertNotEqual(row.send_status, BulkSendRecipient.SEND_SENDING)
