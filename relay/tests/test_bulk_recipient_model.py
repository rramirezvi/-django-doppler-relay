from __future__ import annotations

import uuid
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase

from relay.models import BulkSend, BulkSendRecipient


class BulkSendRecipientModelTests(TestCase):
    def make_bulk(self, **kwargs):
        return BulkSend.objects.create(
            template_id="tpl",
            template_name="Template",
            recipients_file=SimpleUploadedFile("rows.csv", b"email\na@example.com\n"),
            engine_version=BulkSend.ENGINE_V2,
            **kwargs,
        )

    def test_v2_requires_local_template_name_without_doppler_lookup(self):
        with patch(
            "relay.services.doppler_relay.DopplerRelayClient",
            side_effect=AssertionError("Doppler must not be instantiated"),
        ):
            with self.assertRaisesRegex(ValueError, "template_name"):
                BulkSend.objects.create(
                    template_id="tpl",
                    recipients_file=SimpleUploadedFile(
                        "rows.csv", b"email\na@example.com\n"
                    ),
                    engine_version=BulkSend.ENGINE_V2,
                )

    def make_occurrence(self, bulk, **kwargs):
        values = {
            "import_version": 1,
            "source_row_number": 1,
            "recipient": "a@example.com",
            "normalized_recipient": "a@example.com",
            "payload": {"name": "A"},
            "payload_hash": "a" * 64,
            "idempotency_key": uuid.uuid4(),
        }
        values.update(kwargs)
        return BulkSendRecipient.objects.create(bulk_send=bulk, **values)

    def test_legitimate_multiplicity_is_preserved(self):
        bulk = self.make_bulk()
        first = self.make_occurrence(bulk)
        second = self.make_occurrence(
            bulk,
            source_row_number=2,
            idempotency_key=uuid.uuid4(),
        )
        self.assertEqual(first.recipient, second.recipient)
        self.assertEqual(bulk.recipient_occurrences.count(), 2)

    def test_duplicate_source_row_is_rejected(self):
        bulk = self.make_bulk()
        self.make_occurrence(bulk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_occurrence(bulk, idempotency_key=uuid.uuid4())

    def test_duplicate_idempotency_key_is_rejected(self):
        bulk = self.make_bulk()
        key = uuid.uuid4()
        self.make_occurrence(bulk, idempotency_key=key)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_occurrence(
                bulk,
                source_row_number=2,
                idempotency_key=key,
            )

    def test_source_position_is_immutable(self):
        occurrence = self.make_occurrence(self.make_bulk())
        occurrence.source_row_number = 2
        with self.assertRaisesRegex(ValueError, "inmutables"):
            occurrence.save()

    def test_engine_version_is_immutable_after_import_started(self):
        bulk = self.make_bulk(import_status=BulkSend.IMPORT_READY)
        bulk.engine_version = BulkSend.ENGINE_LEGACY
        with self.assertRaisesRegex(ValueError, "engine_version"):
            bulk.save()

    def test_deleting_bulk_cascades_occurrences(self):
        bulk = self.make_bulk()
        occurrence = self.make_occurrence(bulk)
        bulk.delete()
        self.assertFalse(
            BulkSendRecipient.objects.filter(pk=occurrence.pk).exists()
        )
