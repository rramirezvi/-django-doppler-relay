from __future__ import annotations

import unittest
from types import SimpleNamespace

from ops.td02c_settings_gate import (
    CANARY_REQUEST_ID,
    evaluate_django_settings,
    evaluate_effective_settings,
)
from relay.services.bulk_v2_canary import normalize_allowlist


class TD02CSettingsGateTests(unittest.TestCase):
    def active(self, **changes):
        values = {
            "engine_enabled": True,
            "canary_enabled": True,
            "request_allowlist": CANARY_REQUEST_ID,
            "user_allowlist": "1",
            "max_rows": 20,
            "allow_external_template_lookup": False,
            "expect_active": True,
        }
        values.update(changes)
        return evaluate_effective_settings(**values)

    def test_single_exact_request_id_is_valid(self):
        self.assertTrue(self.active().allowed)

    def test_single_exact_user_id_is_valid(self):
        self.assertTrue(self.active(user_allowlist="1").allowed)

    def test_inactive_empty_allowlists_are_valid(self):
        result = evaluate_effective_settings(
            engine_enabled=False,
            canary_enabled=False,
            request_allowlist="",
            user_allowlist="",
            max_rows=20,
            allow_external_template_lookup=False,
            expect_active=False,
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.code, "canary_settings_inactive")

    def test_active_empty_allowlists_are_rejected(self):
        for change in ({"request_allowlist": ""}, {"user_allowlist": ""}):
            with self.subTest(change=change):
                self.assertFalse(self.active(**change).allowed)

    def test_spaces_around_values_are_rejected(self):
        for change in (
            {"request_allowlist": f" {CANARY_REQUEST_ID} "},
            {"user_allowlist": " 1 "},
        ):
            with self.subTest(change=change):
                self.assertFalse(self.active(**change).allowed)

    def test_multiple_values_are_rejected(self):
        self.assertFalse(
            self.active(request_allowlist=f"{CANARY_REQUEST_ID},another").allowed
        )

    def test_duplicates_are_rejected(self):
        for change in (
            {"request_allowlist": f"{CANARY_REQUEST_ID},{CANARY_REQUEST_ID}"},
            {"user_allowlist": "1,1"},
        ):
            with self.subTest(change=change):
                self.assertFalse(self.active(**change).allowed)

    def test_wildcard_is_rejected(self):
        self.assertFalse(self.active(request_allowlist="*").allowed)

    def test_request_prefix_is_rejected(self):
        self.assertFalse(
            self.active(request_allowlist="td02c-canary-import-v1").allowed
        )

    def test_non_numeric_user_id_is_rejected(self):
        self.assertFalse(self.active(user_allowlist="ricardo").allowed)

    def test_partially_valid_user_list_is_rejected(self):
        self.assertFalse(self.active(user_allowlist="1,invalid").allowed)

    def test_zero_user_id_is_rejected(self):
        self.assertFalse(self.active(user_allowlist="0").allowed)

    def test_negative_user_id_is_rejected(self):
        self.assertFalse(self.active(user_allowlist="-1").allowed)

    def test_additional_unexpected_values_are_rejected(self):
        for change in (
            {"request_allowlist": f"{CANARY_REQUEST_ID},extra"},
            {"user_allowlist": "1,2"},
        ):
            with self.subTest(change=change):
                self.assertFalse(self.active(**change).allowed)

    def test_uses_the_policy_parser_result(self):
        self.assertEqual(
            normalize_allowlist(CANARY_REQUEST_ID), ((CANARY_REQUEST_ID,), None)
        )
        self.assertEqual(normalize_allowlist("1", integer=True), ((1,), None))
        self.assertTrue(self.active().allowed)

    def test_valid_activation_through_django_settings_adapter(self):
        settings = SimpleNamespace(
            BULK_PROCESSING_ENGINE_V2=True,
            BULK_PROCESSING_V2_CANARY_ENABLED=True,
            BULK_PROCESSING_V2_CANARY_REQUEST_IDS=CANARY_REQUEST_ID,
            BULK_PROCESSING_V2_CANARY_USER_IDS="1",
            BULK_PROCESSING_V2_CANARY_MAX_ROWS=20,
            BULK_PROCESSING_V2_ALLOW_EXTERNAL_TEMPLATE_LOOKUP=False,
        )
        self.assertTrue(evaluate_django_settings(settings, expect_active=True).allowed)

    def test_valid_deactivation_through_django_settings_adapter(self):
        settings = SimpleNamespace(
            BULK_PROCESSING_ENGINE_V2=False,
            BULK_PROCESSING_V2_CANARY_ENABLED=False,
            BULK_PROCESSING_V2_CANARY_REQUEST_IDS="",
            BULK_PROCESSING_V2_CANARY_USER_IDS="",
            BULK_PROCESSING_V2_CANARY_MAX_ROWS=20,
            BULK_PROCESSING_V2_ALLOW_EXTERNAL_TEMPLATE_LOOKUP=False,
        )
        self.assertTrue(evaluate_django_settings(settings, expect_active=False).allowed)

    def test_remaining_effective_settings_fail_closed(self):
        for change in (
            {"engine_enabled": False},
            {"canary_enabled": False},
            {"max_rows": "20"},
            {"max_rows": 21},
            {"allow_external_template_lookup": True},
        ):
            with self.subTest(change=change):
                self.assertFalse(self.active(**change).allowed)

    def test_regression_raw_strings_are_not_compared_to_sets(self):
        settings = SimpleNamespace(
            BULK_PROCESSING_ENGINE_V2=True,
            BULK_PROCESSING_V2_CANARY_ENABLED=True,
            BULK_PROCESSING_V2_CANARY_REQUEST_IDS=CANARY_REQUEST_ID,
            BULK_PROCESSING_V2_CANARY_USER_IDS="1",
            BULK_PROCESSING_V2_CANARY_MAX_ROWS=20,
            BULK_PROCESSING_V2_ALLOW_EXTERNAL_TEMPLATE_LOOKUP=False,
        )
        self.assertNotEqual(
            settings.BULK_PROCESSING_V2_CANARY_REQUEST_IDS, {CANARY_REQUEST_ID}
        )
        self.assertNotEqual(settings.BULK_PROCESSING_V2_CANARY_USER_IDS, {1})
        self.assertTrue(evaluate_django_settings(settings, expect_active=True).allowed)


if __name__ == "__main__":
    unittest.main()
