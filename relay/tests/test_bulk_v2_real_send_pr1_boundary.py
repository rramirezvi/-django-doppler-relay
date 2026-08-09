"""PR1-T9 mechanical boundary check (design.md §14, tasks.md PR1-T9).

`openspec/changes/bulk-v2-real-send-canary/tasks.md` requires the PR1 diff to
introduce zero NEW reference to any of the eight tokens below, except purely
documentary/test mentions that create no executable path. This test walks
every file this PR1 apply pass touched (source files only — this test file
and design.md/tasks.md themselves are the only permitted exceptions, and are
excluded from the scan by construction, matching the task's own carve-out)
and asserts none of the forbidden tokens appear.

If a new PR1 file is added later and needs one of these tokens for a
legitimate documentary reason, it must be added to the file list here with
an inline justification — never silently exempted.
"""

from pathlib import Path

from django.test import SimpleTestCase

BASE_DIR = Path(__file__).resolve().parent.parent.parent

FORBIDDEN_TOKENS = (
    "DopplerRelayClient",
    "send_template_message",
    "doppler_relay",
    "requests",
    "BackgroundJob",
    "management/commands",
    "BulkSendRecipient",
    "migrations",
)

# Every source file this PR1 apply pass created or modified. Deliberately
# excludes this test file itself (it must name the tokens to check for them)
# and openspec/**/{design,tasks}.md (the task's own explicit exception).
PR1_TOUCHED_FILES = (
    "config/settings.py",
    "relay/services/bulk_v2_real_send.py",
    "relay/tests/test_bulk_v2_real_send_gate.py",
    "relay/tests/test_bulk_v2_real_send_logging.py",
    "relay/tests/test_logging_config.py",
)


class BulkV2RealSendPr1BoundaryTests(SimpleTestCase):
    def test_no_new_pr1_file_references_a_forbidden_symbol(self):
        violations = []
        for relative_path in PR1_TOUCHED_FILES:
            path = BASE_DIR / relative_path
            self.assertTrue(path.is_file(), f"expected PR1 file missing: {path}")
            text = path.read_text(encoding="utf-8")
            for token in FORBIDDEN_TOKENS:
                if token in text:
                    violations.append((relative_path, token))
        self.assertEqual(
            violations,
            [],
            f"PR1 diff introduces forbidden references: {violations}",
        )

    def test_forbidden_token_list_matches_design_and_tasks(self):
        # Pin the token list itself so a future edit here cannot silently
        # narrow the boundary check without being noticed in review.
        self.assertEqual(
            FORBIDDEN_TOKENS,
            (
                "DopplerRelayClient",
                "send_template_message",
                "doppler_relay",
                "requests",
                "BackgroundJob",
                "management/commands",
                "BulkSendRecipient",
                "migrations",
            ),
        )
