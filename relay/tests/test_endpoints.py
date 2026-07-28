import json
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse


class RelayEndpointsTest(TestCase):
    @patch("relay.views.DopplerRelayClient")
    def test_empty_send_payload_is_rejected_without_calling_doppler(
        self, mock_doppler_client
    ):
        url = reverse("relay_send_bulk")

        self.assertEqual(url, "/relay/send/")

        response = self.client.post(
            url,
            data=json.dumps({}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertJSONEqual(
            response.content,
            {
                "ok": False,
                "error": "Falta el ID de la plantilla",
            },
        )
        mock_doppler_client.assert_not_called()
