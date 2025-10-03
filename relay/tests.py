from django.test import TestCase
from django.urls import reverse

class RelayEndpointsTest(TestCase):
    def test_send_endpoint_exists(self):
        resp = self.client.post(reverse("relay_send_email"), data={}, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
