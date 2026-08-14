"""bulk-v2 quota integration -- genuinely concurrent locking tests
(PR B, design round 6/7). PostgreSQL-only.

Mirrors relay/tests/test_bulk_quota_postgresql.py's pattern exactly:
select_for_update()/FOR UPDATE has no real blocking guarantee on SQLite,
so correctness here rests entirely on real PostgreSQL row locks. Real
threads, real separate DB connections (close_old_connections() per
worker), no mocking of select_for_update/transaction.atomic().
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime
from datetime import timezone as dt_timezone
from unittest import skipUnless

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings

from relay.models import BulkSend, BulkSendRecipient, QuotaWindow
from relay.services.bulk_quota import QuotaExhausted, reserve_and_claim

POSTGRESQL = connection.vendor == "postgresql"

QUOTA_SETTINGS = dict(
    DOPPLER_QUOTA_GUARD_ENABLED=True,
    DOPPLER_QUOTA_MONTHLY_LIMIT=1_000_000,
    DOPPLER_QUOTA_DAILY_LIMIT=1_000_000,
    DOPPLER_QUOTA_HOURLY_LIMIT=1,  # the deliberately contended resource
    DOPPLER_QUOTA_SAFETY_MARGIN_RATIO=0.0,
    DOPPLER_QUOTA_OVERAGE_ENABLED=False,
)


def _make_bulk(*, client_request_id: str) -> BulkSend:
    return BulkSend.objects.create(
        template_id="tpl-real",
        template_name="Template real",
        client_request_id=client_request_id,
        recipients_file=SimpleUploadedFile(
            "rows.csv", b"email,name\na@example.com,A\n"
        ),
        engine_version=BulkSend.ENGINE_V2,
        import_status=BulkSend.IMPORT_READY,
    )


def _make_occurrence(bulk: BulkSend, *, source_row_number=1, recipient="a@example.com") -> BulkSendRecipient:
    return BulkSendRecipient.objects.create(
        bulk_send=bulk,
        import_version=1,
        source_row_number=source_row_number,
        recipient=recipient,
        normalized_recipient=recipient,
        payload={"name": "A"},
        payload_hash="a" * 64,
        idempotency_key=uuid.uuid4(),
    )


@skipUnless(POSTGRESQL, "Requiere PostgreSQL")
class TwoWorkersSameRecipientTests(TransactionTestCase):
    """#21 -- two workers race for the SAME single recipient. At most one
    reservation total: exactly one claim, quota incremented exactly once,
    the loser gets None (never QuotaExhausted -- there was nothing left
    for it to even compete for once the row lock resolves)."""

    @override_settings(**QUOTA_SETTINGS)
    def test_two_workers_same_recipient_one_reservation_total(self):
        bulk = _make_bulk(client_request_id="pg-quota-same-recipient")
        _make_occurrence(bulk)

        results: list[object] = []
        errors: list[BaseException] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker():
            close_old_connections()
            barrier.wait(timeout=10)
            try:
                pk = reserve_and_claim(bulk.pk, job_id=1)
                with results_lock:
                    results.append(pk)
            except BaseException as exc:  # noqa: BLE001 -- must prove none escape
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
        non_none = [r for r in results if r is not None]
        self.assertEqual(len(non_none), 1)
        self.assertEqual(results.count(None), 1)

        for window in QuotaWindow.objects.all():
            self.assertLessEqual(window.consumed, window.limit_value)
        self.assertEqual(QuotaWindow.objects.get(window_type=QuotaWindow.WINDOW_HOUR).consumed, 1)


@skipUnless(POSTGRESQL, "Requiere PostgreSQL")
class TwoWorkersDifferentRecipientsLastUnitTests(TransactionTestCase):
    """#22 -- two workers, two DIFFERENT eligible recipients in the same
    BulkSend, but only one hourly unit available. Exactly one worker wins
    the claim; the other observes QuotaExhausted, its recipient staying
    not_started."""

    @override_settings(**QUOTA_SETTINGS)
    def test_two_workers_different_recipients_last_unit_one_wins(self):
        bulk = _make_bulk(client_request_id="pg-quota-diff-recipients")
        row1 = _make_occurrence(bulk, source_row_number=1, recipient="a@example.com")
        row2 = _make_occurrence(bulk, source_row_number=2, recipient="b@example.com")

        results: list[str] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker():
            close_old_connections()
            barrier.wait(timeout=10)
            try:
                pk = reserve_and_claim(bulk.pk, job_id=1)
                outcome = "won" if pk is not None else "nothing_left"
            except QuotaExhausted:
                outcome = "exhausted"
            finally:
                close_old_connections()
            with results_lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(results.count("won"), 1)
        self.assertEqual(results.count("exhausted"), 1)

        row1.refresh_from_db()
        row2.refresh_from_db()
        statuses = sorted([row1.send_status, row2.send_status])
        self.assertEqual(
            statuses,
            sorted([BulkSendRecipient.SEND_SENDING, BulkSendRecipient.SEND_NOT_STARTED]),
        )

        hour_window = QuotaWindow.objects.get(window_type=QuotaWindow.WINDOW_HOUR)
        self.assertEqual(hour_window.consumed, 1)
        self.assertLessEqual(hour_window.consumed, hour_window.limit_value)


@skipUnless(POSTGRESQL, "Requiere PostgreSQL")
class TwoBulksSameLastUnitTests(TransactionTestCase):
    """#23 -- two DIFFERENT BulkSend objects, each with their own eligible
    recipient, competing for the SAME global hourly QuotaWindow. Proves
    quota is genuinely account-wide, not per-bulk: only one of the two
    bulks' recipients gets claimed."""

    @override_settings(**QUOTA_SETTINGS)
    def test_two_different_bulks_share_the_same_hourly_window(self):
        bulk_a = _make_bulk(client_request_id="pg-quota-bulk-a")
        bulk_b = _make_bulk(client_request_id="pg-quota-bulk-b")
        row_a = _make_occurrence(bulk_a, source_row_number=1, recipient="a@example.com")
        row_b = _make_occurrence(bulk_b, source_row_number=1, recipient="b@example.com")

        results: list[str] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(bulk_id: int):
            close_old_connections()
            barrier.wait(timeout=10)
            try:
                pk = reserve_and_claim(bulk_id, job_id=1)
                outcome = "won" if pk is not None else "nothing_left"
            except QuotaExhausted:
                outcome = "exhausted"
            finally:
                close_old_connections()
            with results_lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=worker, args=(bulk_a.pk,)),
            threading.Thread(target=worker, args=(bulk_b.pk,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(results.count("won"), 1)
        self.assertEqual(results.count("exhausted"), 1)

        row_a.refresh_from_db()
        row_b.refresh_from_db()
        statuses = sorted([row_a.send_status, row_b.send_status])
        self.assertEqual(
            statuses,
            sorted([BulkSendRecipient.SEND_SENDING, BulkSendRecipient.SEND_NOT_STARTED]),
        )

        # Exactly one QuotaWindow set exists (shared across both bulks --
        # QuotaWindow has no bulk_send FK at all).
        hour_window = QuotaWindow.objects.get(window_type=QuotaWindow.WINDOW_HOUR)
        self.assertEqual(hour_window.consumed, 1)
        self.assertLessEqual(hour_window.consumed, hour_window.limit_value)
