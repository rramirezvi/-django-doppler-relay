from __future__ import annotations

from decimal import Decimal
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from relay.models import BulkSend, BulkSendRecipient
from relay.services.bulk_import import (
    BulkImportError,
    BulkImportService,
    canonicalize_payload,
    cleanup_stale_spools,
    occurrence_idempotency_key,
)
from relay.services.bulk_progress import get_bulk_import_progress


class BulkImportCanonicalizationTests(TestCase):
    def test_hash_depends_only_on_logical_canonical_payload(self):
        first, first_bytes, first_hash = canonicalize_payload(
            {"b": 1.0, "a": "Cafe\u0301", "__worker": "ignored"}
        )
        second, second_bytes, second_hash = canonicalize_payload(
            {"a": "Caf\u00e9", "b": Decimal("1.000")}
        )
        self.assertEqual(first, second)
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(first_hash, second_hash)

    def test_occurrence_identity_includes_row_and_bulk(self):
        payload_hash = canonicalize_payload({"x": "y"})[2]
        first = occurrence_idempotency_key(
            bulk_send_id=1,
            import_version=1,
            source_row_number=1,
            payload_hash=payload_hash,
        )
        self.assertNotEqual(
            first,
            occurrence_idempotency_key(
                bulk_send_id=1,
                import_version=1,
                source_row_number=2,
                payload_hash=payload_hash,
            ),
        )
        self.assertEqual(
            first,
            occurrence_idempotency_key(
                bulk_send_id=1,
                import_version=1,
                source_row_number=1,
                payload_hash=payload_hash,
            ),
        )

    def test_numeric_and_unicode_golden_cases(self):
        equivalents = [1, 1.0, Decimal("1.000")]
        one_results = [canonicalize_payload({"value": value}) for value in equivalents]
        self.assertEqual({item[1] for item in one_results}, {b'{"value":1}'})
        self.assertEqual(
            {item[2] for item in one_results},
            {"48208f9428d64634bd8e28ff345bf0eab60d53c18fa2fbdb0b9bc1e84df2b5f6"},
        )

        negative_zero = [-0, -0.0, Decimal("-0.000")]
        zero_results = [
            canonicalize_payload({"value": value}) for value in negative_zero
        ]
        self.assertEqual({item[1] for item in zero_results}, {b'{"value":0}'})
        self.assertEqual(
            {item[2] for item in zero_results},
            {"23d7b286bd429460b92a2a1c21b6afc34110446c5034c17363fda363aa0a7c5d"},
        )

        self.assertNotEqual(
            canonicalize_payload({"value": True})[1],
            canonicalize_payload({"value": 1})[1],
        )
        self.assertNotEqual(
            canonicalize_payload({"value": False})[1],
            canonicalize_payload({"value": 0})[1],
        )
        self.assertEqual(
            canonicalize_payload({"value": None})[1],
            b'{"value":null}',
        )
        self.assertEqual(
            canonicalize_payload({"value": "Cafe\u0301"})[1],
            '{"value":"Caf\u00e9"}'.encode("utf-8"),
        )

    def test_large_and_exponential_numbers_are_canonical(self):
        self.assertEqual(
            canonicalize_payload({"value": 10**40})[1],
            ('{"value":' + str(10**40) + "}").encode("ascii"),
        )
        self.assertEqual(
            canonicalize_payload({"value": Decimal("1E+3")})[1],
            b'{"value":1000}',
        )
        self.assertEqual(
            canonicalize_payload({"value": Decimal("1E-3")})[1],
            b'{"value":0.001}',
        )

    def test_unrepresentable_decimal_precision_is_rejected(self):
        with self.assertRaises(BulkImportError) as error:
            canonicalize_payload(
                {"value": Decimal("1.2345678901234567890123456789")}
            )
        self.assertEqual(
            error.exception.code,
            "payload_number_precision_unsupported",
        )


@override_settings(
    BULK_PROCESSING_V2_IMPORT_BATCH_SIZE=2,
    BULK_PROCESSING_V2_MAX_FILE_BYTES=1024 * 1024,
    BULK_PROCESSING_V2_MAX_ROWS=100,
)
class BulkImportServiceTests(TestCase):
    def make_bulk(self, content: bytes, **kwargs):
        return BulkSend.objects.create(
            template_id="tpl",
            template_name="Template",
            recipients_file=SimpleUploadedFile("rows.csv", content),
            engine_version=BulkSend.ENGINE_V2,
            **kwargs,
        )

    def test_import_preserves_order_multiplicity_payload_and_invalid_rows(self):
        bulk = self.make_bulk(
            "\ufeffemail;nombre;saldo\r\n"
            "same@example.com;Ana;10\r\n"
            "same@example.com;Ana;10\r\n"
            "bad-address;José;20\r\n"
            ";Sin correo;30\r\n".encode("utf-8")
        )
        result = BulkImportService(bulk).import_file()
        rows = list(bulk.recipient_occurrences.order_by("source_row_number"))

        self.assertEqual(result.total_rows, 4)
        self.assertEqual(result.valid_rows, 2)
        self.assertEqual(result.invalid_rows, 2)
        self.assertEqual([row.source_row_number for row in rows], [1, 2, 3, 4])
        self.assertEqual(rows[0].recipient, rows[1].recipient)
        self.assertEqual(rows[0].payload, rows[1].payload)
        self.assertEqual(rows[0].payload_hash, rows[1].payload_hash)
        self.assertNotEqual(rows[0].idempotency_key, rows[1].idempotency_key)
        self.assertEqual(
            [row.last_error_code for row in rows[2:]],
            ["recipient_invalid", "recipient_empty"],
        )
        bulk.refresh_from_db()
        self.assertEqual(bulk.import_status, BulkSend.IMPORT_READY_WITH_ERRORS)
        self.assertEqual((bulk.imported_rows, bulk.valid_rows, bulk.invalid_rows), (4, 2, 2))

    def test_mapping_excludes_operational_fields_and_marks_missing_column(self):
        bulk = self.make_bulk(
            b"email,name\nvalid@example.com,Ana\n",
            variables={
                "customer": "name",
                "required": "missing",
                "__sender_user_config_id": 3,
            },
        )
        BulkImportService(bulk).import_file()
        row = bulk.recipient_occurrences.get()
        self.assertEqual(row.status, BulkSendRecipient.STATUS_INVALID)
        self.assertEqual(row.last_error_code, "required_value_missing")
        self.assertEqual(row.payload, {"customer": "Ana", "required": None})

    def test_invalid_encoding_aborts_without_occurrences(self):
        bulk = self.make_bulk(b"email\ninvalid-\xff\n")
        with self.assertRaisesRegex(BulkImportError, "UTF-8"):
            BulkImportService(bulk).import_file()
        bulk.refresh_from_db()
        self.assertEqual(bulk.import_status, BulkSend.IMPORT_ERROR)
        self.assertEqual(bulk.recipient_occurrences.count(), 0)

    def test_missing_email_header_aborts_without_occurrences(self):
        bulk = self.make_bulk(b"name\nAna\n")
        with self.assertRaises(BulkImportError) as error:
            BulkImportService(bulk).import_file()
        self.assertEqual(error.exception.code, "email_column_missing")
        self.assertEqual(bulk.recipient_occurrences.count(), 0)

    @override_settings(BULK_PROCESSING_V2_MAX_ROWS=1)
    def test_row_limit_aborts_without_occurrences(self):
        bulk = self.make_bulk(
            b"email\na@example.com\nb@example.com\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "relay.services.bulk_import.tempfile.tempdir",
                temp_dir,
            ):
                with self.assertRaises(BulkImportError) as error:
                    BulkImportService(bulk).import_file()
            self.assertEqual(
                list(Path(temp_dir).glob("bulk-import-*.jsonl")),
                [],
            )
        self.assertEqual(error.exception.code, "row_limit_exceeded")
        self.assertEqual(bulk.recipient_occurrences.count(), 0)

    def test_same_import_version_cannot_be_repeated(self):
        bulk = self.make_bulk(b"email\na@example.com\n")
        BulkImportService(bulk).import_file()
        with self.assertRaises(BulkImportError) as error:
            BulkImportService(bulk).import_file()
        self.assertEqual(error.exception.code, "import_already_started")
        self.assertEqual(bulk.recipient_occurrences.count(), 1)
        bulk.refresh_from_db()
        self.assertEqual(bulk.import_status, BulkSend.IMPORT_READY)

    def test_explicit_next_import_version_preserves_prior_occurrences(self):
        bulk = self.make_bulk(b"email\na@example.com\n")
        BulkImportService(bulk).import_file()
        second = BulkImportService(
            bulk,
            import_version=2,
            allow_new_version=True,
        ).import_file()
        self.assertEqual(second.import_version, 2)
        self.assertEqual(
            list(
                bulk.recipient_occurrences.order_by(
                    "import_version"
                ).values_list("import_version", flat=True)
            ),
            [1, 2],
        )

    @override_settings(BULK_PROCESSING_V2_IMPORT_BATCH_SIZE=1)
    def test_database_error_rolls_back_all_occurrences(self):
        bulk = self.make_bulk(
            b"email\na@example.com\nb@example.com\n"
        )
        original = BulkSendRecipient.objects.bulk_create
        calls = 0

        def fail_second_batch(objects, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated database failure")
            return original(objects, **kwargs)

        with patch.object(
            BulkSendRecipient.objects,
            "bulk_create",
            side_effect=fail_second_batch,
        ):
            with self.assertRaises(BulkImportError) as error:
                BulkImportService(bulk).import_file()

        self.assertEqual(error.exception.code, "internal_import_error")
        self.assertEqual(bulk.recipient_occurrences.count(), 0)
        bulk.refresh_from_db()
        self.assertEqual(bulk.import_status, BulkSend.IMPORT_ERROR)

    def test_progress_can_be_reconstructed_from_database(self):
        bulk = self.make_bulk(
            b"email,name\na@example.com,A\ninvalid,B\n"
        )
        BulkImportService(bulk).import_file()
        progress = get_bulk_import_progress(bulk, reconcile=True)
        self.assertEqual(
            (
                progress.total_rows,
                progress.valid_rows,
                progress.invalid_rows,
                progress.pending_rows,
            ),
            (2, 1, 1, 1),
        )
        self.assertTrue(progress.reconciled)

    @override_settings(BULK_PROCESSING_V2_SPOOL_MAX_AGE_SECONDS=3600)
    def test_stale_spool_cleanup_is_scoped_and_preserves_fresh_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stale = root / "bulk-import-stale.jsonl"
            fresh = root / "bulk-import-fresh.jsonl"
            unrelated = root / "other.jsonl"
            stale.write_text('{"old":true}\n', encoding="utf-8")
            fresh.write_text('{"new":true}\n', encoding="utf-8")
            unrelated.write_text("keep\n", encoding="utf-8")
            old = time.time() - 7200
            os.utime(stale, (old, old))

            with patch("relay.services.bulk_import.tempfile.gettempdir", return_value=temp_dir):
                removed = cleanup_stale_spools()

            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())

    def test_spool_is_removed_after_success(self):
        bulk = self.make_bulk(b"email\na@example.com\n")
        observed_path = None
        original = BulkImportService._persist_spool

        def inspect_spool(service, spool_path, **kwargs):
            nonlocal observed_path
            observed_path = spool_path
            self.assertTrue(spool_path.exists())
            return original(service, spool_path, **kwargs)

        with patch.object(BulkImportService, "_persist_spool", new=inspect_spool):
            BulkImportService(bulk).import_file()
        self.assertIsNotNone(observed_path)
        self.assertFalse(observed_path.exists())
