from __future__ import annotations

import threading
import uuid
from unittest import skipUnless

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from relay.models import BulkSend, BulkSendRecipient
from relay.services.bulk_v2_send_state import claim_next_recipient


POSTGRESQL = connection.vendor == "postgresql"


@skipUnless(POSTGRESQL, "Requiere PostgreSQL")
class ClaimNextRecipientPostgreSQLTests(TransactionTestCase):
    """design.md §4 / spec 'Two workers race for the same eligible row':
    real threads, mirroring relay/tests/test_bulk_v2_postgresql.py:17-27's
    pattern exactly, proving the single-writer claim generally (not
    conditioned on MAX_ROWS == 1, per spec)."""

    reset_sequences = True

    def make_bulk(self):
        return BulkSend.objects.create(
            template_id="tpl",
            template_name="Template",
            recipients_file=SimpleUploadedFile(
                "rows.csv", b"email,name\na@example.com,A\n"
            ),
            engine_version=BulkSend.ENGINE_V2,
        )

    def make_occurrence(self, bulk):
        return BulkSendRecipient.objects.create(
            bulk_send=bulk,
            import_version=1,
            source_row_number=1,
            recipient="a@example.com",
            normalized_recipient="a@example.com",
            payload={"name": "A"},
            payload_hash="a" * 64,
            idempotency_key=uuid.uuid4(),
        )

    def test_two_workers_race_for_the_same_row(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)

        results: list[int | None] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(job_id: int):
            close_old_connections()
            barrier.wait(timeout=10)
            try:
                claimed = claim_next_recipient(bulk.pk, job_id=job_id)
            finally:
                close_old_connections()
            with lock:
                results.append(claimed)

        threads = [
            threading.Thread(target=worker, args=(1,)),
            threading.Thread(target=worker, args=(2,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        winners = [pk for pk in results if pk is not None]
        losers = [pk for pk in results if pk is None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0], row.pk)
        self.assertEqual(len(losers), 1)

        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENDING)
        self.assertEqual(row.send_attempt_number, 1)
        self.assertIn(row.send_job_id, (1, 2))
