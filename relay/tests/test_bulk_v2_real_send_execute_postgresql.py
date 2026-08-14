"""bulk-v2 real-send authorization/execution -- genuinely concurrent
locking tests (PR C2, design rounds 9-12). PostgreSQL-only.

Mirrors relay/tests/test_bulk_quota_postgresql.py's pattern exactly:
select_for_update()/FOR UPDATE has no real blocking guarantee on SQLite,
so correctness here rests entirely on real PostgreSQL row locks. Real
threads, real separate DB connections (close_old_connections() per
worker), no mocking of select_for_update/transaction.atomic() -- only
`execute_specific_queued_job`/transport are ever patched, and only to
force deterministic interleaving, never to fake locking behavior.
"""

from __future__ import annotations

import threading
import uuid
from unittest import mock, skipUnless

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings

from relay.models import BackgroundJob, BulkSend, BulkSendRecipient
from relay.services import jobs as jobs_module
from relay.services.bulk_v2_real_send_execute import authorize_and_execute_real_send
from relay.tests._bulk_v2_real_send_support import FakeDopplerResponse

POSTGRESQL = connection.vendor == "postgresql"

_DOPPLER_RELAY_TEST_CFG = dict(
    API_KEY="test-key", ACCOUNT_ID=1, AUTH_SCHEME="Bearer",
    BASE_URL="https://api.dopplerrelay.com/",
    DEFAULT_FROM_EMAIL="noreply@example.com", DEFAULT_FROM_NAME="PR C2 Test",
    TIMEOUT=30,
)


def _make_bulk(*, client_request_id: str, user) -> BulkSend:
    return BulkSend.objects.create(
        template_id="tpl-real",
        template_name="Template real",
        client_request_id=client_request_id,
        recipients_file=SimpleUploadedFile("rows.csv", b"email,name\na@example.com,A\n"),
        engine_version=BulkSend.ENGINE_V2,
        import_status=BulkSend.IMPORT_READY,
        scheduled_by=user,
    )


def _make_occurrence(bulk: BulkSend) -> BulkSendRecipient:
    return BulkSendRecipient.objects.create(
        bulk_send=bulk, import_version=1, source_row_number=1,
        recipient="a@example.com", normalized_recipient="a@example.com",
        payload={"name": "A"}, payload_hash="a" * 64,
        idempotency_key=uuid.uuid4(),
    )


def _authorized_settings(*, user, bulk, domain="example.com"):
    return dict(
        BULK_PROCESSING_V2_REAL_SEND_ENABLED=True,
        BULK_PROCESSING_V2_REAL_SEND_USER_IDS=str(user.pk),
        BULK_PROCESSING_V2_REAL_SEND_REQUEST_IDS=bulk.client_request_id,
        BULK_PROCESSING_V2_REAL_SEND_TEMPLATE_IDS=bulk.template_id,
        BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS=domain,
        BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS=1,
        DOPPLER_RELAY=_DOPPLER_RELAY_TEST_CFG,
    )


def _make_user():
    from django.contrib.auth.models import User
    return User.objects.create_user(
        username=f"pr-c2-user-{uuid.uuid4().hex[:8]}", password="unused"
    )


@skipUnless(POSTGRESQL, "Requiere PostgreSQL")
class TwoConcurrentAuthorizationsTests(TransactionTestCase):
    """#1 -- TOCTOU close: two full, real, concurrent
    authorize_and_execute_real_send() calls for the SAME BulkSend.
    Exactly one BackgroundJob must exist afterward."""

    def test_exactly_one_job_created_under_real_concurrency(self):
        user = _make_user()
        bulk = _make_bulk(client_request_id="pgc2-toctou", user=user)
        _make_occurrence(bulk)

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)
        settings_kwargs = _authorized_settings(user=user, bulk=bulk)

        def worker():
            close_old_connections()
            barrier.wait(timeout=10)
            with mock.patch("requests.Session.request", return_value=FakeDopplerResponse()), \
                    override_settings(**settings_kwargs):
                outcome = authorize_and_execute_real_send(bulk.pk)
            with results_lock:
                results.append(outcome.result)
            close_old_connections()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(sorted(results), sorted(["executed_here", "refused"]))
        self.assertEqual(
            BackgroundJob.objects.filter(bulk=bulk, job_type=BackgroundJob.TYPE_BULK_SEND_V2_REAL).count(),
            1,
        )


@skipUnless(POSTGRESQL, "Requiere PostgreSQL")
class CrashRecoveryTests(TransactionTestCase):
    """#3 -- job left `queued` (simulated crash: the claim step never
    even attempts) is recoverable by the exact function the already-
    running continuous worker uses, with zero extra machinery."""

    def test_job_left_queued_is_recoverable_by_existing_worker(self):
        user = _make_user()
        bulk = _make_bulk(client_request_id="pgc2-crash", user=user)
        _make_occurrence(bulk)
        settings_kwargs = _authorized_settings(user=user, bulk=bulk)

        with mock.patch(
            "relay.services.bulk_v2_real_send_execute.execute_specific_queued_job",
            return_value=None,
        ):
            with override_settings(**settings_kwargs):
                outcome = authorize_and_execute_real_send(bulk.pk)

        self.assertEqual(outcome.result, "delegated")
        job = BackgroundJob.objects.get(bulk=bulk, job_type=BackgroundJob.TYPE_BULK_SEND_V2_REAL)
        self.assertEqual(job.state, BackgroundJob.STATE_QUEUED)

        recovered = jobs_module.claim_next_job()
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.pk, job.pk)
        self.assertEqual(recovered.state, BackgroundJob.STATE_RUNNING)
        self.assertEqual(recovered.attempts, 1)


@skipUnless(POSTGRESQL, "Requiere PostgreSQL")
class CommandVsWorkerRaceTests(TransactionTestCase):
    """#4 -- command (via authorize_and_execute_real_send) and the
    continuous worker (claim_next_job()) racing for the SAME job_id.
    Deterministic interleaving via threading.Event (avoids CI flakiness)
    while still exercising real PostgreSQL row locks for the actual
    claim -- exactly one must win."""

    def test_worker_wins_the_race_authorizing_caller_delegates(self):
        user = _make_user()
        bulk = _make_bulk(client_request_id="pgc2-race", user=user)
        _make_occurrence(bulk)
        settings_kwargs = _authorized_settings(user=user, bulk=bulk)

        job_created = threading.Event()
        proceed = threading.Event()
        real_execute = jobs_module.execute_specific_queued_job

        def delayed_execute(job_id):
            job_created.set()
            proceed.wait(timeout=10)
            return real_execute(job_id)

        outcome_holder = {}

        def run_authorize():
            close_old_connections()
            with mock.patch(
                "relay.services.bulk_v2_real_send_execute.execute_specific_queued_job",
                side_effect=delayed_execute,
            ), mock.patch("requests.Session.request", return_value=FakeDopplerResponse()), \
                    override_settings(**settings_kwargs):
                outcome_holder["outcome"] = authorize_and_execute_real_send(bulk.pk)
            close_old_connections()

        t = threading.Thread(target=run_authorize)
        t.start()
        self.assertTrue(job_created.wait(timeout=10), "Check 14a nunca comiteo el job")

        job = BackgroundJob.objects.get(bulk=bulk, job_type=BackgroundJob.TYPE_BULK_SEND_V2_REAL)
        self.assertEqual(job.state, BackgroundJob.STATE_QUEUED)

        close_old_connections()
        worker_claimed = jobs_module.claim_next_job()  # real Postgres CAS, real race resolved here
        self.assertIsNotNone(worker_claimed)
        self.assertEqual(worker_claimed.pk, job.pk)

        proceed.set()
        t.join(timeout=20)

        outcome = outcome_holder["outcome"]
        self.assertEqual(outcome.result, "delegated")
        job.refresh_from_db()
        self.assertEqual(job.attempts, 1, "el job nunca debe ser reclamado dos veces")
        self.assertEqual(job.state, BackgroundJob.STATE_RUNNING)


@skipUnless(POSTGRESQL, "Requiere PostgreSQL")
class BulkSendLockReleasedBeforeDispatchTests(TransactionTestCase):
    """#2 -- the BulkSend row lock must be released before any dispatch/
    transport occurs. While one thread is 'inside Doppler I/O' (a slow
    mocked transport call), a separate connection must be able to
    select_for_update() the same BulkSend row immediately."""

    def test_bulksend_lock_free_while_dispatch_is_in_flight(self):
        user = _make_user()
        bulk = _make_bulk(client_request_id="pgc2-lockfree", user=user)
        _make_occurrence(bulk)
        settings_kwargs = _authorized_settings(user=user, bulk=bulk)

        dispatch_started = threading.Event()
        release_dispatch = threading.Event()

        def slow_transport(*args, **kwargs):
            dispatch_started.set()
            release_dispatch.wait(timeout=10)
            return FakeDopplerResponse()

        def run_authorize():
            close_old_connections()
            with mock.patch("requests.Session.request", side_effect=slow_transport), \
                    override_settings(**settings_kwargs):
                authorize_and_execute_real_send(bulk.pk)
            close_old_connections()

        t = threading.Thread(target=run_authorize)
        t.start()
        self.assertTrue(dispatch_started.wait(timeout=10), "el transporte nunca arranco")

        # Probe from a fresh connection: must succeed immediately, proving
        # the BulkSend lock was already released before dispatch began.
        import time
        from django.db import transaction as django_transaction

        close_old_connections()
        started = time.monotonic()
        with django_transaction.atomic():
            row = BulkSend.objects.select_for_update().filter(pk=bulk.pk).first()
        elapsed = time.monotonic() - started
        close_old_connections()

        release_dispatch.set()
        t.join(timeout=20)

        self.assertIsNotNone(row)
        self.assertLess(elapsed, 1.0, f"el probe tardo {elapsed}s -- sugiere que el lock seguia activo")
