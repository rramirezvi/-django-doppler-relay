"""PR2b-T27 — Scenario 6: two concurrent workers race for the same
eligible row (PostgreSQL-only). Mirrors
relay/tests/test_bulk_v2_postgresql.py:17-27's pattern exactly, per
design.md §4/§17 risk 4 (select_for_update(skip_locked=True) is not a
guarantee on SQLite; correctness rests on the compare-and-set, race tests
are PostgreSQL-only per the existing repo convention).

Skipped on this Windows/SQLite dev machine, as expected — same as
PR2a's test_bulk_v2_send_state_postgresql.py.
"""

from __future__ import annotations

import threading
import uuid
from unittest import mock, skipUnless

from django.conf import settings as django_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings

from relay.models import BulkSend, BulkSendRecipient
from relay.services.bulk_v2_send import process_bulk_id_v2
from relay.tests._bulk_v2_real_send_support import FakeDopplerResponse

POSTGRESQL = connection.vendor == "postgresql"


@skipUnless(POSTGRESQL, "Requiere PostgreSQL")
class TwoWorkersRaceRealSendTests(TransactionTestCase):
    reset_sequences = True

    def make_bulk(self):
        return BulkSend.objects.create(
            template_id="tpl-real",
            template_name="Template real",
            client_request_id="postgres-real-send-race",
            recipients_file=SimpleUploadedFile(
                "rows.csv", b"email,name\na@example.com,A\n"
            ),
            engine_version=BulkSend.ENGINE_V2,
            import_status=BulkSend.IMPORT_READY,
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

    def test_two_workers_race_for_same_row_at_most_one_doppler_call(self):
        bulk = self.make_bulk()
        row = self.make_occurrence(bulk)

        call_count_lock = threading.Lock()
        call_count = {"n": 0}

        def fake_request(*args, **kwargs):
            with call_count_lock:
                call_count["n"] += 1
            return FakeDopplerResponse()

        results: list[str] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        doppler_cfg = dict(django_settings.DOPPLER_RELAY)
        doppler_cfg["DEFAULT_FROM_EMAIL"] = doppler_cfg.get("DEFAULT_FROM_EMAIL") or "noreply@example.com"

        def worker(job_id: int):
            close_old_connections()
            barrier.wait(timeout=10)
            with mock.patch("requests.Session.request", side_effect=fake_request), \
                    override_settings(DOPPLER_RELAY=doppler_cfg):
                try:
                    outcome = process_bulk_id_v2(bulk.pk, job_id=job_id)
                finally:
                    close_old_connections()
            with results_lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=worker, args=(1,)),
            threading.Thread(target=worker, args=(2,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(call_count["n"], 1)

        row.refresh_from_db()
        self.assertEqual(row.send_status, BulkSendRecipient.SEND_SENT)
