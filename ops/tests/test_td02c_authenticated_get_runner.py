from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import pwd
except ImportError:
    pwd = None  # type: ignore[assignment]

from ops.td02c_authenticated_get_runner import (
    Baseline,
    CurlOperations,
    EXPECTED_MODULE,
    RunnerFailure,
    delete_exact_file,
    main,
    resolve_authorized_user,
    run,
    validate_credential_file,
    validate_module_entrypoint,
)
from ops.td02c_http_client import AuthenticatedGetFailure
from ops.deployment_hardening import DeploymentError, NginxTarget


@unittest.skipUnless(os.name == "posix", "POSIX credential metadata")
class CredentialFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.user = pwd.getpwuid(os.geteuid()).pw_name

    def credential(self, mode=0o600):
        path = self.root / "credential"
        path.write_text("not-a-real-password", encoding="utf-8")
        path.chmod(mode)
        return path

    def test_valid_credential_is_external_0600_and_deleted_by_identity(self):
        path = self.credential()
        resolved, identity = validate_credential_file(path, self.repo, self.user)
        delete_exact_file(resolved, identity)
        self.assertFalse(path.exists())

    def test_missing_credential_fails(self):
        with self.assertRaises(FileNotFoundError):
            validate_credential_file(self.root / "missing", self.repo, self.user)

    def test_wrong_mode_fails(self):
        with self.assertRaisesRegex(RunnerFailure, "unsafe"):
            validate_credential_file(self.credential(0o640), self.repo, self.user)

    def test_symlink_fails(self):
        target = self.credential()
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaisesRegex(RunnerFailure, "unsafe"):
            validate_credential_file(link, self.repo, self.user)

    def test_credential_inside_repository_fails(self):
        path = self.repo / "credential"
        path.write_text("secret", encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaisesRegex(RunnerFailure, "unsafe"):
            validate_credential_file(path, self.repo, self.user)

    def test_changed_inode_is_not_deleted(self):
        path = self.credential()
        resolved, identity = validate_credential_file(path, self.repo, self.user)
        path.unlink()
        path.write_text("replacement", encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaisesRegex(RunnerFailure, "cleanup"):
            delete_exact_file(resolved, identity)
        self.assertTrue(path.exists())


class FakeState:
    def __init__(self):
        self.before = Baseline(3, frozenset({"old"}), 10, 0, 0, 4, 20)
        self.deleted = []
        self.asserted = False

    def baseline(self):
        return self.before

    def delete_session(self, key):
        self.deleted.append(key)

    def assert_functional_unchanged(self, before):
        self.asserted = before == self.before


class FakeOperations:
    def __init__(self, **kwargs):
        self.session_identified = kwargs.get("identified", True)
        self.session_key = kwargs.get("key", "new-session-secret")


@unittest.skipUnless(os.name == "posix", "POSIX runner metadata")
class RunnerOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"; self.repo.mkdir()
        self.credential = self.root / "credential"
        self.credential.write_text("private-value", encoding="utf-8")
        self.credential.chmod(0o600)
        self.user = pwd.getpwuid(os.geteuid()).pw_name
        self.args = SimpleNamespace(
            service_unit="django.service", credential_file=self.credential,
            user_id=7, username="operador_td02c",
        )
        self.service = SimpleNamespace(working_directory=self.repo, user=self.user)

    def execute(self, gate_side_effect=None, operations=None):
        stream = io.StringIO(); state = FakeState(); operations = operations or FakeOperations()
        with (
            patch("ops.td02c_authenticated_get_runner.discover_service", return_value=self.service),
            patch("ops.td02c_authenticated_get_runner.DjangoState", return_value=state),
            patch("ops.td02c_authenticated_get_runner.CurlOperations", return_value=operations),
            patch("ops.td02c_authenticated_get_runner.run_authenticated_get_gate", side_effect=gate_side_effect) as gate,
        ):
            code = run(self.args, stream)
        return code, stream.getvalue(), state, gate

    def test_success_invokes_versioned_gate_and_cleans_session_and_credential(self):
        code, output, state, gate = self.execute()
        self.assertEqual(code, 0); gate.assert_called_once()
        self.assertEqual(state.deleted, ["new-session-secret"])
        self.assertTrue(state.asserted); self.assertFalse(self.credential.exists())
        self.assertNotIn("private-value", output); self.assertNotIn("new-session-secret", output)

    def test_login_failure_still_cleans_credential_without_unowned_session(self):
        code, output, state, _ = self.execute(
            AuthenticatedGetFailure("authentication_failed"), FakeOperations(identified=False)
        )
        self.assertEqual(code, 1); self.assertEqual(state.deleted, [])
        self.assertFalse(self.credential.exists()); self.assertNotIn("private-value", output)

    def test_tls_connection_csrf_and_http_failures_are_fail_closed(self):
        for error in ("tls_failed", "connection_failed", "csrf_cookie_missing", "unexpected_status", "unexpected_content_type"):
            with self.subTest(error=error):
                self.credential.write_text("private-value", encoding="utf-8"); self.credential.chmod(0o600)
                code, output, _, _ = self.execute(AuthenticatedGetFailure(error), FakeOperations(identified=False))
                self.assertEqual(code, 1); self.assertNotIn("private-value", output)

    def test_cleanup_failure_is_fail_closed(self):
        state = FakeState(); state.delete_session = Mock(side_effect=RuntimeError("private"))
        stream = io.StringIO()
        with (
            patch("ops.td02c_authenticated_get_runner.discover_service", return_value=self.service),
            patch("ops.td02c_authenticated_get_runner.DjangoState", return_value=state),
            patch("ops.td02c_authenticated_get_runner.CurlOperations", return_value=FakeOperations()),
            patch("ops.td02c_authenticated_get_runner.run_authenticated_get_gate"),
        ):
            self.assertEqual(run(self.args, stream), 1)
        self.assertIn("session_cleanup_failed", stream.getvalue())
        self.assertNotIn("private", stream.getvalue())

    def test_no_orm_existing_session_lookup_fallback_exists(self):
        source = Path("ops/td02c_authenticated_get_runner.py").read_text(encoding="utf-8")
        self.assertNotIn("order_by(\"-expire_date\")", source)
        self.assertNotIn("existing_session", source)
        self.assertIn("run_authenticated_get_gate(operations, log)", source)

    def test_wrapper_contract_is_only_the_versioned_runner(self):
        command = "python -m ops.td02c_authenticated_get_runner --credential-file <0600-path>"
        self.assertNotIn("curl", command); self.assertNotIn("manage.py shell", command)

    def test_session_is_identified_from_new_login_cookie(self):
        workspace = self.root / "workspace"; workspace.mkdir(mode=0o700)
        jar = workspace / "cookies.txt"
        jar.write_text("host\tFALSE\t/\tTRUE\t0\tcsrftoken\tcsrf-private\nhost\tFALSE\t/\tTRUE\t0\tsessionid\tnew-private\n", encoding="utf-8")
        jar.chmod(0o600)
        state = Mock(); log = Mock()
        operations = CurlOperations.__new__(CurlOperations)
        operations.credential_file = self.credential; operations.state = state
        operations.baseline = FakeState().before; operations.log = log
        operations.session_key = ""; operations.session_identified = False
        operations._curl = Mock(return_value=SimpleNamespace(status=302))
        operations.authenticate(workspace, NginxTarget("example.test", 443, "/run/django.sock", None))
        state.identify_new_session.assert_called_once_with(operations.baseline, "new-private")
        self.assertTrue(operations.session_identified)

    def test_multiple_new_sessions_are_ambiguous_and_not_claimed(self):
        workspace = self.root / "workspace"; workspace.mkdir(mode=0o700)
        jar = workspace / "cookies.txt"
        jar.write_text("host\tFALSE\t/\tTRUE\t0\tcsrftoken\tcsrf-private\nhost\tFALSE\t/\tTRUE\t0\tsessionid\tnew-private\n", encoding="utf-8")
        jar.chmod(0o600)
        state = Mock(); state.identify_new_session.side_effect = RunnerFailure("session_identification_ambiguous")
        operations = CurlOperations.__new__(CurlOperations)
        operations.credential_file = self.credential; operations.state = state
        operations.baseline = FakeState().before; operations.log = Mock()
        operations.session_key = ""; operations.session_identified = False
        operations._curl = Mock(return_value=SimpleNamespace(status=302))
        with self.assertRaises(AuthenticatedGetFailure):
            operations.authenticate(workspace, NginxTarget("example.test", 443, "/run/django.sock", None))
        self.assertFalse(operations.session_identified)

    def test_session_not_created_is_fail_closed(self):
        workspace = self.root / "workspace"; workspace.mkdir(mode=0o700)
        jar = workspace / "cookies.txt"
        jar.write_text("host\tFALSE\t/\tTRUE\t0\tcsrftoken\tcsrf-private\n", encoding="utf-8")
        jar.chmod(0o600)
        operations = CurlOperations.__new__(CurlOperations)
        operations.credential_file = self.credential; operations.state = Mock()
        operations.baseline = FakeState().before; operations.log = Mock()
        operations.session_key = ""; operations.session_identified = False
        operations._curl = Mock(return_value=SimpleNamespace(status=200))
        with self.assertRaises(AuthenticatedGetFailure):
            operations.authenticate(workspace, NginxTarget("example.test", 443, "/run/django.sock", None))


class FakeOrmUser:
    """Plain Python stand-in for a Django User row -- no ORM/settings needed.

    ops/tests/*.py must import and run cleanly under bare `python -m
    unittest discover` (the exact mechanism the production bootstrap gate
    uses internally): no DJANGO_SETTINGS_MODULE, no configured apps. A
    module-level Django import anywhere in this file breaks that for
    every test in it, not just the ones needing it -- confirmed for real
    against production (a first version of this test class imported
    django.test.TestCase at module level and broke the entire suite's
    import under the gate's bare-unittest invocation, aborting a real
    deploy cleanly before any merge). resolve_authorized_user() is
    therefore designed to take the ORM lookup and permission check as
    injected callables, so its own logic is testable without Django.
    """

    def __init__(self, pk, username, *, is_active=True, is_staff=True, is_superuser=False):
        self.pk = pk
        self.username = username
        self.is_active = is_active
        self.is_staff = is_staff
        self.is_superuser = is_superuser


class ParameterizedRunnerUserValidationTests(unittest.TestCase):
    """Coverage for resolve_authorized_user(), DjangoState's injectable core."""

    def _lookup(self, user, *, expect_id=None, expect_username=None):
        def lookup(user_id, username):
            if expect_id is not None:
                self.assertEqual(user_id, expect_id)
            if expect_username is not None:
                self.assertEqual(username, expect_username)
            return user
        return lookup

    def test_valid_technical_user_with_matching_id_and_username(self):
        user = FakeOrmUser(7, "td02c_tech")
        result = resolve_authorized_user(
            7, "td02c_tech", lookup=self._lookup(user, expect_id=7, expect_username="td02c_tech"),
            can_operate_bulk_sends=lambda u: True,
        )
        self.assertIs(result, user)

    def test_username_id_mismatch_is_rejected(self):
        # the lookup itself is what enforces exact id+username correspondence;
        # a mismatch means no such row exists, so it returns None
        with self.assertRaises(RunnerFailure):
            resolve_authorized_user(
                7, "not-the-real-username", lookup=lambda uid, uname: None,
                can_operate_bulk_sends=lambda u: True,
            )

    def test_nonexistent_user_id_is_rejected(self):
        with self.assertRaises(RunnerFailure):
            resolve_authorized_user(
                999999, "ghost", lookup=lambda uid, uname: None, can_operate_bulk_sends=lambda u: True,
            )

    def test_empty_username_is_rejected(self):
        with self.assertRaises(RunnerFailure):
            resolve_authorized_user(
                1, "", lookup=lambda uid, uname: FakeOrmUser(1, ""), can_operate_bulk_sends=lambda u: True,
            )

    def test_whitespace_only_username_is_rejected(self):
        with self.assertRaises(RunnerFailure):
            resolve_authorized_user(
                1, "   ", lookup=lambda uid, uname: FakeOrmUser(1, "   "), can_operate_bulk_sends=lambda u: True,
            )

    def test_inactive_user_is_rejected(self):
        user = FakeOrmUser(7, "td02c_inactive", is_active=False)
        with self.assertRaises(RunnerFailure):
            resolve_authorized_user(
                7, "td02c_inactive", lookup=lambda uid, uname: user, can_operate_bulk_sends=lambda u: True,
            )

    def test_non_staff_user_is_rejected(self):
        user = FakeOrmUser(7, "td02c_nonstaff", is_staff=False)
        with self.assertRaises(RunnerFailure):
            resolve_authorized_user(
                7, "td02c_nonstaff", lookup=lambda uid, uname: user, can_operate_bulk_sends=lambda u: True,
            )

    def test_insufficient_permission_is_rejected(self):
        user = FakeOrmUser(7, "td02c_noperm")
        with self.assertRaises(RunnerFailure):
            resolve_authorized_user(
                7, "td02c_noperm", lookup=lambda uid, uname: user, can_operate_bulk_sends=lambda u: False,
            )

    def test_permission_check_is_fully_delegated_not_reimplemented(self):
        """Proves the gate never second-guesses can_operate_bulk_sends --
        it grants exactly when that callable says so, whatever internal
        permission path (relay.change_bulksend or
        relay_super.change_bulksenduserconfigproxy) it used to decide.
        relay.services.operator_permissions.can_operate_bulk_sends itself
        already implements both paths; that real, unchanged function is
        wired in by DjangoState.__init__, not reimplemented here.
        """
        user = FakeOrmUser(7, "td02c_perm")
        for allowed in (True, False):
            with self.subTest(allowed=allowed):
                can_operate = Mock(return_value=allowed)
                if allowed:
                    result = resolve_authorized_user(
                        7, "td02c_perm", lookup=lambda uid, uname: user, can_operate_bulk_sends=can_operate,
                    )
                    self.assertIs(result, user)
                else:
                    with self.assertRaises(RunnerFailure):
                        resolve_authorized_user(
                            7, "td02c_perm", lookup=lambda uid, uname: user, can_operate_bulk_sends=can_operate,
                        )
                can_operate.assert_called_once_with(user)

    def test_superuser_attribute_is_never_inspected(self):
        source = Path("ops/td02c_authenticated_get_runner.py").read_text(encoding="utf-8")
        function_source = source[source.index("def resolve_authorized_user"):]
        function_source = function_source[:function_source.index("\n\n\n")]
        self.assertNotIn("is_superuser", function_source)

    def test_superuser_not_required_end_to_end(self):
        user = FakeOrmUser(7, "td02c_plain", is_superuser=False)
        result = resolve_authorized_user(
            7, "td02c_plain", lookup=lambda uid, uname: user, can_operate_bulk_sends=lambda u: True,
        )
        self.assertFalse(result.is_superuser)


@unittest.skipUnless(os.name == "posix", "POSIX CLI argument parsing")
class ParameterizedRunnerCliTests(unittest.TestCase):
    def test_missing_user_id_argument_exits(self):
        with self.assertRaises(SystemExit):
            main(["--credential-file", "/tmp/x", "--username", "someone"])

    def test_missing_username_argument_exits(self):
        with self.assertRaises(SystemExit):
            main(["--credential-file", "/tmp/x", "--user-id", "7"])

    def test_non_numeric_user_id_argument_exits(self):
        with self.assertRaises(SystemExit):
            main(["--credential-file", "/tmp/x", "--user-id", "not-a-number", "--username", "someone"])

    def test_username_is_never_sourced_from_credential_file(self):
        source = Path("ops/td02c_authenticated_get_runner.py").read_text(encoding="utf-8")
        self.assertIn('username={self.state.user.username}', source)
        self.assertNotIn("credential_file.read_text", source)
        self.assertNotIn("json.loads(self.credential_file", source)
        self.assertNotIn("json.loads(credential", source)

    def test_no_hardcoded_user_identity_remains(self):
        source = Path("ops/td02c_authenticated_get_runner.py").read_text(encoding="utf-8")
        self.assertNotIn("EXPECTED_USER_ID", source)
        self.assertNotIn("EXPECTED_USERNAME", source)
        self.assertNotIn('"ricardo"', source)


@unittest.skipUnless(os.name == "posix", "POSIX module entrypoint")
class ModuleEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.repository = Path.cwd().resolve()
        self.service = SimpleNamespace(
            working_directory=self.repository,
            user="app",
        )

    def validate(self, *, cwd=None, service=None, effective_user="app", path=None):
        module = sys.modules[EXPECTED_MODULE]
        with (
            patch.object(module, "__package__", "ops"),
            patch.object(module, "__spec__", SimpleNamespace(name=EXPECTED_MODULE)),
            patch("ops.td02c_authenticated_get_runner.discover_service", return_value=service or self.service),
            patch("ops.td02c_authenticated_get_runner.Path.cwd", return_value=cwd or self.repository),
            patch("ops.td02c_authenticated_get_runner.os.geteuid", return_value=1000),
            patch("ops.td02c_authenticated_get_runner.pwd.getpwuid", return_value=SimpleNamespace(pw_name=effective_user)),
            patch.object(sys, "path", path or [str(self.repository)]),
        ):
            validate_module_entrypoint("django.service")

    def test_module_entrypoint_valid_from_discovered_repository(self):
        self.validate()

    def test_package_is_importable_from_repository(self):
        result = subprocess.run(
            [sys.executable, "-c", "import ops.td02c_authenticated_get_runner"],
            cwd=self.repository, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_module_help_executes_but_direct_file_is_unsupported(self):
        module_result = subprocess.run(
            [sys.executable, "-m", EXPECTED_MODULE, "--help"],
            cwd=self.repository, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
        direct_result = subprocess.run(
            [sys.executable, "ops/td02c_authenticated_get_runner.py", "--help"],
            cwd=self.repository, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(module_result.returncode, 0, module_result.stderr)
        self.assertNotEqual(direct_result.returncode, 0)
        self.assertIn("No module named 'ops'", direct_result.stderr)

    def test_wrong_working_directory_fails_before_credential_or_http(self):
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaisesRegex(RunnerFailure, "working_directory_mismatch"):
                self.validate(cwd=Path(other).resolve())

    def test_discovered_working_directory_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as other:
            service = SimpleNamespace(working_directory=Path(other), user="app")
            with self.assertRaisesRegex(RunnerFailure, "working_directory_mismatch"):
                self.validate(service=service)

    def test_wrong_effective_user_fails(self):
        with self.assertRaisesRegex(RunnerFailure, "effective_user_mismatch"):
            self.validate(effective_user="root")

    def test_missing_repository_import_root_fails(self):
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaisesRegex(RunnerFailure, "repository_not_importable"):
                self.validate(path=[other])

    def test_entrypoint_failure_does_not_read_credential_or_run_http_cleanup(self):
        module = sys.modules[EXPECTED_MODULE]
        stream = io.StringIO()
        with (
            patch("ops.td02c_authenticated_get_runner.validate_module_entrypoint", side_effect=RunnerFailure("effective_user_mismatch")),
            patch("ops.td02c_authenticated_get_runner.run") as runner,
            patch.object(module.sys, "stdout", stream),
        ):
            self.assertEqual(
                module.main(["--credential-file", "not-read", "--user-id", "1", "--username", "someone"]), 1
            )
        runner.assert_not_called()
        diagnostic = stream.getvalue()
        self.assertIn('"substage": "entrypoint_validated"', diagnostic)
        self.assertIn('"classification": "effective_user_mismatch"', diagnostic)
        self.assertNotIn("not-read", diagnostic)

    def test_user_switch_failure_does_not_read_credential_create_workspace_or_http(self):
        module = sys.modules[EXPECTED_MODULE]
        stream = io.StringIO()
        with (
            patch(
                "ops.td02c_authenticated_get_runner.validate_module_entrypoint",
                side_effect=DeploymentError(
                    "cannot_switch_user: effective user does not match service user"
                ),
            ),
            patch("ops.td02c_authenticated_get_runner.run") as runner,
            patch("ops.td02c_http_client.tempfile.mkdtemp") as workspace,
            patch.object(module.sys, "stdout", stream),
        ):
            self.assertEqual(
                module.main(["--credential-file", "not-read", "--user-id", "1", "--username", "someone"]), 1
            )
        runner.assert_not_called()
        workspace.assert_not_called()
        diagnostic = stream.getvalue()
        self.assertIn('"classification": "cannot_switch_user"', diagnostic)
        self.assertNotIn("not-read", diagnostic)


def nginx_config(*server_names: str, socket: str = "/run/django.sock", certificate: str | None = None) -> str:
    cert_line = f"ssl_certificate {certificate};" if certificate else ""
    return "\n".join(
        f"""
        server {{
            listen 443 ssl;
            server_name {server_name};
            {cert_line}
            location / {{ proxy_pass http://unix:{socket}; }}
        }}
        """
        for server_name in server_names
    )


EXEC_START_RAW = "{ path=/opt/app/.venv/bin/gunicorn ; argv[]=/opt/app/.venv/bin/gunicorn --bind unix:/run/django.sock ; }"


def completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@unittest.skipUnless(os.name == "posix", "POSIX discovery diagnostics")
class NginxDiscoveryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.log_stream = io.StringIO()
        from ops.td02c_http_client import SafeDiagnosticLog

        self.log = SafeDiagnosticLog(self.log_stream)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = SimpleNamespace(
            unit="django.service",
            user="app",
            exec_start_raw=EXEC_START_RAW,
            working_directory=Path(self.temp.name),
        )

    def operations(self, service=None) -> CurlOperations:
        instance = CurlOperations.__new__(CurlOperations)
        instance.service = service or self.service
        instance.target = None
        instance.credential_file = Path("/tmp/unused")
        instance.state = Mock()
        instance.baseline = Mock()
        instance.log = self.log
        instance.session_key = ""
        instance.session_identified = False
        instance._current_discovery_substage = "service_metadata_loaded"
        return instance

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.log_stream.getvalue().splitlines()]

    def last_failure(self) -> dict:
        failures = [record for record in self.records() if record["result"] == "FAIL"]
        self.assertTrue(failures, "expected at least one FAIL substage")
        return failures[-1]

    def dispatch(self, handlers):
        """Return a subprocess.run side_effect keyed by the command's first two tokens."""
        defaults = {("systemctl", "is-active"): completed(0, "active\n", "")}

        def side_effect(argv, **kwargs):
            key = tuple(argv[:2])
            if key in handlers:
                result = handlers[key]
                if isinstance(result, BaseException):
                    raise result
                return result
            return defaults.get(key, completed(0, "", ""))

        return side_effect

    def run_discovery(self, handlers, *, service=None):
        operations = self.operations(service)
        patcher = patch(
            "ops.td02c_authenticated_get_runner.subprocess.run", side_effect=self.dispatch(handlers)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return operations, operations.discover_target

    # 1. systemctl falla ----------------------------------------------------
    def test_systemctl_not_active_is_classified(self):
        _, call = self.run_discovery({("systemctl", "is-active"): completed(3, "inactive\n", "")})
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            call()
        self.assertEqual(caught.exception.error_class, "systemctl_failed")
        self.assertEqual(self.last_failure()["stage"], "service_metadata_loaded")

    # 2. metadata del servicio inválida --------------------------------------
    def test_invalid_service_metadata_is_classified(self):
        service = SimpleNamespace(unit="", user="app", exec_start_raw=EXEC_START_RAW, working_directory=Path("/opt/app"))
        _, call = self.run_discovery({}, service=service)
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            call()
        self.assertEqual(caught.exception.error_class, "service_metadata_invalid")

    # 3. WorkingDirectory ausente o incorrecta -------------------------------
    def test_invalid_working_directory_is_classified(self):
        service = SimpleNamespace(
            unit="django.service", user="app", exec_start_raw=EXEC_START_RAW,
            working_directory=Path("/does/not/exist/at/all"),
        )
        _, call = self.run_discovery(
            {("systemctl", "is-active"): completed(0, "active\n", "")}, service=service
        )
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            call()
        self.assertEqual(caught.exception.error_class, "working_directory_invalid")

    # 4. nginx -t falla -------------------------------------------------------
    def test_nginx_test_failure_is_classified(self):
        _, call = self.run_discovery({
            ("systemctl", "is-active"): completed(0, "active\n", ""),
            ("nginx", "-t"): completed(1, "", "nginx: [emerg] invalid directive\n"),
        })
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            call()
        self.assertEqual(caught.exception.error_class, "nginx_test_failed")
        self.assertEqual(self.last_failure()["stage"], "nginx_config_tested")

    # 5. nginx -T falla ---------------------------------------------------
    def test_nginx_dump_failure_is_classified(self):
        _, call = self.run_discovery({
            ("systemctl", "is-active"): completed(0, "active\n", ""),
            ("nginx", "-t"): completed(0, "", ""),
            ("nginx", "-T"): completed(1, "", "nginx: configuration file test failed\n"),
        })
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            call()
        self.assertEqual(caught.exception.error_class, "nginx_dump_failed")

    # 6. nginx -T devuelve salida vacia ---------------------------------------
    def test_nginx_empty_output_is_classified(self):
        _, call = self.run_discovery({
            ("systemctl", "is-active"): completed(0, "active\n", ""),
            ("nginx", "-t"): completed(0, "", ""),
            ("nginx", "-T"): completed(0, "   \n", ""),
        })
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            call()
        self.assertEqual(caught.exception.error_class, "nginx_output_empty")

    # 7. salida malformada (parsing exception) -------------------------------
    def test_unparseable_output_is_classified(self):
        _, call = self.run_discovery({
            ("systemctl", "is-active"): completed(0, "active\n", ""),
            ("nginx", "-t"): completed(0, "", ""),
            ("nginx", "-T"): completed(0, "server { listen 443 ssl;", ""),
        })
        with patch(
            "ops.td02c_authenticated_get_runner._parse_vhost_candidates",
            side_effect=ValueError("boom"),
        ):
            with self.assertRaises(AuthenticatedGetFailure) as caught:
                call()
        self.assertEqual(caught.exception.error_class, "nginx_output_unparseable")

    # 8. cero vhosts ----------------------------------------------------------
    def test_zero_vhosts_is_classified(self):
        config = nginx_config()  # no server blocks at all
        _, call = self.run_discovery({
            ("systemctl", "is-active"): completed(0, "active\n", ""),
            ("nginx", "-t"): completed(0, "", ""),
            ("nginx", "-T"): completed(0, config or "http {}\n", ""),
        })
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            call()
        self.assertEqual(caught.exception.error_class, "no_vhost_found")

    # 9. multiples vhosts -------------------------------------------------
    def test_multiple_vhosts_is_classified(self):
        config = nginx_config("one.example.com", "two.example.com")
        _, call = self.run_discovery({
            ("systemctl", "is-active"): completed(0, "active\n", ""),
            ("nginx", "-t"): completed(0, "", ""),
            ("nginx", "-T"): completed(0, config, ""),
        })
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            call()
        self.assertEqual(caught.exception.error_class, "multiple_vhosts_found")
        self.assertIn("one.example.com", self.last_failure().get("detail", ""))

    # 10. wildcard ----------------------------------------------------------
    def test_wildcard_vhost_is_rejected(self):
        config = nginx_config("*.example.com")
        _, call = self.run_discovery({
            ("systemctl", "is-active"): completed(0, "active\n", ""),
            ("nginx", "-t"): completed(0, "", ""),
            ("nginx", "-T"): completed(0, config, ""),
        })
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            call()
        self.assertEqual(caught.exception.error_class, "wildcard_vhost_rejected")

    # 11. variable de nginx ---------------------------------------------------
    def test_variable_vhost_is_rejected(self):
        config = nginx_config("$host")
        _, call = self.run_discovery({
            ("systemctl", "is-active"): completed(0, "active\n", ""),
            ("nginx", "-t"): completed(0, "", ""),
            ("nginx", "-T"): completed(0, config, ""),
        })
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            call()
        self.assertEqual(caught.exception.error_class, "variable_vhost_rejected")

    # 12. certificado ausente -------------------------------------------------
    def test_missing_certificate_file_is_classified(self):
        config = nginx_config("canary.example.com", certificate="/does/not/exist.pem")
        _, call = self.run_discovery({
            ("systemctl", "is-active"): completed(0, "active\n", ""),
            ("nginx", "-t"): completed(0, "", ""),
            ("nginx", "-T"): completed(0, config, ""),
        })
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            call()
        self.assertEqual(caught.exception.error_class, "certificate_not_found")

    # 13. certificado no corresponde al hostname ------------------------------
    def test_certificate_hostname_mismatch_is_classified(self):
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as handle:
            handle.write(b"not-a-real-cert")
            cert_path = handle.name
        self.addCleanup(lambda: os.unlink(cert_path))
        config = nginx_config("canary.example.com", certificate=cert_path)
        _, call = self.run_discovery({
            ("systemctl", "is-active"): completed(0, "active\n", ""),
            ("nginx", "-t"): completed(0, "", ""),
            ("nginx", "-T"): completed(0, config, ""),
            ("openssl", "x509"): completed(1, "", "certificate does not cover host\n"),
        })
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            call()
        self.assertEqual(caught.exception.error_class, "certificate_hostname_mismatch")

    # 14. resolucion local invalida -------------------------------------------
    def test_local_resolution_invalid_is_classified(self):
        bad_target = NginxTarget("canary.example.com", 8443, "relative/socket", None)
        config = nginx_config("canary.example.com", certificate=None)
        _, call = self.run_discovery({
            ("systemctl", "is-active"): completed(0, "active\n", ""),
            ("nginx", "-t"): completed(0, "", ""),
            ("nginx", "-T"): completed(0, config, ""),
        })
        with patch("ops.td02c_authenticated_get_runner.discover_nginx_target", return_value=bad_target):
            with self.assertRaises(AuthenticatedGetFailure) as caught:
                call()
        self.assertEqual(caught.exception.error_class, "local_resolution_invalid")

    # 15. permiso denegado ----------------------------------------------------
    def test_permission_denied_is_classified(self):
        for command in (("systemctl", "is-active"), ("nginx", "-t")):
            with self.subTest(command=command):
                self.log_stream.truncate(0); self.log_stream.seek(0)
                _, call = self.run_discovery({command: PermissionError("denied")})
                with self.assertRaises(AuthenticatedGetFailure) as caught:
                    call()
                self.assertEqual(caught.exception.error_class, "command_permission_denied")

    def test_nginx_permission_denied_stderr_is_classified(self):
        _, call = self.run_discovery({
            ("systemctl", "is-active"): completed(0, "active\n", ""),
            ("nginx", "-t"): completed(1, "", "nginx: [emerg] open() failed (13: Permission denied)\n"),
        })
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            call()
        self.assertEqual(caught.exception.error_class, "command_permission_denied")

    # 16. comando inexistente --------------------------------------------------
    def test_command_not_found_is_classified(self):
        _, call = self.run_discovery({
            ("systemctl", "is-active"): completed(0, "active\n", ""),
            ("nginx", "-t"): FileNotFoundError("no such file"),
        })
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            call()
        self.assertEqual(caught.exception.error_class, "command_not_found")

    # 17. excepcion inesperada --------------------------------------------------
    def test_unexpected_exception_is_classified(self):
        operations = self.operations()
        with patch("ops.td02c_authenticated_get_runner.subprocess.run",
                    side_effect=self.dispatch({
                        ("systemctl", "is-active"): MemoryError("unexpected"),
                    })):
            with self.assertRaises(AuthenticatedGetFailure) as caught:
                operations.discover_target()
        self.assertEqual(caught.exception.error_class, "unexpected_discovery_error")
        self.assertEqual(self.last_failure()["stage"], "service_metadata_loaded")

    # 23. mensajes de error sanitizados ------------------------------------
    def test_error_detail_is_redacted(self):
        _, call = self.run_discovery({
            ("systemctl", "is-active"): completed(0, "active\n", ""),
            ("nginx", "-t"): completed(1, "", "SECRET_KEY=abcd1234\nnginx: [emerg] bad config\n"),
        })
        with self.assertRaises(AuthenticatedGetFailure):
            call()
        detail = self.last_failure().get("detail", "")
        self.assertNotIn("abcd1234", detail)

    # 24. exito conserva el comportamiento anterior --------------------------
    def test_successful_discovery_returns_expected_target(self):
        config = nginx_config("canary.example.com")
        operations, call = self.run_discovery({
            ("systemctl", "is-active"): completed(0, "active\n", ""),
            ("nginx", "-t"): completed(0, "", ""),
            ("nginx", "-T"): completed(0, config, ""),
        })
        target = call()
        self.assertEqual(target.server_name, "canary.example.com")
        self.assertIs(operations.target, target)
        records = self.records()
        self.assertTrue(all(record["result"] == "PASS" for record in records))
        stages = [record["stage"] for record in records]
        for expected_stage in (
            "service_metadata_loaded", "service_working_directory_validated",
            "nginx_config_tested", "nginx_config_dumped", "vhost_candidates_parsed",
            "unique_vhost_selected", "certificate_paths_discovered",
            "certificate_hostname_validated", "local_resolution_prepared",
            "nginx_target_validated",
        ):
            self.assertIn(expected_stage, stages)

    # 20/21. cero HTTP y cero sesion cuando falla el discovery ----------------
    def test_discovery_failure_never_reaches_http_or_session_stages(self):
        operations, call = self.run_discovery({
            ("systemctl", "is-active"): completed(3, "inactive\n", ""),
        })
        operations.get_login = Mock()
        operations.authenticate = Mock()
        operations.authenticated_get = Mock()
        with self.assertRaises(AuthenticatedGetFailure):
            call()
        operations.get_login.assert_not_called()
        operations.authenticate.assert_not_called()
        operations.authenticated_get.assert_not_called()
        output = self.log_stream.getvalue()
        for forbidden in ("sessionid", "csrftoken"):
            self.assertNotIn(forbidden, output)

    # 22. cero exposicion de secretos ------------------------------------------
    def test_no_secrets_in_discovery_diagnostics(self):
        operations = self.operations()
        operations.credential_file = Path("/tmp/super-secret-marker-value")
        with patch("ops.td02c_authenticated_get_runner.subprocess.run",
                    side_effect=self.dispatch({
                        ("systemctl", "is-active"): completed(0, "active\n", ""),
                        ("nginx", "-t"): completed(0, "", ""),
                        ("nginx", "-T"): completed(0, nginx_config("canary.example.com"), ""),
                    })):
            operations.discover_target()
        self.assertNotIn("super-secret-marker-value", self.log_stream.getvalue())


if __name__ == "__main__":
    unittest.main()
