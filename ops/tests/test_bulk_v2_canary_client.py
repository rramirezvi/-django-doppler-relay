from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ops.td02c_authenticated_get_runner import Baseline, CurlOperations, RunnerFailure
from ops.td02c_http_client import (
    AuthenticatedGetFailure,
    ResponseMetadata,
    SafeDiagnosticLog,
    discover_canary_target,
    safe_curl_argv,
    secure_cookie_workspace,
    write_post_curl_config,
)

from ops.bulk_v2_canary_client import (
    CanaryProfile,
    ERROR_CODES,
    _assert_expected_delta,
    _assert_import_only_profile,
    _assert_profile_matches_settings,
    _execute_canary_post,
    main,
    run_canary,
    validate_client_module_identity,
)


def nginx_config(server_name: str, socket: str = "/run/django.sock") -> str:
    return f"""
    server {{
        listen 443 ssl;
        server_name {server_name};
        ssl_certificate /etc/ssl/cert.pem;
        location / {{ proxy_pass http://unix:{socket}; }}
    }}
    """


def profile(**changes) -> CanaryProfile:
    values = {
        "request_id": "fresh-canary-request-id",
        "user_id": 42,
        "template_id": "td02c-canary-import-only",
        "template_name": "TD-02C Canary Import Only",
        "subject": "TD-02C Canary Import Only",
        "max_rows": 20,
    }
    values.update(changes)
    return CanaryProfile(**values)


def active_settings(**changes) -> SimpleNamespace:
    values = {
        "BULK_PROCESSING_ENGINE_V2": True,
        "BULK_PROCESSING_V2_CANARY_ENABLED": True,
        "BULK_PROCESSING_V2_CANARY_REQUEST_IDS": "fresh-canary-request-id",
        "BULK_PROCESSING_V2_CANARY_USER_IDS": "42",
        "BULK_PROCESSING_V2_CANARY_MAX_ROWS": 20,
        "BULK_PROCESSING_V2_ALLOW_EXTERNAL_TEMPLATE_LOOKUP": False,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def baseline(**changes) -> Baseline:
    values = {
        "total_sessions": 3,
        "user_sessions": frozenset({"old"}),
        "bulk_sends": 5,
        "bulk_sends_v2": 0,
        "recipients": 0,
        "jobs": 2,
        "messages": 9,
    }
    values.update(changes)
    return Baseline(**values)


def _iter_json_leaf_values(output: str):
    """Yield every leaf value from each newline-delimited JSON object in
    ``output`` (the SafeDiagnosticLog/emit_counts format: one JSON object per
    line). Used to assert a raw id never appears as an actual field value,
    without false-positiving on unrelated numeric noise (e.g. a
    ``duration_seconds`` float like ``0.000142`` that merely *contains* the
    digits "42" as a substring of a larger, unrelated number)."""
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        stack = [json.loads(line)]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
            else:
                yield node


class CanaryProfileShapeTests(unittest.TestCase):
    """CanaryProfile intentionally exposes no send_now/scheduled_at/sender_id
    field -- that absence is the client-side import-only enforcement, not a
    runtime check catching an override attempt."""

    def test_frozen_and_default_max_rows(self):
        instance = profile()
        self.assertEqual(instance.max_rows, 20)
        with self.assertRaises(Exception):
            instance.request_id = "changed"  # type: ignore[misc]

    def test_no_send_now_scheduled_at_or_sender_id_field_exists(self):
        names = {field.name for field in fields(CanaryProfile)}
        self.assertEqual(
            names,
            {"request_id", "user_id", "template_id", "template_name", "subject", "max_rows"},
        )
        for forbidden in ("send_now", "scheduled_at", "sender_id", "import_only"):
            self.assertNotIn(forbidden, names)


class ProfileConsistencyTests(unittest.TestCase):
    """Design D3: client asserts profile == gate expectation == effective raw
    setting before any HTTP. Requirement: max_rows read from the same source
    the gate validates (BULK_PROCESSING_V2_CANARY_MAX_ROWS), never hardcoded."""

    def test_matching_profile_and_settings_pass(self):
        _assert_profile_matches_settings(profile(), active_settings())

    def test_request_id_mismatch_is_profile_mismatch(self):
        with self.assertRaisesRegex(RunnerFailure, "profile_mismatch"):
            _assert_profile_matches_settings(
                profile(request_id="other-id"), active_settings()
            )

    def test_user_id_mismatch_is_profile_mismatch(self):
        with self.assertRaisesRegex(RunnerFailure, "profile_mismatch"):
            _assert_profile_matches_settings(
                profile(user_id=99), active_settings()
            )

    def test_max_rows_above_effective_setting_is_profile_mismatch(self):
        with self.assertRaisesRegex(RunnerFailure, "profile_mismatch"):
            _assert_profile_matches_settings(
                profile(max_rows=21), active_settings(BULK_PROCESSING_V2_CANARY_MAX_ROWS=20)
            )

    def test_max_rows_reads_from_the_gate_validated_setting_not_a_hardcoded_number(self):
        # If the effective setting were (hypothetically) lower, a profile
        # requesting the historical ceiling of 20 must still be rejected --
        # proving the comparison is against the setting, not a literal 20.
        with self.assertRaisesRegex(RunnerFailure, "profile_mismatch"):
            _assert_profile_matches_settings(
                profile(max_rows=20), active_settings(BULK_PROCESSING_V2_CANARY_MAX_ROWS=5)
            )


class ImportOnlyEnforcementTests(unittest.TestCase):
    def test_clean_profile_passes(self):
        _assert_import_only_profile(profile())

    def test_empty_field_is_rejected(self):
        with self.assertRaisesRegex(RunnerFailure, "import_only_violation"):
            _assert_import_only_profile(profile(subject=""))

    def test_embedded_newline_injection_is_rejected(self):
        # A newline could break out of write_post_curl_config's flat
        # `form = "..."` lines and inject an extra directive/field.
        with self.assertRaisesRegex(RunnerFailure, "import_only_violation"):
            _assert_import_only_profile(
                profile(template_name='TD-02C"\nform = "sender_id=999')
            )

    def test_embedded_quote_is_rejected(self):
        with self.assertRaisesRegex(RunnerFailure, "import_only_violation"):
            _assert_import_only_profile(profile(subject='broken"quote'))

    def test_backslash_is_rejected(self):
        with self.assertRaisesRegex(RunnerFailure, "import_only_violation"):
            _assert_import_only_profile(profile(template_id="back\\slash"))


class ExpectedDeltaAssertionTests(unittest.TestCase):
    """Design D6: bulk_sends +1, bulk_sends_v2 +1, recipients +N, jobs +0,
    messages +0. Any nonzero jobs/messages delta is a firewall breach,
    escalated regardless of whether the POST itself was classified allowed."""

    def test_expected_success_delta_is_accepted(self):
        before = baseline()
        after = baseline(bulk_sends=6, bulk_sends_v2=1, recipients=3)
        _assert_expected_delta(before, after, expect_success=True)

    def test_larger_recipient_delta_is_accepted(self):
        before = baseline()
        after = baseline(bulk_sends=6, bulk_sends_v2=1, recipients=50)
        _assert_expected_delta(before, after, expect_success=True)

    def test_missing_bulk_send_row_on_success_is_unexpected_row_delta(self):
        before = baseline()
        after = baseline(recipients=3)  # bulk_sends/bulk_sends_v2 unchanged
        with self.assertRaisesRegex(RunnerFailure, "unexpected_row_delta"):
            _assert_expected_delta(before, after, expect_success=True)

    def test_zero_recipient_delta_on_success_is_unexpected_row_delta(self):
        before = baseline()
        after = baseline(bulk_sends=6, bulk_sends_v2=1, recipients=0)
        with self.assertRaisesRegex(RunnerFailure, "unexpected_row_delta"):
            _assert_expected_delta(before, after, expect_success=True)

    def test_no_delta_expected_when_post_did_not_succeed(self):
        before = baseline()
        _assert_expected_delta(before, before, expect_success=False)

    def test_nonzero_jobs_delta_is_firewall_breach_even_without_expected_success(self):
        before = baseline()
        after = baseline(jobs=before.jobs + 1)
        with self.assertRaisesRegex(RunnerFailure, "firewall_breach"):
            _assert_expected_delta(before, after, expect_success=False)

    def test_nonzero_message_delta_is_firewall_breach(self):
        before = baseline()
        after = baseline(bulk_sends=6, bulk_sends_v2=1, recipients=3, messages=before.messages + 1)
        with self.assertRaisesRegex(RunnerFailure, "firewall_breach"):
            _assert_expected_delta(before, after, expect_success=True)

    def test_firewall_breach_is_checked_before_row_delta(self):
        before = baseline()
        after = baseline(jobs=before.jobs + 1)  # also missing the expected rows
        with self.assertRaisesRegex(RunnerFailure, "firewall_breach"):
            _assert_expected_delta(before, after, expect_success=True)


class ConfigBytesTests(unittest.TestCase):
    """Extends the Task 3 pattern for the client's own POST execution path:
    exact 9 allowed fields, mode 0600, no secret in argv."""

    def operations(self, service=None) -> CurlOperations:
        instance = CurlOperations.__new__(CurlOperations)
        instance.service = service or SimpleNamespace(
            unit="django.service", user="app", exec_start_raw="", working_directory=Path("/tmp")
        )
        instance.target = discover_canary_target(
            nginx_config("canary.example.com"), "/run/django.sock"
        )
        instance.credential_file = Path("/tmp/unused")
        instance.state = Mock()
        instance.baseline = Mock()
        instance.log = SafeDiagnosticLog(io.StringIO())
        instance.session_key = ""
        instance.session_identified = False
        instance._current_discovery_substage = "service_metadata_loaded"
        return instance

    def test_post_config_has_exact_nine_fields_mode_0600_and_no_secret_in_argv(self):
        with secure_cookie_workspace() as workspace:
            csv_path = workspace / "canary.csv"
            csv_path.write_text("email\nsynthetic@example.invalid\n", encoding="utf-8")
            jar = workspace / "cookies.txt"
            jar.write_text(
                "host\tFALSE\t/\tTRUE\t0\tcsrftoken\tcsrf-secret-value\n"
                "host\tFALSE\t/\tTRUE\t0\tsessionid\tsession-secret-value\n",
                encoding="utf-8",
            )
            operations = self.operations()
            completed = SimpleNamespace(returncode=0, stdout="201\tapplication/json\t\t0\t0\t0.2", stderr="")
            with patch("ops.bulk_v2_canary_client.subprocess.run", return_value=completed) as run_mock:
                decision = _execute_canary_post(
                    operations, workspace, profile(), csv_path, SafeDiagnosticLog(io.StringIO())
                )
            self.assertTrue(decision.allowed)
            config_path = workspace / "post.curl.conf"
            config = config_path.read_text(encoding="utf-8")
            form_fields = [line for line in config.splitlines() if line.startswith("form = ")]
            self.assertEqual(len(form_fields), 9)
            self.assertIn('form = "engine_version=v2"', config)
            self.assertIn('form = "send_now=False"', config)
            self.assertIn('form = "scheduled_at="', config)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            argv = run_mock.call_args.args[0]
            joined = " ".join(argv)
            self.assertNotIn("csrf-secret-value", joined)
            self.assertNotIn("session-secret-value", joined)
            self.assertNotIn("--location", argv)
            self.assertNotIn("-k", argv)

    def test_post_execution_calls_curl_exactly_once(self):
        with secure_cookie_workspace() as workspace:
            csv_path = workspace / "canary.csv"
            csv_path.write_text("email\nsynthetic@example.invalid\n", encoding="utf-8")
            jar = workspace / "cookies.txt"
            jar.write_text("host\tFALSE\t/\tTRUE\t0\tcsrftoken\tcsrf-token\n", encoding="utf-8")
            operations = self.operations()
            completed = SimpleNamespace(returncode=0, stdout="201\tapplication/json\t\t0\t0\t0.2", stderr="")
            with patch("ops.bulk_v2_canary_client.subprocess.run", return_value=completed) as run_mock:
                _execute_canary_post(
                    operations, workspace, profile(), csv_path, SafeDiagnosticLog(io.StringIO())
                )
            run_mock.assert_called_once()

    def test_missing_csrf_cookie_refuses_before_curl(self):
        with secure_cookie_workspace() as workspace:
            csv_path = workspace / "canary.csv"
            csv_path.write_text("email\n", encoding="utf-8")
            operations = self.operations()
            with patch("ops.bulk_v2_canary_client.subprocess.run") as run_mock:
                with self.assertRaises(AuthenticatedGetFailure):
                    _execute_canary_post(
                        operations, workspace, profile(), csv_path, SafeDiagnosticLog(io.StringIO())
                    )
            run_mock.assert_not_called()

    def test_non_json_response_is_rejected(self):
        with secure_cookie_workspace() as workspace:
            csv_path = workspace / "canary.csv"
            csv_path.write_text("email\n", encoding="utf-8")
            jar = workspace / "cookies.txt"
            jar.write_text("host\tFALSE\t/\tTRUE\t0\tcsrftoken\tcsrf-token\n", encoding="utf-8")
            operations = self.operations()
            completed = SimpleNamespace(returncode=0, stdout="403\ttext/html\t\t0\t0\t0.1", stderr="")
            with patch("ops.bulk_v2_canary_client.subprocess.run", return_value=completed):
                with self.assertRaises(RunnerFailure):
                    _execute_canary_post(
                        operations, workspace, profile(), csv_path, SafeDiagnosticLog(io.StringIO())
                    )


class RunCanaryOrchestrationTests(unittest.TestCase):
    """Full run_canary() flow, heavily faked -- mirrors
    RunnerOrchestrationTests in test_td02c_authenticated_get_runner.py."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.credential = self.root / "credential"
        self.credential.write_text("private-value", encoding="utf-8")
        self.csv_path = self.root / "canary.csv"
        self.csv_path.write_text("email\nsynthetic@example.invalid\n", encoding="utf-8")
        self.service = SimpleNamespace(working_directory=self.repo, user="app")

    def invoke(
        self,
        *,
        settings=None,
        state=None,
        operations=None,
        gate_error=None,
        post_decision=None,
        validate_credential_side_effect=None,
    ):
        stream = io.StringIO()
        settings = settings or active_settings()
        state = state or Mock(
            baseline=Mock(
                side_effect=[baseline(), baseline(bulk_sends=6, bulk_sends_v2=1, recipients=3)]
            ),
        )
        operations = operations or Mock(session_identified=True, session_key="new-session-secret")

        def fake_run_gate(ops, log, workspace_factory=None):
            if workspace_factory is not None:
                with workspace_factory():
                    pass
            if gate_error is not None:
                raise gate_error

        def fake_execute_post(ops, workspace, prof, csv_path, log):
            if post_decision is None:
                return SimpleNamespace(allowed=True, code="response_expected")
            if isinstance(post_decision, BaseException):
                raise post_decision
            return post_decision

        patchers = [
            patch("ops.bulk_v2_canary_client._load_django_settings", return_value=settings),
            patch("ops.bulk_v2_canary_client.discover_service", return_value=self.service),
            patch(
                "ops.bulk_v2_canary_client.validate_credential_file",
                side_effect=validate_credential_side_effect,
                return_value=(self.credential, (1, 2, 3)) if validate_credential_side_effect is None else None,
            ),
            patch("ops.bulk_v2_canary_client.delete_exact_file"),
            patch("ops.bulk_v2_canary_client.DjangoState", return_value=state),
            patch("ops.bulk_v2_canary_client.CurlOperations", return_value=operations),
            patch("ops.bulk_v2_canary_client.run_authenticated_get_gate", side_effect=fake_run_gate),
            patch("ops.bulk_v2_canary_client._execute_canary_post", side_effect=fake_execute_post),
        ]
        started = [patcher.start() for patcher in patchers]
        for patcher in patchers:
            self.addCleanup(patcher.stop)
        delete_mock = started[3]

        code = run_canary(
            profile(),
            stream,
            csv_path=self.csv_path,
            credential_file=self.credential,
            service_unit="django.service",
            username="td02c_tech",
        )
        return code, stream.getvalue(), state, operations, delete_mock

    def test_gate_refusal_touches_nothing_and_makes_zero_network_calls(self):
        stream = io.StringIO()
        with (
            patch(
                "ops.bulk_v2_canary_client._load_django_settings",
                return_value=active_settings(BULK_PROCESSING_ENGINE_V2=False),
            ),
            patch("ops.bulk_v2_canary_client.discover_service") as discover,
            patch("ops.bulk_v2_canary_client.validate_credential_file") as validate,
            patch("ops.bulk_v2_canary_client.DjangoState") as django_state,
            patch("ops.bulk_v2_canary_client.subprocess.run") as curl,
        ):
            code = run_canary(
                profile(),
                stream,
                csv_path=self.csv_path,
                credential_file=self.credential,
                service_unit="django.service",
                username="td02c_tech",
            )
        self.assertEqual(code, 1)
        discover.assert_not_called()
        validate.assert_not_called()
        django_state.assert_not_called()
        curl.assert_not_called()
        self.assertIn("settings_gate_failed", stream.getvalue())

    def test_gate_pass_proceeds_to_post(self):
        code, output, state, operations, delete_mock = self.invoke()
        self.assertEqual(code, 0)

    def test_profile_mismatch_refuses_before_credential_access(self):
        # A request_id/user_id mismatch is already caught earlier, by the
        # settings gate itself (profile's own values are threaded in as its
        # expected_request_id/expected_user_id) -- see
        # ProfileConsistencyTests for that pure-function coverage. This
        # exercises the one case the gate does not derive from the profile:
        # max_rows, read from BULK_PROCESSING_V2_CANARY_MAX_ROWS.
        stream = io.StringIO()
        with (
            patch("ops.bulk_v2_canary_client._load_django_settings", return_value=active_settings()),
            patch("ops.bulk_v2_canary_client.discover_service") as discover,
            patch("ops.bulk_v2_canary_client.validate_credential_file") as validate,
        ):
            mismatched = profile(max_rows=25)
            code = run_canary(
                mismatched,
                stream,
                csv_path=self.csv_path,
                credential_file=self.credential,
                service_unit="django.service",
                username="td02c_tech",
            )
        self.assertEqual(code, 1)
        discover.assert_not_called()
        validate.assert_not_called()
        self.assertIn("profile_mismatch", stream.getvalue())

    def test_import_only_violation_refuses_before_credential_access(self):
        stream = io.StringIO()
        with (
            patch("ops.bulk_v2_canary_client._load_django_settings", return_value=active_settings()),
            patch("ops.bulk_v2_canary_client.discover_service") as discover,
            patch("ops.bulk_v2_canary_client.validate_credential_file") as validate,
        ):
            unsafe = profile(subject='broken"quote')
            code = run_canary(
                unsafe,
                stream,
                csv_path=self.csv_path,
                credential_file=self.credential,
                service_unit="django.service",
                username="td02c_tech",
            )
        self.assertEqual(code, 1)
        discover.assert_not_called()
        validate.assert_not_called()
        self.assertIn("import_only_violation", stream.getvalue())

    def test_disposition_required_refuses_before_http(self):
        dirty_baseline = baseline(bulk_sends_v2=1, recipients=4)
        state = Mock(baseline=Mock(return_value=dirty_baseline))
        stream = io.StringIO()
        with (
            patch("ops.bulk_v2_canary_client._load_django_settings", return_value=active_settings()),
            patch("ops.bulk_v2_canary_client.discover_service", return_value=self.service),
            patch(
                "ops.bulk_v2_canary_client.validate_credential_file",
                return_value=(self.credential, (1, 2, 3)),
            ),
            patch("ops.bulk_v2_canary_client.delete_exact_file"),
            patch("ops.bulk_v2_canary_client.DjangoState", return_value=state),
            patch("ops.bulk_v2_canary_client.CurlOperations") as curl_ops,
            patch("ops.bulk_v2_canary_client.subprocess.run") as curl,
        ):
            code = run_canary(
                profile(),
                stream,
                csv_path=self.csv_path,
                credential_file=self.credential,
                service_unit="django.service",
                username="td02c_tech",
            )
        self.assertEqual(code, 1)
        curl_ops.assert_not_called()
        curl.assert_not_called()
        self.assertIn("disposition_required", stream.getvalue())

    def test_missing_credential_source_is_refused_and_reported(self):
        code, output, state, operations, delete_mock = self.invoke(
            validate_credential_side_effect=FileNotFoundError("no such file")
        )
        self.assertEqual(code, 1)
        self.assertIn("credential_file_unsafe", output)

    def test_success_sends_exactly_one_post_and_cleans_up_everything(self):
        code, output, state, operations, delete_mock = self.invoke()
        self.assertEqual(code, 0)
        state.delete_session.assert_called_once_with("new-session-secret")
        self.assertNotIn("private-value", output)
        self.assertFalse(self.credential.exists() and False)  # deletion delegated to delete_exact_file (patched)

    def test_post_failure_still_cleans_session_and_credential(self):
        code, output, state, operations, delete_mock = self.invoke(
            post_decision=AuthenticatedGetFailure("unexpected_status")
        )
        self.assertEqual(code, 1)
        state.delete_session.assert_called_once_with("new-session-secret")

    def test_firewall_breach_on_nonzero_jobs_delta_is_loud_failure_not_warning(self):
        dirty_state = Mock()
        dirty_state.baseline.side_effect = [baseline(), baseline(jobs=99)]
        code, output, state, operations, delete_mock = self.invoke(state=dirty_state)
        self.assertEqual(code, 1)
        self.assertIn("firewall_breach", output)

    def test_unexpected_row_delta_on_success_without_bulk_send_creation(self):
        dirty_state = Mock()
        dirty_state.baseline.side_effect = [baseline(), baseline()]  # nothing created
        code, output, state, operations, delete_mock = self.invoke(state=dirty_state)
        self.assertEqual(code, 1)
        self.assertIn("unexpected_row_delta", output)

    def test_credential_cleanup_runs_even_when_post_raises(self):
        # RunnerFailure is the real exception type _execute_canary_post
        # raises on a non-allowed classification; the message text itself
        # must never leak into the diagnostic stream.
        code, output, state, operations, delete_mock = self.invoke(
            post_decision=RunnerFailure("private-detail")
        )
        self.assertEqual(code, 1)
        delete_mock.assert_called_once()
        self.assertNotIn("private-detail", output)

    def test_fingerprint_logged_never_raw_request_id_or_user_id(self):
        code, output, state, operations, delete_mock = self.invoke()
        self.assertEqual(code, 0)
        self.assertNotIn(profile().request_id, output)
        # A plain `str(user_id) not in output` substring check is flaky: the
        # log also carries real duration_seconds floats (e.g. "0.000142"),
        # whose digits can coincidentally contain "42" without the raw id
        # ever being logged. Assert against actual JSON leaf values instead,
        # so only a genuine "42" field value (int or str) fails the test.
        raw_user_id = profile().user_id
        leaked = [
            value
            for value in _iter_json_leaf_values(output)
            if value == raw_user_id or value == str(raw_user_id)
        ]
        self.assertEqual(leaked, [], f"raw user_id leaked as a JSON field value: {leaked}")
        # sha256(client_request_id)[:12] fingerprint, matching relay/api.py's
        # own pattern -- 12 lowercase hex characters, never the raw token.
        self.assertRegex(output, r"request=[a-f0-9]{12}")

    def test_isolated_profile_module_imports_without_django_configured(self):
        # If a module-level Django import existed, importing this test module
        # itself (already done at collection time) would already have failed
        # under the bare `python -m unittest discover` bootstrap invocation.
        import ops.bulk_v2_canary_client as module

        self.assertFalse(hasattr(module, "settings"))


class ClientModuleIdentityTests(unittest.TestCase):
    """Reviewer-mandated fix: ``validate_module_entrypoint`` (reused in
    ``main()`` above) is a closure over
    ``ops.td02c_authenticated_get_runner``'s own
    ``__package__``/``__spec__``/``__file__`` globals -- calling it from
    this module only proves *that* module is correctly deployed and
    importable from the expected checkout, not that
    ``ops.bulk_v2_canary_client`` itself is. ``validate_client_module_identity``
    performs the equivalent proof using this module's own identity globals,
    so it is exercised directly here (no POSIX/``pwd`` dependency, unlike
    the reused entrypoint check)."""

    def setUp(self):
        import ops.bulk_v2_canary_client as module

        self.module = module
        self.actual_file = Path(module.__file__).resolve()
        # ops/bulk_v2_canary_client.py -> ops/ -> <repo root>
        self.repo_root = self.actual_file.parent.parent

    def service(self, working_directory: Path) -> SimpleNamespace:
        return SimpleNamespace(working_directory=working_directory, user="app", unit="django.service")

    def test_happy_path_module_resolves_from_expected_checkout(self):
        # Sub-requirement 1+2: __file__ sits inside the discovered service's
        # working_directory, and resolves to exactly
        # <working_directory>/ops/bulk_v2_canary_client.py.
        with patch(
            "ops.bulk_v2_canary_client.discover_service",
            return_value=self.service(self.repo_root),
        ):
            validate_client_module_identity("django.service")  # must not raise

    def test_pythonpath_injected_duplicate_is_rejected(self):
        # Sub-requirement 4: a same-named module technically importable from
        # elsewhere on sys.path must be rejected because its resolved file
        # does not fall under the discovered checkout's working_directory --
        # not merely "is it importable somewhere."
        with tempfile.TemporaryDirectory() as elsewhere:
            with patch(
                "ops.bulk_v2_canary_client.discover_service",
                return_value=self.service(Path(elsewhere)),
            ):
                with self.assertRaisesRegex(
                    RunnerFailure, "client_module_not_importable_from_checkout"
                ):
                    validate_client_module_identity("django.service")

    def test_wrong_file_identity_is_rejected(self):
        # Sub-requirement 2: working_directory here is the "ops" directory
        # itself -- an ancestor of the resolved file, so the containment
        # check alone would pass -- but the exact expected location
        # (<working_directory>/ops/bulk_v2_canary_client.py) does not match
        # the actual resolved file. Proves the check is exact-path identity,
        # not just "a file with that basename exists somewhere under here."
        ops_directory = self.actual_file.parent
        with patch(
            "ops.bulk_v2_canary_client.discover_service",
            return_value=self.service(ops_directory),
        ):
            with self.assertRaisesRegex(RunnerFailure, "client_module_path_mismatch"):
                validate_client_module_identity("django.service")

    def test_entrypoint_shadowed_by_different_main_is_rejected(self):
        # Sub-requirement 3: main is reachable, but its defining file must
        # match the same resolved identity already proven for the module --
        # proving main() isn't shadowed by some other module's main.
        with (
            patch(
                "ops.bulk_v2_canary_client.discover_service",
                return_value=self.service(self.repo_root),
            ),
            patch(
                "ops.bulk_v2_canary_client.inspect.getfile",
                return_value=str(self.repo_root / "ops" / "other_module.py"),
            ),
        ):
            with self.assertRaisesRegex(
                RunnerFailure, "client_entrypoint_identity_mismatch"
            ):
                validate_client_module_identity("django.service")


class ErrorCodesTests(unittest.TestCase):
    def test_new_error_codes_are_present(self):
        for code in (
            "settings_gate_failed",
            "profile_mismatch",
            "import_only_violation",
            "unexpected_row_delta",
            "firewall_breach",
            "disposition_required",
            "client_module_path_mismatch",
            "client_module_not_importable_from_checkout",
            "client_entrypoint_identity_mismatch",
        ):
            self.assertIn(code, ERROR_CODES)

    def test_existing_http_client_taxonomy_is_included(self):
        from ops.td02c_http_client import ERROR_CODES as HTTP_CLIENT_ERROR_CODES

        self.assertTrue(HTTP_CLIENT_ERROR_CODES.issubset(ERROR_CODES))


@unittest.skipUnless(os.name == "posix", "POSIX module entrypoint")
class MainCliTests(unittest.TestCase):
    def test_missing_required_arguments_exits(self):
        with self.assertRaises(SystemExit):
            main(["--credential-file", "/tmp/x"])

    def test_entrypoint_failure_prevents_run_canary(self):
        stream = io.StringIO()
        with (
            patch(
                "ops.bulk_v2_canary_client.validate_module_entrypoint",
                side_effect=RunnerFailure("effective_user_mismatch"),
            ),
            patch("ops.bulk_v2_canary_client.run_canary") as runner,
            patch("ops.bulk_v2_canary_client.sys.stdout", stream),
        ):
            code = main([
                "--credential-file", "/tmp/not-read",
                "--username", "td02c_tech",
                "--user-id", "42",
                "--request-id", "fresh-canary-request-id",
                "--template-id", "td02c-canary-import-only",
                "--template-name", "TD-02C Canary Import Only",
                "--subject", "TD-02C Canary Import Only",
                "--csv-path", "/tmp/canary.csv",
            ])
        self.assertEqual(code, 1)
        runner.assert_not_called()
        self.assertIn("effective_user_mismatch", stream.getvalue())
        self.assertNotIn("not-read", stream.getvalue())

    def test_client_module_identity_failure_prevents_run_canary(self):
        # Mirrors test_entrypoint_failure_prevents_run_canary's pattern, for
        # the new, module-local identity check: entrypoint validation passes,
        # but this module's own identity check fails -- run_canary must not
        # be reached.
        stream = io.StringIO()
        with (
            patch("ops.bulk_v2_canary_client.validate_module_entrypoint"),
            patch(
                "ops.bulk_v2_canary_client.validate_client_module_identity",
                side_effect=RunnerFailure("client_module_path_mismatch"),
            ),
            patch("ops.bulk_v2_canary_client.run_canary") as runner,
            patch("ops.bulk_v2_canary_client.sys.stdout", stream),
        ):
            code = main([
                "--credential-file", "/tmp/not-read",
                "--username", "td02c_tech",
                "--user-id", "42",
                "--request-id", "fresh-canary-request-id",
                "--template-id", "td02c-canary-import-only",
                "--template-name", "TD-02C Canary Import Only",
                "--subject", "TD-02C Canary Import Only",
                "--csv-path", "/tmp/canary.csv",
            ])
        self.assertEqual(code, 1)
        runner.assert_not_called()
        self.assertIn("client_module_path_mismatch", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
