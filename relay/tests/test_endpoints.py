import json
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.middleware.csrf import _get_new_csrf_string
from django.test import Client, TestCase
from django.urls import reverse


class RelayEndpointsTest(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.url = reverse("relay_send_bulk")
        self.valid_payload = {
            "template_id": "safe-test-template",
            "recipients": [{"email": "safe@example.com"}],
        }

    def _csrf_headers(self):
        token = _get_new_csrf_string()
        self.client.cookies["csrftoken"] = token
        return {"HTTP_X_CSRFTOKEN": token}

    def _create_user(
        self, *, username, is_active=True, is_staff=False, permission=None
    ):
        user = User.objects.create_user(
            username=username,
            password="not-used",
            is_active=is_active,
            is_staff=is_staff,
        )
        if permission:
            app_label, codename = permission.split(".", 1)
            user.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label=app_label,
                    codename=codename,
                )
            )
        return user

    def _login(self, user):
        self.client.force_login(user)

    def _post(self, *, csrf=False):
        return self.client.post(
            self.url,
            data=json.dumps(self.valid_payload),
            content_type="application/json",
            **(self._csrf_headers() if csrf else {}),
        )

    @patch("relay.views.process_bulk_template_send")
    def test_anonymous_without_csrf_is_rejected_by_middleware(self, mock_send):
        response = self._post()

        self.assertEqual(response.status_code, 403)
        mock_send.assert_not_called()

    @patch("relay.views.process_bulk_template_send")
    def test_anonymous_with_valid_csrf_gets_json_401(self, mock_send):
        response = self._post(csrf=True)

        self.assertEqual(response.status_code, 401)
        self.assertJSONEqual(
            response.content,
            {"ok": False, "error": "Usuario no autenticado"},
        )
        mock_send.assert_not_called()

    @patch("relay.views.process_bulk_template_send")
    def test_inactive_user_with_valid_csrf_gets_json_403(self, mock_send):
        user = self._create_user(username="inactive", is_active=False)

        with patch("django.contrib.auth.middleware.get_user", return_value=user):
            response = self._post(csrf=True)

        self.assertEqual(response.status_code, 403)
        self.assertJSONEqual(
            response.content,
            {"ok": False, "error": "No autorizado"},
        )
        mock_send.assert_not_called()

    @patch("relay.views.process_bulk_template_send")
    def test_active_non_staff_with_permission_gets_json_403(self, mock_send):
        self._login(
            self._create_user(
                username="non-staff",
                permission="relay.change_bulksend",
            )
        )

        response = self._post(csrf=True)

        self.assertEqual(response.status_code, 403)
        mock_send.assert_not_called()

    @patch("relay.views.process_bulk_template_send")
    def test_staff_without_permissions_gets_json_403(self, mock_send):
        self._login(self._create_user(username="staff-no-perms", is_staff=True))

        response = self._post(csrf=True)

        self.assertEqual(response.status_code, 403)
        mock_send.assert_not_called()

    @patch("relay.views.process_bulk_template_send", return_value=[])
    def test_staff_with_relay_permission_is_authorized(self, mock_send):
        self._login(
            self._create_user(
                username="relay-operator",
                is_staff=True,
                permission="relay.change_bulksend",
            )
        )

        response = self._post(csrf=True)

        self.assertEqual(response.status_code, 200)
        mock_send.assert_called_once()

    @patch("relay.views.process_bulk_template_send", return_value=[])
    def test_staff_with_proxy_permission_is_authorized(self, mock_send):
        self._login(
            self._create_user(
                username="proxy-operator",
                is_staff=True,
                permission="relay_super.change_bulksenduserconfigproxy",
            )
        )

        response = self._post(csrf=True)

        self.assertEqual(response.status_code, 200)
        mock_send.assert_called_once()

    @patch("relay.views.process_bulk_template_send")
    def test_authorized_user_without_csrf_is_rejected_by_middleware(self, mock_send):
        self._login(
            self._create_user(
                username="no-csrf",
                is_staff=True,
                permission="relay.change_bulksend",
            )
        )

        response = self._post()

        self.assertEqual(response.status_code, 403)
        mock_send.assert_not_called()

    @patch("relay.views.process_bulk_template_send")
    def test_non_post_methods_return_405_without_sending(self, mock_send):
        csrf_headers = self._csrf_headers()
        responses = [
            self.client.get(self.url),
            self.client.put(
                self.url,
                data="{}",
                content_type="application/json",
                **csrf_headers,
            ),
            self.client.patch(
                self.url,
                data="{}",
                content_type="application/json",
                **csrf_headers,
            ),
            self.client.delete(
                self.url,
                data="{}",
                content_type="application/json",
                **csrf_headers,
            ),
        ]

        self.assertEqual([response.status_code for response in responses], [405] * 4)
        mock_send.assert_not_called()

    @patch("relay.views.process_bulk_template_send")
    def test_empty_send_payload_is_rejected_without_calling_doppler(
        self, mock_send
    ):
        self._login(
            self._create_user(
                username="empty-payload",
                is_staff=True,
                permission="relay.change_bulksend",
            )
        )

        response = self.client.post(
            self.url,
            data=json.dumps({}),
            content_type="application/json",
            **self._csrf_headers(),
        )

        self.assertEqual(self.url, "/relay/send/")
        self.assertEqual(response.status_code, 400)
        self.assertJSONEqual(
            response.content,
            {
                "ok": False,
                "error": "Falta el ID de la plantilla",
            },
        )
        mock_send.assert_not_called()
