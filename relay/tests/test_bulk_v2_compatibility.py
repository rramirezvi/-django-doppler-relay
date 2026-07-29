from __future__ import annotations

from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from relay.management.commands.process_bulk_scheduled import Command
from relay.models import BackgroundJob, BulkSend
from relay.services.bulk_processing import process_bulk_id


class BulkV2CompatibilityTests(TestCase):
    def make_bulk(self, *, engine=BulkSend.ENGINE_LEGACY, **kwargs):
        return BulkSend.objects.create(
            template_id="tpl",
            template_name="Template",
            recipients_file=SimpleUploadedFile(
                "rows.csv", b"email\na@example.com\n"
            ),
            engine_version=engine,
            **kwargs,
        )

    def test_existing_bulk_defaults_to_legacy(self):
        bulk = BulkSend.objects.create(
            template_id="tpl",
            template_name="Template",
            recipients_file=SimpleUploadedFile(
                "rows.csv", b"email\na@example.com\n"
            ),
        )
        self.assertEqual(bulk.engine_version, BulkSend.ENGINE_LEGACY)
        self.assertEqual(bulk.import_status, BulkSend.IMPORT_NOT_STARTED)

    @patch("relay.services.bulk_processing.process_bulk_template_send")
    @patch("relay.services.bulk_processing.DopplerRelayClient.get_template_fields")
    def test_legacy_processing_path_is_unchanged(self, template_fields, send):
        template_fields.return_value = {"variables": []}
        send.return_value = [{"status": "ok"}]
        bulk = self.make_bulk()
        process_bulk_id(bulk.pk)
        send.assert_called_once()

    @patch("relay.services.bulk_processing.process_bulk_template_send")
    def test_legacy_worker_rejects_v2_without_sending(self, send):
        bulk = self.make_bulk(engine=BulkSend.ENGINE_V2)
        with self.assertRaisesRegex(ValueError, "worker legacy"):
            process_bulk_id(bulk.pk)
        send.assert_not_called()

    @override_settings(BULK_PROCESSING_ENGINE_V2=True)
    def test_scheduler_does_not_enqueue_v2(self):
        from django.utils import timezone

        bulk = self.make_bulk(
            engine=BulkSend.ENGINE_V2,
            scheduled_at=timezone.now(),
        )
        command = Command()
        command.handle()
        self.assertFalse(
            BackgroundJob.objects.filter(bulk=bulk).exists()
        )
