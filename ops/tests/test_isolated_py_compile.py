from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import pwd
except ImportError:
    pwd = None  # type: ignore[assignment]

from ops.isolated_py_compile import (
    PyCompileValidationError,
    isolated_py_compile,
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

        def inspect(argv, *, cwd, user):
            if argv[0] == "git":
                return subprocess.run(
                    argv,
                    cwd=cwd,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            prefix = next(item for item in argv if item.startswith("PYTHONPYCACHEPREFIX="))
            cache = Path(prefix.partition("=")[2])
            metadata = cache.stat(follow_symlinks=False)
            observed.update(mode=stat.S_IMODE(metadata.st_mode), uid=metadata.st_uid, user=user)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch("ops.isolated_py_compile._run_as_user", side_effect=inspect):
            self.run_compile()
        self.assertEqual(observed, {"mode": 0o700, "uid": os.geteuid(), "user": self.user})
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
        with patch(
            "ops.isolated_py_compile._run_as_user",
            side_effect=[subprocess.CompletedProcess([], 0, "", ""), RuntimeError("stop")],
        ):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                self.run_compile()
        self.assertEqual(list(self.cache_root.iterdir()), [])
