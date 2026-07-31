from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.middleware.csrf import _get_new_csrf_string
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from relay.models import BackgroundJob, BulkSend, BulkSendRecipient


@override_settings(DOPPLER_RELAY={"ACCOUNT_ID": 0})
class BulkV2ApiTests(TestCase):
    def setUp(self):
        self.url = reverse("api_bulk_send_list")
        self.user = User.objects.create_user(
            "operator", password="unused", is_staff=True
        )
        self.user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="relay",
                codename="change_bulksend",
            )
        )
        self.client.force_login(self.user)
        self.canary_settings = override_settings(
            BULK_PROCESSING_V2_CANARY_ENABLED=True,
            BULK_PROCESSING_V2_CANARY_REQUEST_IDS="request-v2",
            BULK_PROCESSING_V2_CANARY_USER_IDS=str(self.user.pk),
            BULK_PROCESSING_V2_CANARY_MAX_ROWS=20,
            BULK_PROCESSING_V2_ALLOW_EXTERNAL_TEMPLATE_LOOKUP=False,
        )
        self.canary_settings.enable()
        self.addCleanup(self.canary_settings.disable)

    def payload(self, *, request_id="request-v2", rows=2, **extra):
        body = "email,name\n" + "".join(
            f"user-{index}@example.com,User {index}\n" for index in range(rows)
        )
        data = {
            "template_id": "tpl",
            "template_name": "Controlled template",
            "engine_version": "v2",
            "send_now": "0",
            "client_request_id": request_id,
            "recipients_file": SimpleUploadedFile(
                "rows.csv",
                body.encode("utf-8"),
                content_type="text/csv",
            ),
        }
        data.update(extra)
        return data

    def test_anonymous_request_is_not_authorized(self):
        anonymous = Client(enforce_csrf_checks=True)
        token = _get_new_csrf_string()
        anonymous.cookies["csrftoken"] = token
        response = anonymous.post(
            self.url,
            self.payload(),
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(BulkSend.objects.count(), 0)

    @override_settings(BULK_PROCESSING_ENGINE_V2=True)
    def test_staff_without_permission_cannot_create_v2(self):
        user = User.objects.create_user(
            "no-permission", password="unused", is_staff=True
        )
        self.client.force_login(user)
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(BulkSend.objects.count(), 0)
        capability = self.client.get(self.url).json()["capabilities"]
        self.assertFalse(capability["bulk_processing_v2_create"])

    @override_settings(BULK_PROCESSING_ENGINE_V2=True)
    def test_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(BulkSend.objects.count(), 0)

    @override_settings(BULK_PROCESSING_ENGINE_V2=False)
    def test_v2_creation_requires_feature_flag(self):
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(BulkSend.objects.count(), 0)

    @override_settings(BULK_PROCESSING_ENGINE_V2=True)
    def test_authorized_operator_can_import_v2_without_job_or_send(self):
        with patch(
            "relay.services.doppler_relay.DopplerRelayClient",
            side_effect=AssertionError("Doppler must not be instantiated"),
        ):
            response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["bulk"]["engine_version"], "v2")
        self.assertEqual(body["bulk"]["import_status"], "ready")
        self.assertEqual(body["bulk"]["import"]["total_rows"], 2)
        self.assertEqual(body["bulk"]["import"]["valid_rows"], 2)
        self.assertEqual(body["bulk"]["import"]["invalid_rows"], 0)
        self.assertEqual(BackgroundJob.objects.count(), 0)

    @override_settings(BULK_PROCESSING_ENGINE_V2=True)
    def test_v2_send_now_is_rejected(self):
        response = self.client.post(
            self.url,
            self.payload(send_now="1"),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(BulkSend.objects.count(), 0)

    @override_settings(BULK_PROCESSING_ENGINE_V2=True)
    def test_v2_scheduling_is_rejected_without_creating_bulk_or_job(self):
        response = self.client.post(
            self.url,
            self.payload(scheduled_at="2026-08-01T10:00:00"),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(BulkSend.objects.count(), 0)
        self.assertEqual(BackgroundJob.objects.count(), 0)

    @override_settings(
        BULK_PROCESSING_ENGINE_V2=True,
        BULK_PROCESSING_V2_MAX_FILE_BYTES=8,
    )
    def test_file_size_limit_is_enforced(self):
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "file_too_large")
        bulk = BulkSend.objects.get()
        self.assertEqual(bulk.import_status, BulkSend.IMPORT_ERROR)
        self.assertEqual(bulk.recipient_occurrences.count(), 0)

    @override_settings(BULK_PROCESSING_ENGINE_V2=True)
    def test_same_client_request_id_returns_existing_v2(self):
        first = self.client.post(self.url, self.payload())
        second = self.client.post(self.url, self.payload())
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(BulkSend.objects.count(), 1)
        self.assertEqual(
            BulkSend.objects.get().recipient_occurrences.count(),
            2,
        )

    @override_settings(BULK_PROCESSING_ENGINE_V2=True)
    def test_ten_and_twenty_rows_are_allowed(self):
        for request_id, rows in (("request-v2", 10), ("request-v2-20", 20)):
            with override_settings(
                BULK_PROCESSING_V2_CANARY_REQUEST_IDS=(
                    "request-v2,request-v2-20"
                )
            ):
                response = self.client.post(
                    self.url, self.payload(request_id=request_id, rows=rows)
                )
            self.assertEqual(response.status_code, 201)
            bulk = BulkSend.objects.get(client_request_id=request_id)
            self.assertEqual(bulk.recipient_occurrences.count(), rows)

    @override_settings(BULK_PROCESSING_ENGINE_V2=True)
    def test_twenty_one_rows_fail_before_any_persistence(self):
        with patch(
            "relay.services.doppler_relay.DopplerRelayClient",
            side_effect=AssertionError("Doppler must not be instantiated"),
        ):
            response = self.client.post(self.url, self.payload(rows=21))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "row_limit_exceeded")
        self.assertEqual(BulkSend.objects.count(), 0)
        self.assertEqual(BulkSendRecipient.objects.count(), 0)
        self.assertEqual(BackgroundJob.objects.count(), 0)

    @override_settings(
        BULK_PROCESSING_ENGINE_V2=True,
        BULK_PROCESSING_V2_CANARY_ENABLED=False,
    )
    def test_engine_true_canary_false_fails_closed(self):
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "canary_disabled")
        self.assertEqual(BulkSend.objects.count(), 0)

    @override_settings(BULK_PROCESSING_ENGINE_V2=True)
    def test_request_not_in_allowlist_fails_closed(self):
        response = self.client.post(
            self.url, self.payload(request_id="not-authorized")
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "request_not_allowed")
        self.assertEqual(BulkSend.objects.count(), 0)

    @override_settings(
        BULK_PROCESSING_ENGINE_V2=True,
        BULK_PROCESSING_V2_CANARY_USER_IDS="999999",
    )
    def test_user_not_in_allowlist_fails_closed(self):
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "user_not_allowed")
        self.assertEqual(BulkSend.objects.count(), 0)

    @override_settings(BULK_PROCESSING_ENGINE_V2=True)
    def test_empty_csv_fails_before_persistence(self):
        response = self.client.post(self.url, self.payload(rows=0))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "row_count_empty")
        self.assertEqual(BulkSend.objects.count(), 0)

    @override_settings(BULK_PROCESSING_ENGINE_V2=True)
    def test_blank_csv_rows_do_not_bypass_empty_gate(self):
        response = self.client.post(
            self.url,
            self.payload(
                recipients_file=SimpleUploadedFile(
                    "rows.csv", b"email,name\n,\n  ,  \n"
                )
            ),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "row_count_empty")
        self.assertEqual(BulkSend.objects.count(), 0)

    @override_settings(
        BULK_PROCESSING_ENGINE_V2=True,
        BULK_PROCESSING_V2_CANARY_MAX_ROWS=0,
    )
    def test_invalid_canary_configuration_fails_closed(self):
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "canary_config_invalid")
        self.assertEqual(BulkSend.objects.count(), 0)

    @override_settings(
        BULK_PROCESSING_ENGINE_V2=True,
        BULK_PROCESSING_V2_ALLOW_EXTERNAL_TEMPLATE_LOOKUP=True,
    )
    def test_external_template_lookup_enabled_fails_closed(self):
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error_code"], "external_lookup_not_allowed"
        )
        self.assertEqual(BulkSend.objects.count(), 0)

    @override_settings(BULK_PROCESSING_ENGINE_V2=True)
    def test_v2_requires_explicit_template_name_without_lookup(self):
        with patch(
            "relay.services.doppler_relay.DopplerRelayClient",
            side_effect=AssertionError("Doppler must not be instantiated"),
        ):
            response = self.client.post(
                self.url, self.payload(template_name="")
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "template_name_required")
        self.assertEqual(BulkSend.objects.count(), 0)

    @override_settings(BULK_PROCESSING_ENGINE_V2=True)
    def test_process_endpoint_rejects_v2(self):
        response = self.client.post(self.url, self.payload())
        bulk_id = response.json()["bulk"]["id"]
        process_response = self.client.post(
            reverse("api_bulk_send_process", args=[bulk_id])
        )
        self.assertEqual(process_response.status_code, 409)
        self.assertEqual(BackgroundJob.objects.count(), 0)

    @override_settings(BULK_PROCESSING_ENGINE_V2=True)
    def test_existing_v2_remains_readable_when_flag_is_disabled(self):
        response = self.client.post(self.url, self.payload())
        bulk_id = response.json()["bulk"]["id"]
        with override_settings(BULK_PROCESSING_ENGINE_V2=False):
            detail = self.client.get(
                reverse("api_bulk_send_detail", args=[bulk_id])
            )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["bulk"]["engine_version"], "v2")
