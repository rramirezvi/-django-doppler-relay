"""Structural contract tests for the pilot-token fragment mechanism in
``config/templates/app/index.html`` (design D2, tasks.md Task 4,
``openspec/changes/bulk-v2-gradual-promotion``).

This repo has no Jest/Vitest/npm JS test infrastructure, and none is being
added for this single template change. These tests read the template as
plain text and assert on its literal structure -- they do not execute
React or a browser DOM. They exist to catch a regression in the pilot-token
contract (fragment source, key name, V2-only gating, readonly enforcement,
absence of an alternative editable input) using only the stdlib, without
requiring new tooling.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "templates" / "app" / "index.html"
)


class PilotTokenTemplateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TEMPLATE_PATH.read_text(encoding="utf-8")

    def test_parse_pilot_token_from_hash_exists(self):
        self.assertRegex(
            self.source,
            r"function\s+parsePilotTokenFromHash\s*\(",
            "parsePilotTokenFromHash must exist as a named function",
        )

    def test_uses_location_hash_not_location_search(self):
        self.assertIn(
            "parsePilotTokenFromHash(window.location.hash)",
            self.source,
            "the parser must be invoked with window.location.hash",
        )
        self.assertNotIn(
            "window.location.search",
            self.source,
            "window.location.search must never appear -- query strings leak "
            "into server access logs and the Referer header, which is "
            "exactly what the fragment choice (over a query parameter) was "
            "made to avoid",
        )

    def test_recognizes_pilot_token_key(self):
        # Scoped to the parser function body, not the whole file, so an
        # unrelated future "pilot_token" string elsewhere would not
        # accidentally satisfy this.
        match = re.search(
            r"function\s+parsePilotTokenFromHash\s*\([^)]*\)\s*\{(.*?)\n\s*\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "could not locate parsePilotTokenFromHash body")
        body = match.group(1)
        self.assertIn('"pilot_token"', body)
        self.assertIn("URLSearchParams", body)

    def test_fallback_to_crypto_random_uuid_exists(self):
        self.assertRegex(self.source, r"function\s+newClientRequestId\s*\(")
        self.assertIn("window.crypto.randomUUID()", self.source)
        self.assertRegex(
            self.source,
            r"effectiveClientRequestId\s*=\s*pilotTokenActive\s*\?\s*pilotToken\s*:\s*clientRequestId",
            "submission must fall back to the UUID-backed state whenever no "
            "pilot token is active",
        )

    def test_pilot_token_gated_to_v2_path_only(self):
        self.assertRegex(
            self.source,
            r"pilotTokenActive\s*=\s*v2Available\s*&&\s*form\.engine_version\s*===\s*[\"']v2[\"']\s*&&\s*!!pilotToken",
            "the pilot token must become the effective client_request_id "
            "only when v2Available AND engine_version is exactly 'v2' AND a "
            "non-empty token was present in the fragment",
        )

    def _pilot_token_input_props(self) -> str:
        match = re.search(
            r'"Token de piloto",\s*e\("input",\s*\{([^}]*)\}',
            self.source,
        )
        self.assertIsNotNone(match, "could not locate the pilot-token input element")
        return match.group(1)

    def test_field_renders_readonly(self):
        self.assertIn("readOnly: true", self._pilot_token_input_props())

    def test_no_editable_alternative_for_the_token(self):
        props = self._pilot_token_input_props()
        self.assertNotIn(
            "onChange",
            props,
            "the pilot-token input must have no onChange handler -- there "
            "must be no code path that lets a user type or edit the token",
        )
        self.assertIn(
            "value: pilotToken",
            props,
            "the displayed value must be sourced from the state variable "
            "populated exclusively by parsePilotTokenFromHash",
        )


if __name__ == "__main__":
    unittest.main()
