from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

try:
    import pwd
except ImportError:
    pwd = None  # type: ignore[assignment]

from ops.deployment_hardening import (
    DeploymentContext,
    NginxTarget,
    Runner,
    ServiceMetadata,
)
from ops.td02c_deployment_runner import (
    Config,
    Evidence,
    ProductionBackend,
    RUNTIME_PATHS,
    TD02CDeploymentError,
    main,
    repository_lock,
    validate_deploy_check_output,
    validate_single_invocation,
)


class ContractTests(unittest.TestCase):
    def test_deploy_check_accepts_only_explicit_warning_allowlist(self):
        self.assertEqual(validate_deploy_check_output(""), set())
        self.assertEqual(
            validate_deploy_check_output("WARNINGS:\n?: (security.W005)\n?: (security.W021)"),
            {"W005", "W021"},
        )
        for output in (
            "?: (security.W999)",
            "SystemCheckError: ERROR",
            "WARNING without a code",
        ):
            with self.subTest(output=output), self.assertRaises(TD02CDeploymentError):
                validate_deploy_check_output(output)

    def test_preflight_does_not_refresh_or_fetch(self):
        config = Config(
            mode="preflight-only", service_unit="django.service", worker_unit="worker",
            old_sha="1" * 40, target_sha="2" * 40, remote="origin",
            branch="production", expected_commits=(), backup_root=None,
            validation_evidence=None, evidence_output=Path("C:/outside/evidence.json"),
            allowed_warnings=(),
        )
        backend = ProductionBackend(config, Mock(), Mock())
        service = Mock(working_directory=Path("/repo"), user="app")
        context = Mock(runtime_files=list(RUNTIME_PATHS), changed_files=[], runtime_hashes={})
        with (
            patch("ops.td02c_deployment_runner.discover_service", return_value=service),
            patch.object(backend, "_require_service_identity"),
            patch("ops.td02c_deployment_runner.repository_lock", return_value=unittest.mock.MagicMock(__enter__=Mock(), __exit__=Mock(return_value=False))),
            patch("ops.td02c_deployment_runner.preflight", return_value=context),
            patch("ops.td02c_deployment_runner.refresh_deployment_ref") as refresh,
            patch.object(backend, "_validate_python"),
            patch.object(backend, "_assert_runtime_unchanged"),
            patch.object(backend, "_validate_operational_gates"),
        ):
            with self.assertRaises(TD02CDeploymentError):  # evidence is mandatory
                backend.preflight()
        refresh.assert_not_called()
    def test_only_package_module_invocation_is_accepted(self):
        validate_single_invocation(
            ["python", "-m", "ops.td02c_deployment_runner", "--mode", "preflight-only"]
        )

    def test_direct_file_and_duplicated_shell_logic_are_rejected(self):
        commands = (
            ["python", "ops/td02c_deployment_runner.py"],
            ["python", "-m", "ops.td02c_deployment_runner", "git merge"],
            ["python", "-m", "ops.td02c_deployment_runner", "/tmp/td02c-pycache-root"],
            ["python", "-m", "ops.td02c_deployment_runner", "curl https://example"],
        )
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(TD02CDeploymentError):
                    validate_single_invocation(command)

    def test_modes_dispatch_to_one_versioned_backend(self):
        for mode, method in (
            ("preflight-only", "preflight"),
            ("deploy-only", "deploy"),
            ("rollback", "rollback"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                evidence = Path(directory) / "evidence.json"
                backend = Mock()
                with patch(
                    "ops.td02c_deployment_runner.ProductionBackend",
                    return_value=backend,
                ):
                    code = main([
                        "--mode", mode,
                        "--old-sha", "1" * 40,
                        "--target-sha", "2" * 40,
                        "--branch", "production",
                        "--evidence-output", str(evidence),
                    ])
                self.assertEqual(code, 0)
                getattr(backend, method).assert_called_once_with()
                if os.name == "posix":
                    self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o600)

    def test_evidence_cleanup_removes_atomic_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            evidence = Evidence(output)
            evidence.emit("preflight", "PASS", "safe")
            evidence.save()
            self.assertEqual(list(Path(directory).glob(".evidence.json.*.tmp")), [])
            self.assertEqual(json.loads(output.read_text())["events"][0]["classification"], "safe")

    @unittest.skipUnless(os.name == "posix", "POSIX evidence permissions")
    def test_evidence_rejects_corruption_symlink_and_checkout_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            corrupt = root / "corrupt.json"
            corrupt.write_text("{", encoding="utf-8")
            corrupt.chmod(0o600)
            with self.assertRaisesRegex(TD02CDeploymentError, "evidence_json_invalid"):
                Evidence(corrupt)
            target = root / "target.json"
            target.write_text('{"events": []}', encoding="utf-8")
            target.chmod(0o600)
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaises(TD02CDeploymentError):
                Evidence(link)
            checkout = root / "repo"
            checkout.mkdir(mode=0o700)
            evidence = Evidence(checkout / "evidence.json", checkout)
            with self.assertRaisesRegex(TD02CDeploymentError, "evidence_inside_checkout"):
                evidence.save()

    def test_failure_returns_phase_specific_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.json"
            backend = Mock()
            backend.preflight.side_effect = TD02CDeploymentError(
                "preflight", "synthetic_failure"
            )
            with patch(
                "ops.td02c_deployment_runner.ProductionBackend",
                return_value=backend,
            ):
                code = main([
                    "--mode", "preflight-only", "--old-sha", "1" * 40,
                    "--target-sha", "2" * 40, "--branch", "production",
                    "--evidence-output", str(evidence),
                ])
            self.assertEqual(code, 20)
            event = json.loads(evidence.read_text())["events"][-1]
            self.assertEqual(event["exit_code"], 20)


@unittest.skipUnless(os.name == "posix" and pwd is not None, "Linux Git integration")
class EndToEndRepositoryFlowTests(unittest.TestCase):
    class HarnessRunner(Runner):
        def run(self, args, **kwargs):
            if "unittest" in args:
                return subprocess.CompletedProcess(args, 0, "")
            return super().run(args, **kwargs)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "production")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "ops").mkdir()
        (self.repo / "ops" / "tool.py").write_text("value = 1\n", encoding="utf-8")
        (self.repo / "ops" / "old.py").write_text("legacy = True\n", encoding="utf-8")
        for name in RUNTIME_PATHS:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("baseline\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "old")
        self.old = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("checkout", "-qb", "target")
        (self.repo / "ops" / "tool.py").write_text("value = 2\n", encoding="utf-8")
        (self.repo / "ops" / "new.py").write_text("new = True\n", encoding="utf-8")
        (self.repo / "ops" / "old.py").unlink()
        self.git("add", "-A", "ops")
        self.git("commit", "-qm", "target")
        self.target = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("checkout", "-q", "production")
        for index, name in enumerate(RUNTIME_PATHS):
            (self.repo / name).write_text(f"runtime-{index}\n", encoding="utf-8")
        self.runtime_before = self.runtime_manifest()
        self.user = pwd.getpwuid(os.geteuid()).pw_name
        self.context = self.make_context()
        self.evidence_path = self.root / "evidence.json"
        self.config = Config(
            mode="deploy-only", service_unit="django.service",
            worker_unit="worker.service", old_sha=self.old,
            target_sha=self.target, remote="origin", branch="production",
            expected_commits=(self.target,), backup_root=self.root,
            validation_evidence=None, evidence_output=self.evidence_path,
            allowed_warnings=(),
        )

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )

    def runtime_manifest(self):
        result = {}
        for name in RUNTIME_PATHS:
            path = self.repo / name
            info = path.stat()
            result[name] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid,
                info.st_size, info.st_mtime_ns,
            )
        return result

    def make_context(self):
        hashes = {name: values[0] for name, values in self.runtime_before.items()}
        metadata = {
            name: {
                "sha256": values[0], "mode": values[1], "uid": values[2],
                "gid": values[3], "size": values[4], "mtime_ns": values[5],
            }
            for name, values in self.runtime_before.items()
        }
        service = ServiceMetadata(
            unit="django.service", working_directory=self.repo,
            exec_start_path=Path(sys.executable), exec_start_raw="",
            python=Path(sys.executable), user=self.user, group=self.user,
            main_pid=1, fragment_path=self.repo / "unit", environment_files=(),
        )
        return DeploymentContext(
            service=service, nginx=NginxTarget("example.invalid", 443, "/tmp/x", None),
            old_sha=self.old, target_sha=self.target, repository=self.repo,
            branch="production", remote="origin",
            changed_files=["ops/new.py", "ops/old.py", "ops/tool.py"],
            runtime_files=list(RUNTIME_PATHS), intersections=[],
            runtime_hashes=hashes, baseline_smoke={}, baseline_warning_codes=set(),
            approved_commits=[self.target], runtime_metadata=metadata,
        )

    def backend(self, *, fail_phase=""):
        evidence = Evidence(self.evidence_path)
        backend = ProductionBackend(self.config, evidence, self.HarnessRunner())
        def preflight():
            backend.context = self.context
            return self.context
        backend._preflight_unlocked = Mock(side_effect=preflight)

        def validate_python(phase):
            if phase == fail_phase:
                raise TD02CDeploymentError(phase, "synthetic_failure")
            evidence.emit(phase, "PASS", "synthetic_pycompile_pass")

        def validate_gates(phase):
            if phase == fail_phase:
                raise TD02CDeploymentError(phase, "synthetic_failure")

        backend._validate_python = validate_python
        backend._validate_operational_gates = validate_gates
        return backend

    def deploy_with_patches(self, backend):
        with (
            patch("ops.td02c_deployment_runner.write_backup", return_value=self.root / "backup"),
            patch("ops.td02c_deployment_runner.run_manage_check", return_value=("", set())),
            patch("ops.td02c_deployment_runner.refresh_deployment_ref"),
            patch("ops.td02c_deployment_runner.discover_service", return_value=self.context.service),
        ):
            backend.deploy()

    def test_full_fast_forward_preserves_modified_runtime(self):
        backend = self.backend()
        self.deploy_with_patches(backend)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.target)
        self.assertEqual(self.runtime_manifest(), self.runtime_before)

    def test_failure_before_merge_leaves_head_untouched(self):
        backend = self.backend()
        backend._preflight_unlocked = Mock(side_effect=TD02CDeploymentError("preflight", "fail"))
        with self.assertRaises(TD02CDeploymentError):
            self.deploy_with_patches(backend)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.old)
        self.assertEqual(self.runtime_manifest(), self.runtime_before)

    def test_failure_after_merge_rolls_back_complete_range(self):
        backend = self.backend(fail_phase="postmerge_pycompile")
        with self.assertRaises(TD02CDeploymentError):
            self.deploy_with_patches(backend)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.old)
        self.assertEqual((self.repo / "ops" / "tool.py").read_text(), "value = 1\n")
        self.assertFalse((self.repo / "ops" / "new.py").exists())
        self.assertEqual((self.repo / "ops" / "old.py").read_text(), "legacy = True\n")
        self.assertEqual(self.runtime_manifest(), self.runtime_before)

    def test_evidence_write_failure_after_merge_rolls_back(self):
        backend = self.backend()
        original_save = backend.evidence.save
        backend.evidence.save = Mock(side_effect=TD02CDeploymentError("evidence", "synthetic_write_failure"))
        with self.assertRaises(TD02CDeploymentError):
            self.deploy_with_patches(backend)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.old)
        self.assertEqual(self.runtime_manifest(), self.runtime_before)
        backend.evidence.save = original_save

    def test_wrong_materialized_owner_aborts_and_rolls_back(self):
        backend = self.backend()
        fake_account = Mock(pw_uid=os.geteuid() + 10000)
        with patch("ops.td02c_deployment_runner.pwd.getpwnam", return_value=fake_account):
            with self.assertRaisesRegex(TD02CDeploymentError, "materialized_path_wrong_owner"):
                self.deploy_with_patches(backend)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.old)

    def test_repeated_end_to_end_runs_are_deterministic(self):
        for _ in range(3):
            backend = self.backend()
            self.deploy_with_patches(backend)
            self.assertEqual(self.runtime_manifest(), self.runtime_before)
            from ops.deployment_hardening import targeted_rollback
            targeted_rollback(
                type("Args", (), {"branch": "production"})(),
                Runner(), self.context, [],
            )
            self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.old)
            self.assertEqual(self.runtime_manifest(), self.runtime_before)

    def test_real_lock_rejects_concurrent_holder_and_accepts_stale_metadata(self):
        with repository_lock(self.repo, "first") as lock_path:
            with self.assertRaisesRegex(TD02CDeploymentError, "operation_already_running"):
                with repository_lock(self.repo, "second"):
                    self.fail("second holder acquired active lock")
        self.assertTrue(lock_path.exists())
        with repository_lock(self.repo, "after-release"):
            pass

    def test_two_real_processes_cannot_hold_same_repository_lock(self):
        ready = self.root / "lock-ready"
        code = (
            "import pathlib,sys,time; "
            "from ops.td02c_deployment_runner import repository_lock; "
            "repo=pathlib.Path(sys.argv[1]); ready=pathlib.Path(sys.argv[2]); "
            "ctx=repository_lock(repo,'child'); ctx.__enter__(); "
            "ready.write_text('ready'); time.sleep(3); ctx.__exit__(None,None,None)"
        )
        project = Path(__file__).resolve().parents[2]
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(self.repo), str(ready)],
            cwd=project, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 2
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "child did not acquire lock")
            with self.assertRaisesRegex(TD02CDeploymentError, "operation_already_running"):
                with repository_lock(self.repo, "parent"):
                    self.fail("parent acquired active child lock")
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_sigint_and_sigterm_after_merge_trigger_targeted_rollback(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                backend = self.backend()
                original = backend._validate_python
                def terminate_after_merge(phase, current=signum):
                    if phase == "postmerge_pycompile":
                        os.kill(os.getpid(), current)
                    original(phase)
                backend._validate_python = terminate_after_merge
                with self.assertRaises(TD02CDeploymentError):
                    self.deploy_with_patches(backend)
                self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.old)
                self.assertEqual(self.runtime_manifest(), self.runtime_before)

    def test_sigint_and_sigterm_before_merge_do_not_mutate_git_and_release_lock(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                backend = self.backend()
                with (
                    patch("ops.td02c_deployment_runner.discover_service", return_value=self.context.service),
                    patch("ops.td02c_deployment_runner.refresh_deployment_ref", side_effect=lambda *a, current=signum, **k: os.kill(os.getpid(), current)),
                ):
                    with self.assertRaises(TD02CDeploymentError):
                        backend.deploy()
                self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.old)
                with repository_lock(self.repo, "after-signal"):
                    pass


if __name__ == "__main__":
    unittest.main()
