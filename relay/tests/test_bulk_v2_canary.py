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

    def test_multi_entry_allowlist_admits_every_listed_pair(self):
        request_allowlist = "stage1-c1-u41-01,stage1-c1-u52-01,stage1-c1-u67-01"
        user_allowlist = "41,52,67"
        for user_id, client_request_id in (
            (41, "stage1-c1-u41-01"),
            (52, "stage1-c1-u52-01"),
            (67, "stage1-c1-u67-01"),
        ):
            with self.subTest(user_id=user_id, client_request_id=client_request_id):
                decision = self.decision(
                    request_allowlist=request_allowlist,
                    user_allowlist=user_allowlist,
                    client_request_id=client_request_id,
                    user_id=user_id,
                )
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.code, "canary_allowed")

    def test_multi_entry_allowlist_rejects_user_not_on_the_list(self):
        decision = self.decision(
            request_allowlist="stage1-c1-u41-01,stage1-c1-u52-01,stage1-c1-u67-01",
            user_allowlist="41,52,67",
            client_request_id="stage1-c1-u41-01",
            user_id=99,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "user_not_allowed")

    def test_multi_entry_allowlist_rejects_request_not_on_the_list(self):
        decision = self.decision(
            request_allowlist="stage1-c1-u41-01,stage1-c1-u52-01,stage1-c1-u67-01",
            user_allowlist="41,52,67",
            client_request_id="not-registered",
            user_id=41,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "request_not_allowed")

    def test_multi_entry_malformed_entry_disables_the_entire_list(self):
        base = {
            "request_allowlist": "stage1-c1-u41-01,stage1-c1-u52-01",
            "user_allowlist": "41,52",
        }
        for changes in (
            {"request_allowlist": "stage1-c1-u41-01,stage1-c1-u52-01,stage1-c1-u41-01"},
            {"request_allowlist": "stage1-c1-u41-01,stage1-c1-u52-*"},
            {"user_allowlist": "41,52,41"},
            {"user_allowlist": "41,52,0"},
        ):
            allowlists = dict(base, **changes)
            with self.subTest(changes=changes):
                for user_id, client_request_id in (
                    (41, "stage1-c1-u41-01"),
                    (52, "stage1-c1-u52-01"),
                ):
                    decision = self.decision(
                        client_request_id=client_request_id,
                        user_id=user_id,
                        **allowlists,
                    )
                    self.assertEqual(decision.code, "canary_config_invalid")
