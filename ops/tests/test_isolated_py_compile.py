from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import pwd
except ImportError:
    pwd = None  # type: ignore[assignment]

from ops.isolated_py_compile import (
    PyCompileValidationError,
    _cleanup_on_termination,
    _validate_workspace,
    _write_probe,
    isolated_py_compile,
    isolated_py_compile_ephemeral,
    unique_cache_workspace,
    validate_cache_root,
)


@unittest.skipUnless(
    os.name == "posix" and pwd is not None,
    "Linux/POSIX operational validation",
)
class IsolatedPyCompileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.cache_root = self.root / "cache-root"
        self.repo.mkdir()
        self.cache_root.mkdir(mode=0o700)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        self.source = self.repo / "ops" / "tool.py"
        self.source.parent.mkdir()
        self.source.write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.repo, check=True)
        self.user = pwd.getpwuid(os.geteuid()).pw_name

    def run_compile(self):
        return isolated_py_compile(
            repository=self.repo,
            python=Path(sys.executable),
            service_user=self.user,
            sources=[Path("ops/tool.py")],
            cache_root=self.cache_root,
        )

    def run_ephemeral_compile(self):
        return isolated_py_compile_ephemeral(
            repository=self.repo,
            python=Path(sys.executable),
            service_user=self.user,
            sources=[Path("ops/tool.py")],
            temporary_parent=self.root,
        )

    def test_ephemeral_compile_uses_real_system_temp_and_cleans_everything(self):
        temporary_parent = Path(tempfile.gettempdir()).resolve(strict=True)
        before_workspaces = set(temporary_parent.glob("td02c-pycache-parent-*"))
        before_status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=self.repo, text=True, capture_output=True, check=True,
        ).stdout
        before_bytecode = set(self.repo.rglob("*.pyc"))

        result = isolated_py_compile_ephemeral(
            repository=self.repo,
            python=Path(sys.executable),
            service_user=self.user,
            sources=[Path("ops/tool.py")],
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            set(temporary_parent.glob("td02c-pycache-parent-*")),
            before_workspaces,
        )
        self.assertEqual(set(self.repo.rglob("*.pyc")), before_bytecode)
        self.assertEqual(
            subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=self.repo, text=True, capture_output=True, check=True,
            ).stdout,
            before_status,
        )

    def test_ephemeral_compile_ignores_incompatible_fixed_cache_root(self):
        obsolete = self.root / "td02c-pycache-root"
        obsolete.mkdir(mode=0o500)
        try:
            self.assertEqual(self.run_ephemeral_compile().returncode, 0)
            self.assertEqual(
                [path for path in self.root.iterdir() if path.name.startswith("td02c-pycache-parent-")],
                [],
            )
        finally:
            obsolete.chmod(0o700)

    def test_ephemeral_parent_is_cleaned_after_compile_exception(self):
        with patch(
            "ops.isolated_py_compile.isolated_py_compile",
            side_effect=RuntimeError("synthetic failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                self.run_ephemeral_compile()
        self.assertEqual(
            [path for path in self.root.iterdir() if path.name.startswith("td02c-pycache-parent-")],
            [],
        )

    def test_nonwritable_checkout_pycache_does_not_block_valid_compile(self):
        checkout_cache = self.source.parent / "__pycache__"
        checkout_cache.mkdir(mode=0o500)
        before = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=self.repo, text=True, capture_output=True, check=True,
        ).stdout
        result = self.run_compile()
        after = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=self.repo, text=True, capture_output=True, check=True,
        ).stdout
        self.assertEqual(result.returncode, 0)
        self.assertEqual(before, after)
        self.assertEqual(list(checkout_cache.glob("*.pyc")), [])
        self.assertEqual(list(self.cache_root.iterdir()), [])

    def test_syntax_error_is_nonzero_and_cache_is_removed(self):
        self.source.write_text("def broken(:\n", encoding="utf-8")
        result = self.run_compile()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SyntaxError", result.stdout)
        self.assertEqual(list(self.cache_root.iterdir()), [])

    def test_temporary_cache_is_0700_and_owned_by_operational_user(self):
        observed = {}
        from ops import isolated_py_compile as module
        original = module._run_as_user

        def inspect(argv, *, cwd, user):
            if argv[0] == "env":
                prefix = next(item for item in argv if item.startswith("PYTHONPYCACHEPREFIX="))
                cache = Path(prefix.partition("=")[2])
                metadata = cache.stat(follow_symlinks=False)
                observed.update(
                    mode=stat.S_IMODE(metadata.st_mode),
                    uid=metadata.st_uid,
                    gid=metadata.st_gid,
                    user=user,
                )
            return original(argv, cwd=cwd, user=user)

        with patch("ops.isolated_py_compile._run_as_user", side_effect=inspect):
            self.run_compile()
        self.assertEqual(
            observed,
            {
                "mode": 0o700,
                "uid": os.geteuid(),
                "gid": os.getegid(),
                "user": self.user,
            },
        )
        self.assertEqual(list(self.cache_root.iterdir()), [])

    def test_cache_root_inside_repository_is_rejected(self):
        with self.assertRaisesRegex(PyCompileValidationError, "outside"):
            validate_cache_root(self.repo, self.source.parent)

    def test_symlink_cache_root_is_rejected(self):
        link = self.root / "cache-link"
        try:
            link.symlink_to(self.cache_root, target_is_directory=True)
        except OSError:
            self.skipTest("symlink unavailable")
        with self.assertRaisesRegex(PyCompileValidationError, "symlink"):
            validate_cache_root(self.repo, link)

    def test_cleanup_occurs_when_runner_raises(self):
        from ops import isolated_py_compile as module
        original = module._run_as_user

        def fail_compile(argv, *, cwd, user):
            if argv[0] == "env":
                raise RuntimeError("stop")
            return original(argv, cwd=cwd, user=user)

        with patch("ops.isolated_py_compile._run_as_user", side_effect=fail_compile):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                self.run_compile()
        self.assertEqual(list(self.cache_root.iterdir()), [])

    def test_unique_workspace_per_execution_and_no_collision(self):
        seen = []
        barrier = threading.Barrier(2)

        def allocate():
            with unique_cache_workspace(
                repository=self.repo,
                cache_root=self.cache_root,
                service_user=self.user,
            ) as workspace:
                seen.append(workspace)
                barrier.wait(timeout=5)

        threads = [threading.Thread(target=allocate) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(set(seen)), 2)
        self.assertEqual(list(self.cache_root.iterdir()), [])

    def test_incompatible_fixed_sibling_does_not_affect_run(self):
        obsolete = self.root / "td02c-pycache-root"
        obsolete.mkdir(mode=0o500)
        try:
            self.assertEqual(self.run_compile().returncode, 0)
        finally:
            obsolete.chmod(0o700)

    def test_relative_workspace_is_rejected(self):
        with self.assertRaisesRegex(PyCompileValidationError, "ambiguous"):
            _validate_workspace(
                repository=self.repo,
                workspace=Path("relative"),
                service_user=self.user,
            )

    def test_workspace_inside_checkout_is_rejected(self):
        workspace = self.repo / "td02c-pycache-inside"
        workspace.mkdir(mode=0o700)
        with self.assertRaisesRegex(PyCompileValidationError, "unsafe"):
            _validate_workspace(
                repository=self.repo,
                workspace=workspace,
                service_user=self.user,
            )

    def test_wrong_mode_is_rejected(self):
        workspace = self.root / "td02c-pycache-mode"
        workspace.mkdir(mode=0o755)
        with self.assertRaisesRegex(PyCompileValidationError, "0700"):
            _validate_workspace(
                repository=self.repo,
                workspace=workspace,
                service_user=self.user,
            )

    def test_wrong_owner_is_rejected(self):
        workspace = self.root / "td02c-pycache-owner"
        workspace.mkdir(mode=0o700)
        account = pwd.getpwnam(self.user)
        with patch("ops.isolated_py_compile._account") as account_lookup:
            account_lookup.return_value = type(
                "Account", (), {"pw_uid": account.pw_uid + 1, "pw_gid": account.pw_gid}
            )()
            with self.assertRaisesRegex(PyCompileValidationError, "owner"):
                _validate_workspace(
                    repository=self.repo,
                    workspace=workspace,
                    service_user=self.user,
                )

    def test_symlink_workspace_is_rejected(self):
        target = self.root / "td02c-pycache-target"
        target.mkdir(mode=0o700)
        link = self.root / "td02c-pycache-link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(PyCompileValidationError, "ambiguous"):
            _validate_workspace(
                repository=self.repo, workspace=link, service_user=self.user
            )

    def test_effective_write_probe_success_and_failure(self):
        workspace = self.root / "td02c-pycache-probe"
        workspace.mkdir(mode=0o700)
        _write_probe(workspace, self.user)
        self.assertEqual(list(workspace.iterdir()), [])
        with patch(
            "ops.isolated_py_compile._run_as_user",
            return_value=subprocess.CompletedProcess([], 1, "denied", ""),
        ):
            with self.assertRaisesRegex(PyCompileValidationError, "probe failed"):
                _write_probe(workspace, self.user)

    def test_termination_signal_runs_cleanup(self):
        with self.assertRaisesRegex(InterruptedError, "signal"):
            with _cleanup_on_termination():
                with unique_cache_workspace(
                    repository=self.repo,
                    cache_root=self.cache_root,
                    service_user=self.user,
                ):
                    os.kill(os.getpid(), signal.SIGTERM)
        self.assertEqual(list(self.cache_root.iterdir()), [])
