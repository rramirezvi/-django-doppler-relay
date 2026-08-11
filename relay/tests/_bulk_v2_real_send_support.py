"""Shared, non-test support for PR2b's real-send test files.

Not itself a test module (does not match Django's `test*.py` discovery
naming on the file, but classes defined here ARE imported and used by
files that do match). Provides:

  - `NoRealDopplerCallTestCase` (PR2b-T35): patches
    `requests.Session.request` to raise loudly by default in every test
    that inherits it, so a test that forgets to configure a mock reaches
    a hard failure instead of a real network call.
  - `RealSendFixtureMixin`: BulkSend/BulkSendRecipient/User fixtures and
    an `authorized_settings(...)` helper producing the six
    BULK_PROCESSING_V2_REAL_SEND_* override values needed to reach an
    `evaluate_real_send` allowed decision in a test.
  - `FakeDopplerResponse`: a minimal stand-in for `requests.Response`,
    used as the mocked transport's return value on a simulated 2xx/4xx/5xx
    reply.
"""

from __future__ import annotations

import uuid
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase

from relay.models import BulkSend, BulkSendRecipient


class FakeDopplerResponse:
    def __init__(self, *, status_code=201, json_data=None, headers=None, text=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {"message_id": "msg-fake-123"}
        self.headers = headers if headers is not None else {"Location": "/accounts/1/deliveries/msg-fake-123"}
        self.text = text if text is not None else "{}"
        self.request = mock.Mock(
            method="POST",
            url="https://api.dopplerrelay.com/accounts/1/templates/tpl/message",
            headers={},
            body=b"{}",
        )

    def json(self):
        return self._json_data


def _unmocked_transport_call(*args, **kwargs):
    raise RuntimeError(
        "real HTTP call attempted in test — call self.mock_transport(...) "
        "in this test or its setUp before exercising a Doppler-calling "
        "code path (PR2b-T35)."
    )


class NoRealDopplerCallTestCase(TransactionTestCase):
    """PR2b-T35: base class every PR2b test file that can reach the send
    path must inherit. Patches `requests.Session.request` — the exact
    transport call `DopplerRelayClient._request` makes — to raise
    `RuntimeError` by default. Call `self.mock_transport(...)` to install
    a controlled response/exception for the duration of a test.

    Deliberately `TransactionTestCase`, not `TestCase`: `TestCase` wraps
    every test body in its own outer `transaction.atomic()` block for
    rollback, which would make `transaction.get_autocommit()` always
    False and trip `ensure_autocommit_context()`'s runtime guard
    (design.md §4) on every call, even outside any transaction the test
    itself opens (same finding as PR2a's state-machine tests).
    """

    def setUp(self):
        super().setUp()
        self._transport_patcher = mock.patch(
            "requests.Session.request", side_effect=_unmocked_transport_call
        )
        self._transport_mock = self._transport_patcher.start()
        self.addCleanup(self._transport_patcher.stop)

    def mock_transport(self, *, return_value=None, side_effect=None):
        if side_effect is not None:
            self._transport_mock.side_effect = side_effect
            self._transport_mock.return_value = None
        else:
            self._transport_mock.side_effect = None
            self._transport_mock.return_value = return_value
        return self._transport_mock


class RealSendFixtureMixin:
    def make_user(self, **overrides):
        values = dict(
            username=f"real-send-user-{uuid.uuid4().hex[:8]}",
            password="unused",
        )
        values.update(overrides)
        return User.objects.create_user(**values)

    def make_bulk(self, *, user=None, **overrides):
        values = dict(
            template_id="tpl-real",
            template_name="Template real",
            client_request_id=f"real-send-request-{uuid.uuid4().hex[:8]}",
            recipients_file=SimpleUploadedFile(
                "rows.csv", b"email,name\na@example.com,A\n"
            ),
            engine_version=BulkSend.ENGINE_V2,
            import_status=BulkSend.IMPORT_READY,
        )
        if user is not None:
            values["scheduled_by"] = user
        values.update(overrides)
        return BulkSend.objects.create(**values)

    def make_occurrence(self, bulk, **overrides):
        values = dict(
            bulk_send=bulk,
            import_version=1,
            source_row_number=1,
            recipient="a@example.com",
            normalized_recipient="a@example.com",
            payload={"name": "A"},
            payload_hash="a" * 64,
            idempotency_key=uuid.uuid4(),
            status=BulkSendRecipient.STATUS_PENDING,
        )
        values.update(overrides)
        return BulkSendRecipient.objects.create(**values)

    def authorized_settings(self, *, user, bulk, domain="example.com"):
        from django.conf import settings as django_settings

        # bulk_v2_send.py's _build_recipients_model falls back to
        # DOPPLER_RELAY's DEFAULT_FROM_EMAIL/DEFAULT_FROM_NAME (the same
        # fallback V1 itself uses when no per-user config exists — see
        # that function's docstring). The real .env-driven test
        # environment leaves these empty, which is a legitimate
        # fail-closed outcome (payload_invalid) but not what most tests in
        # this file are exercising, so tests that need a real send to
        # reach the transport override DOPPLER_RELAY here too.
        doppler_cfg = dict(django_settings.DOPPLER_RELAY)
        doppler_cfg["DEFAULT_FROM_EMAIL"] = doppler_cfg.get("DEFAULT_FROM_EMAIL") or "noreply@example.com"
        doppler_cfg["DEFAULT_FROM_NAME"] = doppler_cfg.get("DEFAULT_FROM_NAME") or "Real Send Canary"

        return dict(
            BULK_PROCESSING_V2_REAL_SEND_ENABLED=True,
            BULK_PROCESSING_V2_REAL_SEND_USER_IDS=str(user.pk),
            BULK_PROCESSING_V2_REAL_SEND_REQUEST_IDS=bulk.client_request_id,
            BULK_PROCESSING_V2_REAL_SEND_TEMPLATE_IDS=bulk.template_id,
            BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS=domain,
            BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS=1,
            DOPPLER_RELAY=doppler_cfg,
        )
