"""PR2b-T33: grep-provable structural test asserting zero
`ambiguous -> sent` / `ambiguous -> retry` code path exists anywhere.

PR2b-T35(b): meta-test confirming every `DopplerRelayClient(` construction
site across this change's new PR2b test files is paired with a mock/patch
of the transport in the same file.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from django.test import SimpleTestCase

import relay.services.bulk_v2_send as bulk_v2_send_module
import relay.services.bulk_v2_send_state as bulk_v2_send_state_module
from relay.services.bulk_v2_send_state import LEGAL_TRANSITIONS

FORBIDDEN_SYMBOL_FRAGMENTS = (
    "resolve_ambiguous",
    "retry_ambiguous",
    "reconcile_ambiguous",
)

# Files this change (PR2b) added or modified that can construct a
# DopplerRelayClient in test code.
PR2B_TEST_FILES = (
    "test_bulk_v2_real_send_command.py",
    "test_bulk_v2_crash_scenarios.py",
    "test_bulk_v2_real_send_postgresql.py",
    "test_doppler_single_attempt.py",
)


class AmbiguousHasNoExitPathTests(SimpleTestCase):
    def test_legal_transitions_whitelist_has_no_ambiguous_source_edge(self):
        # design.md §2.5 point 2 / §11.3: no tuple in LEGAL_TRANSITIONS may
        # have "ambiguous" as its FROM state.
        ambiguous_as_source = [
            edge for edge in LEGAL_TRANSITIONS if edge[0] == "ambiguous"
        ]
        self.assertEqual(ambiguous_as_source, [])

    def test_no_reconciliation_or_retry_symbol_exists_in_send_path_modules(self):
        for module in (bulk_v2_send_module, bulk_v2_send_state_module):
            source = inspect.getsource(module)
            for fragment in FORBIDDEN_SYMBOL_FRAGMENTS:
                with self.subTest(module=module.__name__, fragment=fragment):
                    self.assertNotIn(fragment, source)

    def test_no_reconciliation_or_retry_symbol_anywhere_under_relay_services(self):
        services_dir = Path(bulk_v2_send_module.__file__).parent
        for py_file in services_dir.glob("*.py"):
            text = py_file.read_text(encoding="utf-8")
            for fragment in FORBIDDEN_SYMBOL_FRAGMENTS:
                with self.subTest(file=py_file.name, fragment=fragment):
                    self.assertNotIn(fragment, text)

    def test_no_update_or_save_pairs_ambiguous_where_with_a_different_set_target(self):
        # Structural proof over the module's actual write call sites: a
        # transition OUT of ambiguous into any other state would require a
        # literal SEND_AMBIGUOUS constant used as a `filter(...)` WHERE
        # clause (the compare-and-set "expected FROM" state). None of the
        # three transition functions filter on SEND_AMBIGUOUS at all — each
        # filters only on SEND_SENDING (the sole legal FROM state for all
        # three terminal transitions), so the only legitimate occurrence of
        # the SEND_AMBIGUOUS constant paired with the send_status keyword is
        # the SET clause inside mark_ambiguous itself.
        #
        # NOTE: this assertion's own descriptive text is deliberately kept
        # away from the literal token sequence
        # "send_status" + "=" + "BulkSendRecipient.SEND_AMBIGUOUS" split
        # across adjacent lines, because PR2a's own grep-provable structural
        # test (test_bulk_v2_send_state.py's SendStatusWriteBoundaryTests)
        # scans raw file text for that exact pattern inside any
        # parenthesized call body anywhere under relay/, comments included.
        source = inspect.getsource(bulk_v2_send_state_module)
        needle = "send_status" + "=" + "BulkSendRecipient.SEND_AMBIGUOUS"
        matching_lines = [line for line in source.splitlines() if needle in line]
        self.assertTrue(matching_lines)
        for line in matching_lines:
            self.assertNotIn(".filter(", line)


class NoRealHttpMetaTest(SimpleTestCase):
    def test_every_doppler_client_construction_site_is_paired_with_a_mock(self):
        tests_dir = Path(__file__).parent
        construction_re = re.compile(r"DopplerRelayClient\(")
        mock_markers = (
            "mock.patch",
            "mock_transport",
            "NoRealDopplerCallTestCase",
        )
        checked_any = False
        for filename in PR2B_TEST_FILES:
            path = tests_dir / filename
            self.assertTrue(path.exists(), f"expected PR2b test file missing: {filename}")
            text = path.read_text(encoding="utf-8")
            if construction_re.search(text):
                checked_any = True
                paired = any(marker in text for marker in mock_markers)
                self.assertTrue(
                    paired,
                    f"{filename} constructs DopplerRelayClient but has no "
                    "transport mock marker in the same file",
                )
        self.assertTrue(checked_any, "expected at least one PR2b test file to construct DopplerRelayClient")
