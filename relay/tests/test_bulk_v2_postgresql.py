from __future__ import annotations

import threading
import uuid
from unittest import skipUnless

from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from relay.models import BackgroundJob, BulkSend, BulkSendRecipient
from relay.services.bulk_import import BulkImportError, BulkImportService


POSTGRESQL = connection.vendor == "postgresql"


@skipUnless(POSTGRESQL, "Requiere PostgreSQL")
@override_settings(
    BULK_PROCESSING_V2_IMPORT_BATCH_SIZE=1,
    BULK_PROCESSING_V2_MAX_FILE_BYTES=1024 * 1024,
    BULK_PROCESSING_V2_MAX_ROWS=100,
)
class BulkV2PostgreSQLTests(TransactionTestCase):
    reset_sequences = True

    def make_bulk(self):
        return BulkSend.objects.create(
            template_id="tpl",
            template_name="Template",
            recipients_file=SimpleUploadedFile(
                "rows.csv",
                b"email,name\na@example.com,A\nb@example.com,B\n",
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

    def test_database_check_constraint_rejects_invalid_status(self):
        occurrence = self.make_occurrence(self.make_bulk())
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE relay_bulksendrecipient "
                    "SET status = %s WHERE id = %s",
                    ["not-a-state", occurrence.pk],
                )
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.status, BulkSendRecipient.STATUS_PENDING)

    def test_database_rejects_duplicate_client_request_id(self):
        first = self.make_bulk()
        first.client_request_id = "postgres-unique-request"
        first.save(update_fields=["client_request_id"])
        with self.assertRaises(IntegrityError), transaction.atomic():
            BulkSend.objects.create(
                client_request_id="postgres-unique-request",
                template_id="tpl",
                template_name="Template",
                recipients_file=SimpleUploadedFile(
                    "other.csv", b"email\nother@example.com\n"
                ),
                engine_version=BulkSend.ENGINE_V2,
            )

    def test_database_rejects_zero_source_row_and_import_version(self):
        bulk = self.make_bulk()
        for column in ("source_row_number", "import_version"):
            with self.subTest(column=column):
                values = {
                    "bulk_send_id": bulk.pk,
                    "import_version": 1,
                    "source_row_number": 1,
                    "recipient": "a@example.com",
                    "normalized_recipient": "a@example.com",
                    "payload": "{}",
                    "payload_hash": "a" * 64,
                    "idempotency_key": str(uuid.uuid4()),
                    "status": "pending",
                    "last_error_code": "",
                    "last_error_message": "",
                }
                values[column] = 0
                with self.assertRaises(IntegrityError), transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            INSERT INTO relay_bulksendrecipient
                            (bulk_send_id, import_version, source_row_number,
                             recipient, normalized_recipient, payload,
                             payload_hash, idempotency_key, status,
                             last_error_code, last_error_message,
                             created_at, updated_at)
                            VALUES
                            (%(bulk_send_id)s, %(import_version)s,
                             %(source_row_number)s, %(recipient)s,
                             %(normalized_recipient)s, %(payload)s::jsonb,
                             %(payload_hash)s, %(idempotency_key)s::uuid,
                             %(status)s, %(last_error_code)s,
                             %(last_error_message)s, CURRENT_TIMESTAMP,
                             CURRENT_TIMESTAMP)
                            """,
                            values,
                        )

    def test_payload_uses_jsonb_and_round_trips_unicode_and_numbers(self):
        bulk = self.make_bulk()
        BulkImportService(bulk).import_file()
        row = bulk.recipient_occurrences.order_by("source_row_number").first()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_typeof(payload)::text "
                "FROM relay_bulksendrecipient WHERE id = %s",
                [row.pk],
            )
            payload_type = cursor.fetchone()[0]
        self.assertEqual(payload_type, "jsonb")
        self.assertEqual(row.payload, {"name": "A"})

    def test_expected_indexes_and_constraints_exist(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'relay_bulksendrecipient'
                ORDER BY indexname
                """
            )
            indexes = dict(cursor.fetchall())
            cursor.execute(
                """
                SELECT conname, contype
                FROM pg_constraint
                WHERE conrelid = 'relay_bulksendrecipient'::regclass
                ORDER BY conname
                """
            )
            constraints = dict(cursor.fetchall())

        self.assertIn("bulk_recipient_status_idx", indexes)
        self.assertIn("bulk_recipient_order_idx", indexes)
        self.assertIn(
            "relay_bulksendrecipient_idempotency_key_key",
            indexes,
        )
        self.assertEqual(constraints["uniq_bulk_import_source_row"], "u")
        self.assertEqual(constraints["bulk_recipient_valid_status"], "c")
        self.assertEqual(
            constraints["bulk_recipient_import_version_gte_1"], "c"
        )
        self.assertEqual(
            constraints["bulk_recipient_source_row_gte_1"], "c"
        )
        self.assertIn("f", constraints.values())

    def test_simultaneous_same_version_import_has_one_winner(self):
        bulk = self.make_bulk()
        barrier = threading.Barrier(2)
        results: list[tuple[str, str]] = []
        lock = threading.Lock()

        def import_same_version():
            close_old_connections()
            local_bulk = BulkSend.objects.get(pk=bulk.pk)
            barrier.wait(timeout=10)
            try:
                BulkImportService(local_bulk).import_file()
            except BulkImportError as exc:
                result = ("error", exc.code)
            else:
                result = ("ok", "")
            finally:
                close_old_connections()
            with lock:
                results.append(result)

        threads = [
            threading.Thread(target=import_same_version),
            threading.Thread(target=import_same_version),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sum(kind == "ok" for kind, _ in results), 1)
        self.assertEqual(
            [code for kind, code in results if kind == "error"],
            ["import_already_started"],
        )
        bulk.refresh_from_db()
        self.assertEqual(bulk.import_status, BulkSend.IMPORT_READY)
        self.assertEqual(bulk.recipient_occurrences.count(), 2)
        self.assertEqual(
            bulk.recipient_occurrences.values("source_row_number").distinct().count(),
            2,
        )

    def test_concurrent_api_requests_with_same_client_request_id_are_idempotent(self):
        user = User.objects.create_user(
            "postgres-canary", password="unused", is_staff=True
        )
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="relay",
                codename="change_bulksend",
            )
        )
        barrier = threading.Barrier(2)
        results: list[tuple[int, bool]] = []
        errors: list[str] = []
        lock = threading.Lock()

        def create_same_canary():
            close_old_connections()
            client = Client()
            client.force_login(User.objects.get(pk=user.pk))
            barrier.wait(timeout=10)
            try:
                response = client.post(
                    reverse("api_bulk_send_list"),
                    {
                        "template_id": "tpl",
                        "template_name": "Controlled template",
                        "engine_version": "v2",
                        "send_now": "0",
                        "client_request_id": "postgres-concurrent-canary",
                        "recipients_file": SimpleUploadedFile(
                            "rows.csv",
                            b"email,name\na@example.com,A\nb@example.com,B\n",
                        ),
                    },
                )
                result = (
                    response.status_code,
                    bool(response.json().get("duplicate", False)),
                )
                with lock:
                    results.append(result)
            except Exception as exc:  # pragma: no cover - diagnostic boundary
                with lock:
                    errors.append(type(exc).__name__)
            finally:
                close_old_connections()

        with override_settings(
            BULK_PROCESSING_ENGINE_V2=True,
            BULK_PROCESSING_V2_CANARY_ENABLED=True,
            BULK_PROCESSING_V2_CANARY_REQUEST_IDS="postgres-concurrent-canary",
            BULK_PROCESSING_V2_CANARY_USER_IDS=str(user.pk),
            BULK_PROCESSING_V2_CANARY_MAX_ROWS=20,
            BULK_PROCESSING_V2_ALLOW_EXTERNAL_TEMPLATE_LOOKUP=False,
            CONN_MAX_AGE=0,
        ):
            threads = [
                threading.Thread(target=create_same_canary),
                threading.Thread(target=create_same_canary),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(code for code, _ in results), [200, 201])
        self.assertEqual(sum(duplicate for _, duplicate in results), 1)
        bulk = BulkSend.objects.get(
            client_request_id="postgres-concurrent-canary"
        )
        self.assertEqual(bulk.recipient_occurrences.count(), 2)
        self.assertEqual(BackgroundJob.objects.count(), 0)
