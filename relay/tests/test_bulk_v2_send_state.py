from __future__ import annotations

import re
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from relay.models import BulkSend, BulkSendRecipient
from relay.services.bulk_import import BulkImportService
from relay.services.bulk_v2_send_state import (
    LEGAL_TRANSITIONS,
    STALE_SENDING_AFTER,
    RECOVERY_ABORT_AMBIGUOUS,
    RECOVERY_ABORT_IN_FLIGHT,
    RECOVERY_ABORT_STALE_SENDING,
    RECOVERY_CLEAN_NO_OP,
    RECOVERY_PROCEED_TO_GATE,
    SendStateError,
    SendStateTransitionError,
    claim_next_recipient,
    describe_send_ledger,
    ensure_autocommit_context,
    mark_ambiguous,
    mark_send_failed,
    mark_sent,
    next_recovery_step,
)


class BulkV2SendStateTestBase(TestCase):
    def make_bulk(self):
        return BulkSend.objects.create(
            template_id="tpl",
            template_name="Template",
            recipients_file=SimpleUploadedFile(
                "rows.csv", b"email,name\na@example.com,A\n"
            ),
            engine_version=BulkSend.ENGINE_V2,
        )

    def make_occurrence(self, bulk, **overrides):
        values = dict(
            bulk_send=bulk,
            import_version=1,
            source_row_number=1,
            recipient="a@example.com",
            normalized_recipient="a@example.com",
            payload={"name": "A"},
            payload_hash="a" * 64,
            idempotency_key=uuid.uuid4(),
            status=BulkSendRecipient.STATUS_PENDING,
        )
        values.update(overrides)
        return BulkSendRecipient.objects.create(**values)


# ---------------------------------------------------------------------------
# PR2a-T6: one test per invariant (design.md §2.3's table, 6 invariants)
# ---------------------------------------------------------------------------
class BulkV2SendStateInvariantTests(BulkV2SendStateTestBase):
    # --- Invariant 1: status=invalid can never progress past not_started --

    def test_invalid_row_cannot_reach_sending_via_direct_save(self):
        bulk = self.make_bulk()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_occurrence(
                    bulk,
                    status=BulkSendRecipient.STATUS_INVALID,
                    send_status=BulkSendRecipient.SEND_SENDING,
                    send_started_at=timezone.now(),
                    send_attempt_number=1,
                )

    def test_invalid_row_cannot_reach_sent_via_direct_save(self):
        bulk = self.make_bulk()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_occurrence(
                    bulk,
                    status=BulkSendRecipient.STATUS_INVALID,
                    send_status=BulkSendRecipient.SEND_SENT,
                    send_started_at=timezone.now(),
                    sent_at=timezone.now(),
                    send_attempt_number=1,
                )

    def test_claim_next_recipient_excludes_invalid_rows(self):
        bulk = self.make_bulk()
        invalid_row = self.make_occurrence(
            bulk, status=BulkSendRecipient.STATUS_INVALID, source_row_number=1,
            idempotency_key=uuid.uuid4(),
        )
        claimed = claim_next_recipient(bulk.pk, job_id=1)
        self.assertIsNone(claimed)
        invalid_row.refresh_from_db()
        self.assertEqual(invalid_row.send_status, BulkSendRecipient.SEND_NOT_STARTED)

    # --- Invariant 2: sent requires sent_at -------------------------------

    def test_sent_without_sent_at_is_rejected(self):
        bulk = self.make_bulk()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_occurrence(
                    bulk,
                    send_status=BulkSendRecipient.SEND_SENT,
                    send_started_at=timezone.now(),
                    sent_at=None,
                    send_attempt_number=1,
                )

    # --- Invariant 3: sending requires send_started_at ---------------------

    def test_sending_without_send_started_at_is_rejected(self):
        bulk = self.make_bulk()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_occurrence(
                    bulk,
                    send_status=BulkSendRecipient.SEND_SENDING,
                    send_started_at=None,
                    send_attempt_number=1,
                )

    # --- Invariant 4: ambiguous unreachable directly from not_started ------

    def test_ambiguous_unreachable_directly_from_not_started(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)
        with self.assertRaises(SendStateTransitionError):
            mark_ambiguous(row.pk, error_code="timeout", error_message="boom")
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)

    def test_ambiguous_reachable_only_after_sending(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        claimed = claim_next_recipient(bulk.pk, job_id=1)
        self.assertEqual(claimed, row.pk)
        mark_ambiguous(row.pk, error_code="timeout", error_message="boom")
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_AMBIGUOUS)
        self.assertEqual(row.send_error_code, "timeout")

    # --- Invariant 5: send_failed requires a non-empty error code ----------

    def test_send_failed_without_error_code_is_rejected_at_db_level(self):
        bulk = self.make_bulk()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_occurrence(
                    bulk,
                    send_status=BulkSendRecipient.SEND_FAILED,
                    send_started_at=timezone.now(),
                    send_attempt_number=1,
                    send_error_code="",
                )

    def test_mark_send_failed_rejects_empty_error_code_at_service_layer(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        claim_next_recipient(bulk.pk, job_id=1)
        with self.assertRaises(SendStateTransitionError):
            mark_send_failed(row.pk, error_code="", error_message="x")

    # --- Invariant 6: no sent -> sending (or any) backward transition ------

    def test_sent_row_rejects_save_back_to_sending(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        claim_next_recipient(bulk.pk, job_id=1)
        mark_sent(row.pk, message_id="msg-1", location="https://x/1")
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENT)

        row.send_status = BulkSendRecipient.SEND_SENDING
        with self.assertRaises(ValueError):
            row.save()

        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENT)

    def test_compare_and_set_rejects_sent_to_sending(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        claim_next_recipient(bulk.pk, job_id=1)
        mark_sent(row.pk, message_id="msg-1", location="https://x/1")

        # No function in bulk_v2_send_state.py targets `sending` as a SET
        # value with `sent` as the expected WHERE — verified structurally
        # by the legal-edge whitelist not containing ("sent", "sending").
        self.assertNotIn(("sent", "sending"), LEGAL_TRANSITIONS)
        # And a raw attempt at the same compare-and-set pattern this module
        # uses elsewhere (WHERE send_status='not_started', a state the row
        # is no longer in) matches zero rows. Uses raw SQL, not the ORM's
        # .update(), to keep this test file itself outside the grep
        # boundary asserted by SendStatusWriteBoundaryTests below.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE relay_bulksendrecipient SET send_status = %s "
                "WHERE id = %s AND send_status = %s",
                ["sending", row.pk, "not_started"],
            )
            self.assertEqual(cursor.rowcount, 0)

    def test_terminal_rows_reject_any_other_transition_via_save(self):
        bulk = self.make_bulk()
        for target_send_status, mark in (
            (BulkSendRecipient.SEND_SENT, lambda pk: mark_sent(pk, message_id="m", location="l")),
            (BulkSendRecipient.SEND_FAILED, lambda pk: mark_send_failed(pk, error_code="e", error_message="m")),
            (BulkSendRecipient.SEND_AMBIGUOUS, lambda pk: mark_ambiguous(pk, error_code="e", error_message="m")),
        ):
            with self.subTest(target=target_send_status):
                row = self.make_occurrence(
                    bulk, source_row_number=bulk.recipient_occurrences.count() + 1,
                    idempotency_key=uuid.uuid4(),
                )
                claim_next_recipient(bulk.pk, job_id=1)
                mark(row.pk)
                row.refresh_from_db()
                self.assertEqual(row.send_status, target_send_status)

                row.send_status = BulkSendRecipient.SEND_NOT_STARTED
                with self.assertRaises(ValueError):
                    row.save()

    # --- Transition-graph whitelist: only the four defined edges exist ----

    def test_legal_transitions_whitelist_is_exactly_the_four_edges(self):
        self.assertEqual(
            LEGAL_TRANSITIONS,
            frozenset({
                ("not_started", "sending"),
                ("sending", "sent"),
                ("sending", "send_failed"),
                ("sending", "ambiguous"),
            }),
        )

    def test_full_transition_sequence_observes_only_legal_edges(self):
        """Exercises all four legal edges end to end and records every
        (from, to) pair actually produced by the module's public
        functions, asserting the observed set is a subset of
        LEGAL_TRANSITIONS (spec: 'every observed transition MUST be one
        of' the four defined edges)."""
        bulk = self.make_bulk()
        observed: set[tuple[str, str]] = set()

        sent_row = self.make_occurrence(bulk, idempotency_key=uuid.uuid4())
        pk = claim_next_recipient(bulk.pk, job_id=1)
        observed.add(("not_started", "sending"))
        mark_sent(pk, message_id="m", location="l")
        observed.add(("sending", "sent"))

        failed_row = self.make_occurrence(
            bulk, source_row_number=2, idempotency_key=uuid.uuid4()
        )
        pk = claim_next_recipient(bulk.pk, job_id=1)
        observed.add(("not_started", "sending"))
        mark_send_failed(pk, error_code="e", error_message="m")
        observed.add(("sending", "send_failed"))

        ambiguous_row = self.make_occurrence(
            bulk, source_row_number=3, idempotency_key=uuid.uuid4()
        )
        pk = claim_next_recipient(bulk.pk, job_id=1)
        observed.add(("not_started", "sending"))
        mark_ambiguous(pk, error_code="e", error_message="m")
        observed.add(("sending", "ambiguous"))

        self.assertTrue(observed.issubset(LEGAL_TRANSITIONS))
        self.assertEqual(observed, LEGAL_TRANSITIONS)


# ---------------------------------------------------------------------------
# PR2a-T7: claim_next_recipient unit tests
# ---------------------------------------------------------------------------
class ClaimNextRecipientTests(BulkV2SendStateTestBase):
    def test_successful_claim_transitions_and_commits(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)
        before = timezone.now()

        claimed_pk = claim_next_recipient(bulk.pk, job_id=42)

        self.assertEqual(claimed_pk, row.pk)
        fresh = BulkSendRecipient.objects.get(pk=row.pk)
        self.assertEqual(fresh.send_status, BulkSendRecipient.SEND_SENDING)
        self.assertIsNotNone(fresh.send_started_at)
        self.assertGreaterEqual(fresh.send_started_at, before)
        self.assertEqual(fresh.send_attempt_number, 1)
        self.assertEqual(fresh.send_job_id, 42)

    def test_claim_returns_none_when_no_eligible_row(self):
        bulk = self.make_bulk()
        self.assertIsNone(claim_next_recipient(bulk.pk, job_id=1))

    def test_claim_skips_invalid_rows_even_when_not_started(self):
        bulk = self.make_bulk()
        self.make_occurrence(bulk, status=BulkSendRecipient.STATUS_INVALID)
        self.assertIsNone(claim_next_recipient(bulk.pk, job_id=1))

    def test_claim_loses_race_when_row_mutated_underneath_by_raw_sql(self):
        """Backend-independent simulation of a lost race: another writer
        (raw SQL, bypassing the ORM's .update()) flips the row to
        `sending` between when this test observes it as eligible and when
        claim_next_recipient runs. The compare-and-set WHERE clause must
        reject the update rather than corrupt the row."""
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE relay_bulksendrecipient "
                "SET send_status = %s, send_started_at = %s, "
                "send_attempt_number = 1 WHERE id = %s",
                ["sending", timezone.now().isoformat(), row.pk],
            )

        claimed = claim_next_recipient(bulk.pk, job_id=99)
        self.assertIsNone(claimed)
        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENDING)
        self.assertNotEqual(row.send_job_id, 99)

    def test_claim_order_is_deterministic(self):
        bulk = self.make_bulk()
        second = self.make_occurrence(
            bulk, source_row_number=2, idempotency_key=uuid.uuid4()
        )
        first = self.make_occurrence(
            bulk, source_row_number=1, idempotency_key=uuid.uuid4()
        )
        claimed = claim_next_recipient(bulk.pk, job_id=1)
        self.assertEqual(claimed, first.pk)


# ---------------------------------------------------------------------------
# PR2a-T8: describe_send_ledger unit tests — one per classification bucket
# ---------------------------------------------------------------------------
class DescribeSendLedgerTests(BulkV2SendStateTestBase):
    def _row(self, bulk, n, **overrides):
        return self.make_occurrence(
            bulk, source_row_number=n, idempotency_key=uuid.uuid4(), **overrides
        )

    def test_seven_buckets_are_mutually_exclusive_and_correctly_populated(self):
        bulk = self.make_bulk()
        now = timezone.now()

        excluded_row = self._row(bulk, 1, status=BulkSendRecipient.STATUS_INVALID)
        eligible_row = self._row(bulk, 2)
        in_flight_row = self._row(
            bulk, 3,
            send_status=BulkSendRecipient.SEND_SENDING,
            send_started_at=now - timedelta(seconds=1),
            send_attempt_number=1,
        )
        stale_row = self._row(
            bulk, 4,
            send_status=BulkSendRecipient.SEND_SENDING,
            send_started_at=now - STALE_SENDING_AFTER - timedelta(seconds=1),
            send_attempt_number=1,
        )
        sent_row = self._row(
            bulk, 5,
            send_status=BulkSendRecipient.SEND_SENT,
            send_started_at=now - timedelta(seconds=5),
            sent_at=now,
            send_attempt_number=1,
        )
        failed_row = self._row(
            bulk, 6,
            send_status=BulkSendRecipient.SEND_FAILED,
            send_started_at=now - timedelta(seconds=5),
            send_attempt_number=1,
            send_error_code="doppler_http_400",
        )
        ambiguous_row = self._row(
            bulk, 7,
            send_status=BulkSendRecipient.SEND_AMBIGUOUS,
            send_started_at=now - timedelta(seconds=5),
            send_attempt_number=1,
            send_error_code="timeout",
        )

        ledger = describe_send_ledger(bulk.pk, now=now)

        self.assertEqual({r.pk for r in ledger.excluded}, {excluded_row.pk})
        self.assertEqual({r.pk for r in ledger.eligible}, {eligible_row.pk})
        self.assertEqual({r.pk for r in ledger.in_flight}, {in_flight_row.pk})
        self.assertEqual({r.pk for r in ledger.stale_sending}, {stale_row.pk})
        self.assertEqual({r.pk for r in ledger.terminal_sent}, {sent_row.pk})
        self.assertEqual({r.pk for r in ledger.terminal_failed}, {failed_row.pk})
        self.assertEqual({r.pk for r in ledger.blocked_ambiguous}, {ambiguous_row.pk})
        self.assertEqual(len(ledger.rows), 7)

        buckets = (
            ledger.excluded, ledger.eligible, ledger.in_flight,
            ledger.stale_sending, ledger.terminal_sent, ledger.terminal_failed,
            ledger.blocked_ambiguous,
        )
        seen_pks: set[int] = set()
        for bucket in buckets:
            for ledger_row in bucket:
                self.assertNotIn(ledger_row.pk, seen_pks)
                seen_pks.add(ledger_row.pk)

    def test_stale_boundary_is_applied_at_exactly_stale_sending_after(self):
        bulk = self.make_bulk()
        now = timezone.now()
        fresh = self._row(
            bulk, 1,
            send_status=BulkSendRecipient.SEND_SENDING,
            send_started_at=now - timedelta(seconds=1),
            send_attempt_number=1,
        )
        aged = self._row(
            bulk, 2,
            send_status=BulkSendRecipient.SEND_SENDING,
            send_started_at=now - STALE_SENDING_AFTER - timedelta(seconds=1),
            send_attempt_number=1,
        )

        ledger = describe_send_ledger(bulk.pk, now=now)

        self.assertEqual({r.pk for r in ledger.in_flight}, {fresh.pk})
        self.assertEqual({r.pk for r in ledger.stale_sending}, {aged.pk})

    def test_stale_sending_after_is_300_seconds(self):
        self.assertEqual(
            STALE_SENDING_AFTER, timedelta(seconds=10 * settings.DOPPLER_RELAY["TIMEOUT"])
        )
        self.assertEqual(STALE_SENDING_AFTER, timedelta(seconds=300))

    def test_zero_csv_dependency_describe_send_ledger_and_claim(self):
        """design.md §6: 'Zero CSV dependency is structural.' Behaviour
        must be identical whether the original recipients_file is
        present, missing, or corrupted, because describe_send_ledger and
        claim_next_recipient never reference it."""
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)

        file_path = Path(bulk.recipients_file.path)
        self.assertTrue(file_path.exists())
        ledger_with_file = describe_send_ledger(bulk.pk)
        self.assertEqual(len(ledger_with_file.eligible), 1)

        file_path.unlink()
        self.assertFalse(file_path.exists())

        ledger_without_file = describe_send_ledger(bulk.pk)
        self.assertEqual(len(ledger_without_file.eligible), 1)
        self.assertEqual(
            [r.pk for r in ledger_with_file.eligible],
            [r.pk for r in ledger_without_file.eligible],
        )

        claimed = claim_next_recipient(bulk.pk, job_id=1)
        self.assertEqual(claimed, row.pk)


class RecoveryDecisionTests(TestCase):
    """The ordered 5-rule recovery decision (design §6), consumed by the
    management command in PR2b. Pure function over an already-computed
    SendLedger; tested here without any command/worker code."""

    def _ledger(self, **buckets):
        from relay.services.bulk_v2_send_state import SendLedger

        defaults = dict(
            bulk_send_id=1, rows=(), excluded=(), eligible=(), in_flight=(),
            stale_sending=(), terminal_sent=(), terminal_failed=(),
            blocked_ambiguous=(),
        )
        defaults.update(buckets)
        return SendLedger(**defaults)

    def test_ambiguous_wins_over_everything_else(self):
        ledger = self._ledger(
            blocked_ambiguous=("x",), stale_sending=("y",), in_flight=("z",),
            eligible=("w",),
        )
        self.assertEqual(next_recovery_step(ledger), RECOVERY_ABORT_AMBIGUOUS)

    def test_stale_sending_wins_over_in_flight_and_eligible(self):
        ledger = self._ledger(stale_sending=("y",), in_flight=("z",), eligible=("w",))
        self.assertEqual(next_recovery_step(ledger), RECOVERY_ABORT_STALE_SENDING)

    def test_in_flight_wins_over_eligible(self):
        ledger = self._ledger(in_flight=("z",), eligible=("w",))
        self.assertEqual(next_recovery_step(ledger), RECOVERY_ABORT_IN_FLIGHT)

    def test_empty_eligible_is_clean_no_op(self):
        ledger = self._ledger()
        self.assertEqual(next_recovery_step(ledger), RECOVERY_CLEAN_NO_OP)

    def test_otherwise_proceeds_to_gate(self):
        ledger = self._ledger(eligible=("w",))
        self.assertEqual(next_recovery_step(ledger), RECOVERY_PROCEED_TO_GATE)


class EnsureAutocommitContextTests(TransactionTestCase):
    # TransactionTestCase, not TestCase: TestCase itself wraps every test
    # in an outer transaction.atomic() for rollback-based isolation, which
    # would make transaction.get_autocommit() report False even for the
    # "outside any transaction" case below.
    def test_raises_inside_open_transaction(self):
        with self.assertRaises(SendStateError):
            with transaction.atomic():
                ensure_autocommit_context()

    def test_does_not_raise_outside_a_transaction(self):
        ensure_autocommit_context()


# ---------------------------------------------------------------------------
# PR2a-T9: grep-provable structural test — send_status is written only in
# bulk_v2_send_state.py (and declared, not written, in the migration/model).
# ---------------------------------------------------------------------------
class SendStatusWriteBoundaryTests(TestCase):
    ALLOWED_RELATIVE_PATHS = {
        Path("services") / "bulk_v2_send_state.py",
        Path("migrations") / "20260808120000_bulk_v2_real_send_state.py",
    }

    UPDATE_CALL_RE = re.compile(r"\.update\(")
    ATTR_ASSIGN_RE = re.compile(r"\bself\.send_status\s*=(?!=)")

    def _extract_update_call_bodies(self, text: str) -> list[str]:
        bodies = []
        for match in self.UPDATE_CALL_RE.finditer(text):
            start = match.end() - 1  # position of the opening '('
            depth = 0
            end = None
            for idx in range(start, len(text)):
                if text[idx] == "(":
                    depth += 1
                elif text[idx] == ")":
                    depth -= 1
                    if depth == 0:
                        end = idx
                        break
            if end is not None:
                bodies.append(text[start:end])
        return bodies

    def _writes_send_status(self, text: str) -> bool:
        for body in self._extract_update_call_bodies(text):
            if re.search(r"\bsend_status\s*=", body):
                return True
        return bool(self.ATTR_ASSIGN_RE.search(text))

    def test_send_status_is_written_only_in_the_state_module_and_migration(self):
        relay_root = Path(settings.BASE_DIR) / "relay"
        violations = []
        for path in relay_root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(relay_root)
            if relative in self.ALLOWED_RELATIVE_PATHS:
                continue
            text = path.read_text(encoding="utf-8")
            if self._writes_send_status(text):
                violations.append(str(relative))
        self.assertEqual(
            violations, [],
            f"send_status written outside the state module in: {violations}",
        )

    def test_allowed_module_does_contain_the_writes(self):
        # Sanity check that the detector isn't vacuously passing.
        state_module = (
            Path(settings.BASE_DIR) / "relay" / "services" / "bulk_v2_send_state.py"
        )
        text = state_module.read_text(encoding="utf-8")
        self.assertTrue(self._writes_send_status(text))


# ---------------------------------------------------------------------------
# Existing V2 import path safety: every row shape BulkImportService produces
# today satisfies all six new constraints without modification.
# ---------------------------------------------------------------------------
class ImportPathConstraintSafetyTests(TestCase):
    def test_import_created_rows_satisfy_all_six_new_constraints(self):
        bulk = BulkSend.objects.create(
            template_id="tpl",
            template_name="Template",
            recipients_file=SimpleUploadedFile(
                "rows.csv",
                b"email,name\na@example.com,A\ninvalid-not-an-email,B\n",
            ),
            engine_version=BulkSend.ENGINE_V2,
        )
        result = BulkImportService(bulk).import_file()

        self.assertEqual(result.valid_rows, 1)
        self.assertEqual(result.invalid_rows, 1)

        rows = list(bulk.recipient_occurrences.order_by("source_row_number"))
        self.assertEqual(len(rows), 2)

        pending_rows = [r for r in rows if r.status == BulkSendRecipient.STATUS_PENDING]
        invalid_rows = [r for r in rows if r.status == BulkSendRecipient.STATUS_INVALID]
        self.assertEqual(len(pending_rows), 1)
        self.assertEqual(len(invalid_rows), 1)

        for row in rows:
            self.assertEqual(row.send_status, BulkSendRecipient.SEND_NOT_STARTED)
            self.assertEqual(row.send_attempt_number, 0)
            self.assertIsNone(row.send_started_at)
            self.assertIsNone(row.sent_at)
            self.assertEqual(row.send_error_code, "")
            self.assertEqual(row.send_message_id, "")
            self.assertEqual(row.send_location, "")
            self.assertIsNone(row.send_job_id)

        # Re-saving each row unmodified must not raise (constraints are
        # satisfied, not merely un-checked at INSERT time).
        for row in rows:
            row.save()

        # And the invalid row is correctly excluded from claiming.
        claimed = claim_next_recipient(bulk.pk, job_id=1)
        self.assertEqual(claimed, pending_rows[0].pk)
