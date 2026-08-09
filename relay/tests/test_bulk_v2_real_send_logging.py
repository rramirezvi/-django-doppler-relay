"""bulk-v2-real-send-canary observability: redaction (design.md §12.2/§12.3).

PR1 scope only: no send-capable code exists yet, so this test does not
exercise `bulk_v2_real_send_attempt`/`bulk_v2_real_send_result` (PR2b). It
establishes, as infrastructure, that a `bulk_v2_real_send_decision`-shaped
log line built from only the fields design.md §12.2 allows never contains
the Doppler API key, a raw token, or a full recipient email address —
the pattern PR2-T20 extends to the attempt/result events.
"""

import hashlib
import logging

from django.test import SimpleTestCase, override_settings

logger = logging.getLogger("relay.bulk_v2_real_send")

DOPPLER_API_KEY_SENTINEL = "sk_live_SENTINEL_DO_NOT_LOG_1234567890abcdef"
RAW_TOKEN_SENTINEL = "Bearer super-secret-raw-token-zzz"
FULL_RECIPIENT_EMAIL = "jane.doe@example.com"


@override_settings(DOPPLER_RELAY={"API_KEY": DOPPLER_API_KEY_SENTINEL})
class BulkV2RealSendDecisionRedactionTests(SimpleTestCase):
    def _emit_decision_event(self, *, client_request_id, bulk_send_id, code):
        # Mirrors design.md §12.2's field set for bulk_v2_real_send_decision:
        # decision, code, bulk_send_id, request=<sha256(client_request_id)[:12]>,
        # eligible_rows, max_rows, at. No API key, no raw token, no full
        # recipient address is ever a field of this event.
        request_fingerprint = hashlib.sha256(
            client_request_id.encode("utf-8")
        ).hexdigest()[:12]
        with self.assertLogs(logger, level="INFO") as captured:
            logger.info(
                "bulk_v2_real_send_decision decision=%s code=%s bulk_send_id=%s "
                "request=%s eligible_rows=%s max_rows=%s",
                "refused" if code != "real_send_allowed" else "allowed",
                code,
                bulk_send_id,
                request_fingerprint,
                1,
                1,
            )
        return captured.output

    def test_decision_event_never_contains_the_doppler_api_key(self):
        from django.conf import settings

        output = self._emit_decision_event(
            client_request_id=FULL_RECIPIENT_EMAIL,
            bulk_send_id=42,
            code="real_send_disabled",
        )
        formatted = "\n".join(output)
        self.assertNotIn(settings.DOPPLER_RELAY["API_KEY"], formatted)
        self.assertNotIn(DOPPLER_API_KEY_SENTINEL, formatted)

    def test_decision_event_never_contains_a_raw_token(self):
        output = self._emit_decision_event(
            client_request_id="req-1",
            bulk_send_id=42,
            code="real_send_allowed",
        )
        formatted = "\n".join(output)
        self.assertNotIn(RAW_TOKEN_SENTINEL, formatted)

    def test_decision_event_never_contains_the_full_recipient_address(self):
        # client_request_id deliberately set to a full email address to prove
        # the fingerprinting (sha256[:12]) is what reaches the log line, not
        # the raw identifier.
        output = self._emit_decision_event(
            client_request_id=FULL_RECIPIENT_EMAIL,
            bulk_send_id=42,
            code="real_send_allowed",
        )
        formatted = "\n".join(output)
        self.assertNotIn(FULL_RECIPIENT_EMAIL, formatted)
