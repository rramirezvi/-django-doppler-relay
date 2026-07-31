from django.test import SimpleTestCase

from relay.services.bulk_v2_canary import evaluate_canary


class BulkV2CanaryPolicyTests(SimpleTestCase):
    def decision(self, **changes):
        values = {
            "engine_enabled": True,
            "canary_enabled": True,
            "request_allowlist": "exact-request",
            "user_allowlist": "17",
            "max_rows": 20,
            "allow_external_template_lookup": False,
            "client_request_id": "exact-request",
            "user_id": 17,
            "total_rows": 10,
            "send_now": False,
            "scheduled_at": "",
            "import_only": True,
        }
        values.update(changes)
        return evaluate_canary(**values)

    def test_authorized_decision_is_structured(self):
        decision = self.decision()
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.code, "canary_allowed")

    def test_exact_comparison_does_not_accept_prefix(self):
        decision = self.decision(client_request_id="exact")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "request_not_allowed")

    def test_empty_allowlists_have_stable_rejections(self):
        self.assertEqual(
            self.decision(request_allowlist="").code,
            "request_allowlist_empty",
        )
        self.assertEqual(
            self.decision(user_allowlist="").code,
            "user_allowlist_empty",
        )

    def test_empty_identifiers_are_rejected(self):
        self.assertEqual(
            self.decision(client_request_id="").code,
            "request_id_required",
        )
        self.assertEqual(
            self.decision(user_id=None).code,
            "user_not_allowed",
        )

    def test_duplicates_wildcards_and_nonpositive_limit_are_invalid(self):
        for changes in (
            {"request_allowlist": "same,same"},
            {"request_allowlist": "request-*"},
            {"user_allowlist": "17,17"},
            {"max_rows": "invalid"},
            {"max_rows": 0},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    self.decision(**changes).code,
                    "canary_config_invalid",
                )

    def test_import_only_constraints_are_independent(self):
        self.assertEqual(
            self.decision(send_now=True).code, "send_not_allowed"
        )
        self.assertEqual(
            self.decision(scheduled_at="2026-08-01T10:00:00").code,
            "schedule_not_allowed",
        )
        self.assertEqual(
            self.decision(import_only=False).code,
            "import_only_required",
        )
