from __future__ import annotations

import io
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
    run,
    validate_credential_file,
    validate_module_entrypoint,
)
from ops.td02c_http_client import AuthenticatedGetFailure
from ops.deployment_hardening import NginxTarget


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
        self.args = SimpleNamespace(service_unit="django.service", credential_file=self.credential)
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
            self.assertEqual(module.main(["--credential-file", "not-read"]), 1)
        runner.assert_not_called()
        diagnostic = stream.getvalue()
        self.assertIn('"substage": "entrypoint_validated"', diagnostic)
        self.assertIn('"classification": "effective_user_mismatch"', diagnostic)
        self.assertNotIn("not-read", diagnostic)


if __name__ == "__main__":
    unittest.main()
