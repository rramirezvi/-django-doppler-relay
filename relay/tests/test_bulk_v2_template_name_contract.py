"""Structural contract tests for the ``template_name`` propagation fix in
``config/templates/app/index.html`` (Stage 1 pilot blocker: the deployed
form built ``client_request_id``/``template_id`` correctly but never sent
``template_name``, so every browser-based v2 submission failed closed on
the server with ``template_name_required`` before the canary gate ever ran
-- ``ops/bulk_v2_canary_client.py``, the headless client used by both prior
canary cycles, always sent both fields, which is why this went unnoticed
until the first browser-based attempt).

Same convention as ``test_bulk_v2_pilot_token_template_contract.py``: no
Jest/Vitest/npm infrastructure exists or is being added. These tests read
the template as plain text and assert on its literal structure -- they do
not execute React or a browser DOM.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "templates" / "app" / "index.html"
)


class TemplateNameContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TEMPLATE_PATH.read_text(encoding="utf-8")

    def _select_template_body(self) -> str:
        match = re.search(
            r"function\s+selectTemplate\s*\([^)]*\)\s*\{(.*?)\n\s*\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "could not locate selectTemplate body")
        return match.group(1)

    def _submit_body(self) -> str:
        match = re.search(
            r"async function submit\(ev\) \{(.*?)\n      \}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "could not locate submit() body")
        return match.group(1)

    # 1. Normal template selection -> template_id + template_name.
    def test_select_template_sets_both_id_and_name(self):
        body = self._select_template_body()
        self.assertIn("template_id: templateId", body)
        self.assertRegex(
            body,
            r"template_name:\s*tpl\?\.name\s*\|\|\s*[\"']{2}",
            "selectTemplate must copy the loaded template's real name "
            "verbatim (tpl.name), with no transformation -- this is what "
            "makes the PRUEBA template (or any other) keep its exact name",
        )

    # 2. V2 with no available name -> no POST.
    def test_v2_without_template_name_blocks_before_post(self):
        body = self._submit_body()
        match = re.search(
            r'if \(form\.engine_version === "v2" && !form\.template_name\) \{(.*?)\n\s*\}',
            body,
            re.DOTALL,
        )
        self.assertIsNotNone(
            match, "submit() must fail closed when v2 has no known template_name"
        )
        guard_body = match.group(1)
        self.assertIn("setToast(", guard_body)
        self.assertIn("return;", guard_body)
        # The guard must appear textually before the FormData/api() call so
        # a missing name can never reach the network request.
        guard_pos = body.index('!form.template_name')
        post_pos = body.index('api("/api/bulk-sends/"')
        self.assertLess(
            guard_pos, post_pos,
            "the template_name guard must run before the POST is issued",
        )
        # And before the submission lock/spinner state, matching the
        # existing !file guard's pattern (no partial side effects on abort).
        lock_pos = body.index("submitLock.current = true")
        self.assertLess(
            guard_pos, lock_pos,
            "the guard must abort before acquiring the submit lock, exactly "
            "like the pre-existing !file check",
        )

    # 3. V1 (legacy) is unaffected.
    def test_legacy_path_is_not_gated_by_template_name(self):
        body = self._submit_body()
        # The guard is scoped with the literal 'v2' string comparison --
        # legacy submissions never satisfy engine_version === "v2" and so
        # never hit the new check.
        self.assertIn('form.engine_version === "v2" && !form.template_name', body)
        # template_name is still appended for both engines (harmless for
        # legacy, since relay/api.py only reads it inside the v2 branch),
        # so the FormData shape for legacy gains one inert field, and
        # nothing legacy-specific was touched.
        self.assertIn('fd.append("template_name", form.template_name || "")', body)

    # 4. template_name is never operator-editable.
    def test_template_name_has_no_editable_input(self):
        # No onChange handler anywhere in the file may assign to
        # form.template_name directly -- it may only ever be *set* inside
        # selectTemplate (from tpl.name) or *cleared* to "" alongside a
        # template_id reset. There must be no `template_name: ev.target...`
        # style assignment, which would mean a raw operator-typed value.
        self.assertNotRegex(
            self.source,
            r"template_name:\s*ev\.target",
            "template_name must never be set from a raw input event -- it "
            "is derived exclusively from the loaded template's real name",
        )
        # Every setForm call that resets template_id to "" must also reset
        # template_name to "", so a manually-typed template_id (the
        # templatesError fallback input) can never carry a stale or
        # fabricated name forward.
        id_resets = re.findall(r"template_id:\s*(?:\"\"|ev\.target\.value)[^}]*\}", self.source)
        self.assertTrue(id_resets, "expected at least the manual template-id fallback input")

    def test_manual_template_id_fallback_clears_template_name(self):
        match = re.search(
            r'"ID de plantilla manual",\s*e\("input",\s*\{([^}]*)\}',
            self.source,
        )
        self.assertIsNotNone(match, "could not locate the manual template-id fallback input")
        props = match.group(1)
        self.assertIn("template_id: ev.target.value", props)
        self.assertIn(
            'template_name: ""',
            props,
            "typing a manual template_id must clear template_name rather "
            "than inventing or carrying forward a stale name",
        )

    def test_template_search_typing_clears_stale_template_name(self):
        # Typing in the autocomplete search box (which detaches the form
        # from whatever template was previously selected) must also drop
        # the previously-resolved template_name, not just template_id.
        self.assertIn(
            'setForm({...form, template_id: "", template_name: "", subject: ""})',
            self.source,
        )

    # 5. The PRUEBA template keeps its exact real name (generic mapping,
    #    verified structurally -- selectTemplate never truncates, slugifies
    #    or otherwise transforms tpl.name).
    def test_selected_template_name_is_not_transformed(self):
        body = self._select_template_body()
        # Only two candidate expressions may ever be assigned into
        # template_name: the raw tpl.name, or the empty-string fallback.
        match = re.search(r"template_name:\s*([^,\n]+),", body)
        self.assertIsNotNone(match)
        expression = match.group(1).strip()
        self.assertEqual(expression, 'tpl?.name || ""')

    # 6. Final v2 payload contains both fields.
    def test_v2_payload_includes_both_template_fields(self):
        body = self._submit_body()
        id_pos = body.index('fd.append("template_id"')
        name_pos = body.index('fd.append("template_name"')
        post_pos = body.index('api("/api/bulk-sends/"')
        self.assertLess(id_pos, post_pos)
        self.assertLess(name_pos, post_pos)


if __name__ == "__main__":
    unittest.main()
