import inspect

from django.test import SimpleTestCase

from relay.services import bulk_v2_canary
from relay.services.bulk_v2_real_send import evaluate_real_send


class BulkV2RealSendGateTests(SimpleTestCase):
    def decision(self, **changes):
        values = {
            "real_send_enabled": True,
            "user_allowlist": "17",
            "request_allowlist": "exact-request",
            "template_allowlist": "template-abc",
            "recipient_domain_allowlist": "example.com",
            "max_rows": 1,
            "user_id": 17,
            "client_request_id": "exact-request",
            "template_id": "template-abc",
            "recipient_domains": ("example.com",),
            "eligible_row_count": 1,
        }
        values.update(changes)
        return evaluate_real_send(**values)

    # --- success -----------------------------------------------------

    def test_authorized_decision_is_structured(self):
        decision = self.decision()
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.code, "real_send_allowed")

    # --- 1: kill switch ------------------------------------------------

    def test_real_send_disabled(self):
        decision = self.decision(real_send_enabled=False)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "real_send_disabled")

    # --- 2: config invalid, including MAX_ROWS == 1 exactness ----------

    def test_config_invalid_from_malformed_allowlists(self):
        for changes in (
            {"user_allowlist": "17,17"},
            {"request_allowlist": "req-*"},
            {"template_allowlist": "tmpl,tmpl"},
            {"recipient_domain_allowlist": "example.com,example.com"},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    self.decision(**changes).code, "real_send_config_invalid"
                )

    def test_max_rows_must_be_exactly_the_int_1(self):
        for max_rows in (0, 2, "1", 1.0, True, None):
            with self.subTest(max_rows=max_rows):
                self.assertEqual(
                    self.decision(max_rows=max_rows).code,
                    "real_send_config_invalid",
                )

    def test_max_rows_of_exactly_1_is_not_config_invalid(self):
        decision = self.decision(max_rows=1)
        self.assertNotEqual(decision.code, "real_send_config_invalid")

    # --- 3-6: empty allowlists -----------------------------------------

    def test_empty_allowlists_have_stable_rejections(self):
        self.assertEqual(
            self.decision(user_allowlist="").code, "real_send_user_allowlist_empty"
        )
        self.assertEqual(
            self.decision(request_allowlist="").code,
            "real_send_request_allowlist_empty",
        )
        self.assertEqual(
            self.decision(template_allowlist="").code,
            "real_send_template_allowlist_empty",
        )
        self.assertEqual(
            self.decision(recipient_domain_allowlist="").code,
            "real_send_domain_allowlist_empty",
        )

    # --- 7-11: identity/authorization checks ----------------------------

    def test_request_id_required(self):
        self.assertEqual(
            self.decision(client_request_id="").code,
            "real_send_request_id_required",
        )

    def test_request_not_allowed(self):
        self.assertEqual(
            self.decision(client_request_id="other-request").code,
            "real_send_request_not_allowed",
        )

    def test_user_not_allowed(self):
        self.assertEqual(
            self.decision(user_id=99).code, "real_send_user_not_allowed"
        )
        self.assertEqual(
            self.decision(user_id=None).code, "real_send_user_not_allowed"
        )

    def test_template_not_allowed(self):
        self.assertEqual(
            self.decision(template_id="other-template").code,
            "real_send_template_not_allowed",
        )

    def test_domain_not_allowed(self):
        self.assertEqual(
            self.decision(recipient_domains=("evil.example",)).code,
            "real_send_domain_not_allowed",
        )
        self.assertEqual(
            self.decision(
                recipient_domains=("example.com", "evil.example")
            ).code,
            "real_send_domain_not_allowed",
        )

    # --- 12-13: row-count checks -----------------------------------------

    def test_row_count_empty(self):
        self.assertEqual(
            self.decision(eligible_row_count=0).code, "real_send_row_count_empty"
        )

    def test_row_limit_exceeded(self):
        self.assertEqual(
            self.decision(eligible_row_count=2).code,
            "real_send_row_limit_exceeded",
        )

    # --- structural independence from evaluate_canary -------------------

    def test_module_does_not_import_evaluate_canary(self):
        import relay.services.bulk_v2_real_send as module

        self.assertFalse(hasattr(module, "evaluate_canary"))

    def test_parameter_names_exclude_evaluate_canarys_canary_only_names(self):
        # design.md §9 / PR1-T3: evaluate_real_send must not share a
        # parameter name with evaluate_canary's canary-specific concepts.
        # (Shared *concept* names like user_allowlist/max_rows/user_id are
        # expected and fine — both gates authorize a similar shape of
        # action — what must never appear here is any of the six names
        # that are specific to the import-only canary semantics.)
        real_send_params = set(
            inspect.signature(evaluate_real_send).parameters
        )
        canary_only_params = {
            "send_now",
            "scheduled_at",
            "import_only",
            "engine_enabled",
            "canary_enabled",
            "allow_external_template_lookup",
        }
        self.assertTrue(canary_only_params.issubset(
            set(inspect.signature(bulk_v2_canary.evaluate_canary).parameters)
        ))
        self.assertEqual(real_send_params & canary_only_params, set())
