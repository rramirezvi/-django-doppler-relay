"""PR2b-T34: doppler_relay.py behavior-preservation tests (design.md §10).

(a) default max_attempts == 3; (b) a mocked transport that always raises
is invoked exactly 3 times for a default client and exactly 1 time for a
max_attempts=1 client; (c) build_single_attempt_client() returns a client
with max_attempts == 1. (d) the existing, untouched V1 test suite passing
unmodified is confirmed separately as part of this sdd-apply session's
overall validation run (reported in the apply-progress summary), not
nested inside this test process — running Django's own test runner
recursively from within a running test is not a reliable in-process
technique and is not how this repository's other suites verify
regression-freedom either.
"""

from __future__ import annotations

from unittest import mock

import requests
from django.test import SimpleTestCase

from relay.services.bulk_v2_send import build_single_attempt_client
from relay.services.doppler_relay import DopplerRelayClient


class DopplerSingleAttemptTests(SimpleTestCase):
    def test_default_max_attempts_is_3(self):
        client = DopplerRelayClient()
        self.assertEqual(client.max_attempts, 3)

    def test_default_client_retries_exactly_3_times_on_persistent_failure(self):
        client = DopplerRelayClient()
        with mock.patch("time.sleep"), mock.patch.object(
            client.session, "request", side_effect=requests.ConnectionError("boom")
        ) as mocked:
            with self.assertRaises(Exception):
                client._request("GET", "/x")
        self.assertEqual(mocked.call_count, 3)

    def test_single_attempt_client_retries_exactly_once_on_persistent_failure(self):
        client = DopplerRelayClient(max_attempts=1)
        with mock.patch("time.sleep"), mock.patch.object(
            client.session, "request", side_effect=requests.ConnectionError("boom")
        ) as mocked:
            with self.assertRaises(Exception):
                client._request("GET", "/x")
        self.assertEqual(mocked.call_count, 1)

    def test_max_attempts_floors_at_1_for_non_positive_input(self):
        self.assertEqual(DopplerRelayClient(max_attempts=0).max_attempts, 1)
        self.assertEqual(DopplerRelayClient(max_attempts=-5).max_attempts, 1)

    def test_build_single_attempt_client_has_max_attempts_1(self):
        client = build_single_attempt_client()
        self.assertEqual(client.max_attempts, 1)
        self.assertIsInstance(client, DopplerRelayClient)
