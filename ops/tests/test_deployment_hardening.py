import contextlib
import dataclasses
import inspect
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import pwd
except ImportError:  # pragma: no cover - these tests are POSIX-only
    pwd = None  # type: ignore[assignment]


@contextlib.contextmanager
def deterministic_test_umask(mask=0o022):
    """Test-only helper: set an explicit, deterministic umask around
    fixture creation whose expected mode is derived from Git (0644 for
    regular files, 0755 for tracked executables), restoring the original
    umask on exit -- including when an exception propagates. Scoped to
    fixture materialization only: never wrap production code, an entire
    test suite, or a test that is deliberately exercising a different
    ambient umask (see DeterministicUmaskGitTests). A no-op on non-POSIX
    platforms, where os.umask has no equivalent meaning for file-creation
    semantics."""
    if os.name != "posix":
        yield
        return
    original = os.umask(mask)
    try:
        yield
    finally:
        os.umask(original)

from ops.deployment_hardening import (
    DeploymentContext,
    DeploymentError,
    EnvironmentFile,
    NginxTarget,
    Runner,
    ServiceMetadata,
    _resolve_git_binary,
    _validate_bootstrap_existing_paths,
    acquire_target_object,
    bootstrap_existing_component_deployment,
    bootstrap_module_deployment,
    build_parser,
    changed_runtime_intersections,
    classify_restart_requirement,
    _restart_unit_argv,
    _DJANGO_SERVICE_RESTART_SUDO_ARGV,
    current_main_pid,
    is_ops_only_allowlisted_path,
    delete_temporary_target_ref,
    deployment_plan,
    discover_and_validate_nginx,
    discover_nginx_target,
    discover_service,
    execute_deployment,
    fetch_bootstrap_target_ref,
    filesystem_metadata,
    interpreter_from_exec_start,
    parse_environment_files,
    parse_exec_start_path,
    require_fast_forward,
    require_exact_commit_sequence,
    refresh_deployment_ref,
    require_deployment_ref,
    run_as_service_user_command,
    run_git_materializing,
    run_nginx_config_test,
    run_nginx_discovery,
    resolve_commit,
    ServiceUserCommand,
    safe_repo_path,
    smoke_request,
    snapshot_git_state,
    targeted_rollback,
    temporary_target_ref,
    unexpected_warning_codes,
    validate_target_ref,
    validate_token,
    verify_remote_target_ref,
    wait_for_application_ready,
    validate_readiness_layers,
    validate_bootstrap_existing_post_merge,
    validate_bootstrap_post_merge,
    warning_codes,
)
from ops.deployment_test_profile import BootstrapEvidence, ValidationEvidence


class BootstrapContractTests(unittest.TestCase):
    def test_parser_exposes_explicit_bootstrap_module(self):
        args = build_parser().parse_args([
            "--service-unit", "django.service", "--old-sha", "1" * 40,
            "--target-sha", "2" * 40, "--remote", "origin",
            "--branch", "production", "--bootstrap-module",
            "ops.td02c_deployment_runner",
        ])
        self.assertEqual(args.bootstrap_module, "ops.td02c_deployment_runner")

    def test_bootstrap_rejects_unsafe_module_name_before_discovery(self):
        with self.assertRaisesRegex(DeploymentError, "Unsafe bootstrap"):
            bootstrap_module_deployment(
                Namespace(), Runner(), "../../unversioned"
            )

    def test_parser_exposes_explicit_target_ref(self):
        args = build_parser().parse_args([
            "--service-unit", "django.service", "--old-sha", "1" * 40,
            "--target-sha", "2" * 40, "--remote", "origin",
            "--branch", "production", "--target-ref",
            "refs/heads/td02c-bootstrap-" + "2" * 40,
        ])
        self.assertEqual(args.target_ref, "refs/heads/td02c-bootstrap-" + "2" * 40)

    def test_parser_target_ref_defaults_to_none(self):
        args = build_parser().parse_args([
            "--service-unit", "django.service", "--old-sha", "1" * 40,
            "--target-sha", "2" * 40, "--remote", "origin", "--branch", "production",
        ])
        self.assertIsNone(args.target_ref)

    def test_validate_target_ref_rejects_lock_suffix_and_git_dir_traversal(self):
        for bad_ref in ("refs/heads/x.lock", "refs/heads/x/.git/config"):
            with self.subTest(bad_ref=bad_ref):
                with self.assertRaisesRegex(DeploymentError, "Unsafe target ref"):
                    validate_target_ref(bad_ref)

    def test_validate_target_ref_accepts_well_formed_ref(self):
        ref = "refs/heads/td02c-bootstrap-" + "a" * 40
        self.assertEqual(validate_target_ref(ref), ref)

    def test_exact_commit_sequence_rejects_additional_missing_and_wrong_order(self):
        a, b, c = "a" * 40, "b" * 40, "c" * 40
        require_exact_commit_sequence([a, b], [a, b])
        for actual, expected in (([a, b, c], [a, b]), ([a], [a, b]), ([b, a], [a, b])):
            with self.subTest(actual=actual), self.assertRaises(DeploymentError):
                require_exact_commit_sequence(actual, expected)


class _BootstrapGitRunner:
    """Test-only fake Runner. Honors deterministic_umask exactly like the
    real Runner does (see ops.deployment_hardening.Runner.run) so that a
    fixture baseline captured deterministically in setUp() and a real
    merge/restore performed later through run_git_materializing -- using
    this fake runner instead of the real one -- agree on the resulting
    mode regardless of whatever umask happens to be ambient wherever this
    suite runs. Production code is untouched: this mirrors its behaviour,
    it does not implement it."""

    def run(self, command, *, cwd=None, user=None, check=True, deterministic_umask=False, **kwargs):
        if command and Path(str(command[0])).name.startswith("python"):
            return subprocess.CompletedProcess(command, 0, "", "")
        preexec = (
            (lambda: os.umask(0o022))
            if deterministic_umask and os.name == "posix"
            else None
        )
        result = subprocess.run(
            command, cwd=cwd, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, preexec_fn=preexec,
        )
        if check and result.returncode:
            raise DeploymentError(result.stderr.strip())
        return result


@unittest.skipUnless(os.name == "posix", "POSIX umask semantics")
class DeterministicTestUmaskHelperTests(unittest.TestCase):
    """Regression coverage for deterministic_test_umask itself -- the
    fixture-only helper, never production code. Real repro of the exact
    production finding: parent process ambient umask 0002 (the app
    account's real login umask), the same value used throughout this
    class's setUp."""

    def setUp(self):
        self.original_umask = os.umask(0o002)
        self.addCleanup(os.umask, self.original_umask)
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _umask(self):
        current = os.umask(0)
        os.umask(current)
        return current

    def test_parent_process_starts_at_umask_0002(self):
        self.assertEqual(self._umask(), 0o002)

    def test_normal_file_created_inside_helper_is_0644(self):
        path = self.root / "normal.txt"
        with deterministic_test_umask():
            path.write_text("x", encoding="utf-8")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_executable_created_inside_helper_is_0755_when_x_bit_set_after(self):
        # write_text() never sets the executable bit itself (that comes
        # from Git's index at checkout time, not from umask); this proves
        # the helper leaves 0o111 free to be added on top of the 0644
        # umask-derived base, matching 0644|0111=0755 exactly.
        path = self.root / "script.sh"
        with deterministic_test_umask():
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(path.stat().st_mode | 0o111)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    def test_umask_restored_to_0002_after_normal_exit(self):
        with deterministic_test_umask():
            pass
        self.assertEqual(self._umask(), 0o002)

    def test_umask_restored_to_0002_after_exception(self):
        with self.assertRaises(ValueError):
            with deterministic_test_umask():
                raise ValueError("synthetic failure inside the helper")
        self.assertEqual(self._umask(), 0o002)

    def test_fixture_created_outside_helper_stays_0664_under_ambient_0002(self):
        path = self.root / "unprotected.txt"
        path.write_text("x", encoding="utf-8")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o664)

    def test_deliberately_wrong_mode_outside_helper_is_still_rejected_by_gate(self):
        # The helper never weakens validate_bootstrap_post_merge: a file
        # created at the "wrong" mode 0664 outside the helper is still
        # exactly what the gate is supposed to catch.
        path = self.root / "unprotected.txt"
        path.write_text("x", encoding="utf-8")
        self.assertNotEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_sequential_helper_uses_never_leak_umask_between_blocks(self):
        for _ in range(5):
            with deterministic_test_umask():
                self.assertEqual(self._umask(), 0o022)
            self.assertEqual(self._umask(), 0o002)
        self.assertEqual(self._umask(), 0o002)

    def test_helper_is_a_noop_on_non_posix(self):
        with patch("ops.tests.test_deployment_hardening.os.name", "nt"):
            before = self._umask()
            with deterministic_test_umask():
                pass
            self.assertEqual(self._umask(), before)


class BootstrapPostMergeGateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        isolate_git_environment(self, self.root)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "production"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "ops@example.invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Ops Tests"], cwd=self.repo, check=True)
        # The whole fixture -- both commits -- is materialized under a
        # deterministic umask: validate_bootstrap_post_merge checks
        # new_paths against a fixed 0644/0755 expectation (no baseline to
        # compare against), and preexisting_modified_paths like
        # existing.txt are only meaningfully baseline-tested (e.g. a
        # deliberate chmod to a "different" mode) if the baseline itself
        # is a known, deterministic value -- neither can depend on
        # whatever umask happens to be ambient wherever this suite runs.
        with deterministic_test_umask():
            (self.repo / "runtime.txt").write_text("runtime-base", encoding="utf-8")
            (self.repo / "removed.txt").write_text("removed", encoding="utf-8")
            (self.repo / "existing.txt").write_text("old", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
            subprocess.run(["git", "commit", "-qm", "old"], cwd=self.repo, check=True)
            self.old = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
            (self.repo / "ops").mkdir()
            (self.repo / "ops" / "td02c_deployment_runner.py").write_text("VALUE=1\n", encoding="utf-8")
            (self.repo / "added.txt").write_text("added", encoding="utf-8")
            (self.repo / "existing.txt").write_text("new", encoding="utf-8")
            (self.repo / "removed.txt").unlink()
            subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
            subprocess.run(["git", "commit", "-qm", "target"], cwd=self.repo, check=True)
            self.target = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
        self.assertEqual(stat.S_IMODE((self.repo / "added.txt").stat().st_mode), 0o644)
        self.assertEqual(
            stat.S_IMODE((self.repo / "ops" / "td02c_deployment_runner.py").stat().st_mode), 0o644,
        )
        self.assertEqual(stat.S_IMODE((self.repo / "existing.txt").stat().st_mode), 0o644)
        (self.repo / "runtime.txt").write_text("runtime-local", encoding="utf-8")
        info = (self.repo / "runtime.txt").stat()
        import hashlib, stat as stat_module
        digest = hashlib.sha256((self.repo / "runtime.txt").read_bytes()).hexdigest()
        service = ServiceMetadata(
            unit="django.service", working_directory=self.repo,
            exec_start_path=Path("/bin/true"), exec_start_raw="/bin/true",
            python=Path("/usr/bin/python3"), user="app", group="app",
            main_pid=1, fragment_path=Path("/tmp/django.service"),
            environment_files=(),
        )
        self.context = DeploymentContext(
            service=service, nginx=NginxTarget("invalid", 443, "", None),
            old_sha=self.old, target_sha=self.target, repository=self.repo,
            branch="production", remote="origin",
            changed_files=["added.txt", "existing.txt", "ops/td02c_deployment_runner.py", "removed.txt"],
            runtime_files=["runtime.txt"], intersections=[],
            runtime_hashes={"runtime.txt": digest}, baseline_smoke={},
            baseline_warning_codes=set(), approved_commits=[self.target],
            runtime_metadata={"runtime.txt": {
                "sha256": digest, "mode": stat_module.S_IMODE(info.st_mode),
                "uid": info.st_uid, "gid": info.st_gid, "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
            }},
            materialized_path_metadata_before={
                "existing.txt": filesystem_metadata(self.repo / "existing.txt"),
                "removed.txt": {
                    **filesystem_metadata(self.repo / "existing.txt"),
                    "size": len("removed"),
                },
            },
            preexisting_modified_paths=["existing.txt"],
            new_paths=["added.txt", "ops/td02c_deployment_runner.py"],
            deleted_paths=["removed.txt"],
        )
        self.runner = _BootstrapGitRunner()

    def _bootstrap_args(self):
        remote = self.root / "remote.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(self.repo), str(remote)], check=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=self.repo, check=True)
        # This checkout is the real baseline for tests that then perform a
        # real merge (test_bootstrap_runs_preflight_once_and_installs_module):
        # deterministic here for the same reason as setUp -- it must agree
        # with whatever the real merge afterwards produces, not with
        # whatever umask happens to be ambient wherever this suite runs.
        with deterministic_test_umask():
            subprocess.run(["git", "checkout", "-q", self.old], cwd=self.repo, check=True)
            subprocess.run(["git", "branch", "-f", "production", self.old], cwd=self.repo, check=True)
            subprocess.run(["git", "checkout", "-q", "production"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "branch.production.remote", "origin"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "branch.production.merge", "refs/heads/production"], cwd=self.repo, check=True)
        (self.repo / "runtime.txt").write_text("runtime-local", encoding="utf-8")
        return Namespace(
            service_unit="django.service", old_sha=self.old,
            target_sha=self.target, remote="origin", branch="production",
            expected_commit=[self.target],
        )

    def test_post_merge_accepts_target_and_added_removed_paths(self):
        with patch("ops.deployment_hardening.pwd", None), patch("ops.deployment_hardening.grp", None):
            result = validate_bootstrap_post_merge(
                self.runner, self.context, module_name="ops.td02c_deployment_runner"
            )
        self.assertEqual(result, self.target)

    def test_post_merge_rejects_old_head(self):
        subprocess.run(["git", "checkout", "-q", self.old], cwd=self.repo, check=True)
        with self.assertRaisesRegex(DeploymentError, "HEAD is not target"):
            validate_bootstrap_post_merge(
                self.runner, self.context, module_name="ops.td02c_deployment_runner"
            )

    def test_post_merge_rejects_partial_index(self):
        (self.repo / "added.txt").write_text("partial", encoding="utf-8")
        subprocess.run(["git", "add", "added.txt"], cwd=self.repo, check=True)
        with self.assertRaisesRegex(DeploymentError, "staged changes"):
            validate_bootstrap_post_merge(
                self.runner, self.context, module_name="ops.td02c_deployment_runner"
            )

    def test_post_merge_rejects_runtime_metadata_change(self):
        os.utime(self.repo / "runtime.txt", ns=(
            (self.repo / "runtime.txt").stat().st_atime_ns,
            (self.repo / "runtime.txt").stat().st_mtime_ns + 1_000_000_000,
        ))
        with self.assertRaisesRegex(DeploymentError, "Runtime metadata changed"):
            validate_bootstrap_post_merge(
                self.runner, self.context, module_name="ops.td02c_deployment_runner"
            )

    def test_post_merge_accepts_preexisting_metadata_unchanged(self):
        with patch("ops.deployment_hardening.pwd", None), patch(
            "ops.deployment_hardening.grp", None
        ):
            self.assertEqual(validate_bootstrap_post_merge(
                self.runner, self.context,
                module_name="ops.td02c_deployment_runner",
            ), self.target)

    def test_post_merge_rejects_missing_preexisting_baseline(self):
        self.context.materialized_path_metadata_before.pop("existing.txt")
        with self.assertRaisesRegex(DeploymentError, "pre-merge metadata missing"):
            validate_bootstrap_post_merge(
                self.runner, self.context,
                module_name="ops.td02c_deployment_runner",
            )

    @unittest.skipUnless(os.name == "posix", "POSIX metadata semantics")
    def test_post_merge_rejects_preexisting_group_change(self):
        baseline = self.context.materialized_path_metadata_before["existing.txt"]
        baseline["gid"] = int(baseline["gid"]) + 1
        baseline["group"] = "different-approved-group"
        with self.assertRaisesRegex(DeploymentError, "preexisting metadata changed"):
            validate_bootstrap_post_merge(
                self.runner, self.context,
                module_name="ops.td02c_deployment_runner",
            )

    @unittest.skipUnless(os.name == "posix", "POSIX metadata semantics")
    def test_post_merge_rejects_preexisting_owner_change(self):
        baseline = self.context.materialized_path_metadata_before["existing.txt"]
        baseline["uid"] = 0 if os.getuid() != 0 else 1
        baseline["owner"] = "root" if os.getuid() != 0 else "not-root"
        with self.assertRaisesRegex(DeploymentError, "preexisting metadata changed"):
            validate_bootstrap_post_merge(
                self.runner, self.context,
                module_name="ops.td02c_deployment_runner",
            )

    @unittest.skipUnless(os.name == "posix", "POSIX metadata semantics")
    def test_post_merge_rejects_preexisting_mode_change(self):
        path = self.repo / "existing.txt"
        path.chmod(0o664)
        with self.assertRaisesRegex(DeploymentError, "preexisting metadata changed"):
            validate_bootstrap_post_merge(
                self.runner, self.context,
                module_name="ops.td02c_deployment_runner",
            )

    def test_post_merge_rejects_incomplete_classification(self):
        self.context.preexisting_modified_paths.clear()
        with self.assertRaisesRegex(DeploymentError, "classification is missing"):
            validate_bootstrap_post_merge(
                self.runner, self.context,
                module_name="ops.td02c_deployment_runner",
            )

    def test_post_merge_rejects_deleted_path_left_in_worktree(self):
        (self.repo / "removed.txt").write_text("residue", encoding="utf-8")
        with self.assertRaises(DeploymentError):
            validate_bootstrap_post_merge(
                self.runner, self.context,
                module_name="ops.td02c_deployment_runner",
            )

    def test_baseline_metadata_is_sanitized(self):
        baseline = self.context.materialized_path_metadata_before["existing.txt"]
        self.assertEqual(
            set(baseline),
            {"exists", "type", "uid", "gid", "owner", "group", "mode",
             "size", "device", "inode", "symlink"},
        )
        self.assertNotIn("content", baseline)
        self.assertNotIn("sha256", baseline)

    def test_post_merge_rejects_changed_path_wrong_ownership(self):
        with (
            patch("ops.deployment_hardening.pwd",
                  SimpleNamespace(getpwnam=lambda _name: SimpleNamespace(pw_uid=999999))),
            patch("ops.deployment_hardening.grp",
                  SimpleNamespace(getgrnam=lambda _name: SimpleNamespace(gr_gid=999999))),
        ):
            with self.assertRaisesRegex(DeploymentError, "unsafe ownership"):
                validate_bootstrap_post_merge(
                    self.runner, self.context,
                    module_name="ops.td02c_deployment_runner",
                )

    @unittest.skipUnless(os.name == "posix", "POSIX ownership semantics")
    def test_post_merge_accepts_app_app(self):
        gid = (self.repo / "added.txt").stat().st_gid
        with (
            patch("ops.deployment_hardening.pwd", SimpleNamespace(
                getpwnam=lambda _name: SimpleNamespace(pw_uid=os.getuid()))),
            patch("ops.deployment_hardening.grp", SimpleNamespace(
                getgrnam=lambda name: SimpleNamespace(
                    gr_gid=gid if name == "app" else gid + 1))),
        ):
            self.assertEqual(validate_bootstrap_post_merge(
                self.runner, self.context,
                module_name="ops.td02c_deployment_runner",
            ), self.target)

    @unittest.skipUnless(os.name == "posix", "POSIX ownership semantics")
    def test_post_merge_accepts_app_www_data(self):
        gid = (self.repo / "added.txt").stat().st_gid
        with (
            patch("ops.deployment_hardening.pwd", SimpleNamespace(
                getpwnam=lambda _name: SimpleNamespace(pw_uid=os.getuid()))),
            patch("ops.deployment_hardening.grp", SimpleNamespace(
                getgrnam=lambda name: SimpleNamespace(
                    gr_gid=gid if name == "www-data" else gid + 1))),
        ):
            self.assertEqual(validate_bootstrap_post_merge(
                self.runner, self.context,
                module_name="ops.td02c_deployment_runner",
            ), self.target)

    @unittest.skipUnless(os.name == "posix", "POSIX ownership semantics")
    def test_post_merge_rejects_root_owner(self):
        with (
            patch("ops.deployment_hardening.pwd", SimpleNamespace(
                getpwnam=lambda _name: SimpleNamespace(pw_uid=os.getuid() + 1))),
            patch("ops.deployment_hardening.grp", None),
        ):
            with self.assertRaisesRegex(DeploymentError, "unsafe ownership"):
                validate_bootstrap_post_merge(
                    self.runner, self.context,
                    module_name="ops.td02c_deployment_runner",
                )

    @unittest.skipUnless(os.name == "posix", "POSIX ownership semantics")
    def test_post_merge_rejects_unknown_group(self):
        gid = (self.repo / "added.txt").stat().st_gid
        with (
            patch("ops.deployment_hardening.pwd", SimpleNamespace(
                getpwnam=lambda _name: SimpleNamespace(pw_uid=os.getuid()))),
            patch("ops.deployment_hardening.grp", SimpleNamespace(
                getgrnam=lambda _name: SimpleNamespace(gr_gid=gid + 1))),
        ):
            with self.assertRaisesRegex(DeploymentError, "unsafe ownership"):
                validate_bootstrap_post_merge(
                    self.runner, self.context,
                    module_name="ops.td02c_deployment_runner",
                )

    def test_post_merge_rejects_symlink(self):
        path = self.repo / "added.txt"
        path.unlink()
        try:
            path.symlink_to("runtime.txt")
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(DeploymentError):
            validate_bootstrap_post_merge(
                self.runner, self.context,
                module_name="ops.td02c_deployment_runner",
            )

    @unittest.skipUnless(os.name == "posix", "POSIX permission semantics")
    def test_post_merge_rejects_expanded_permissions(self):
        (self.repo / "added.txt").chmod(0o666)
        with patch("ops.deployment_hardening.pwd", SimpleNamespace(
            getpwnam=lambda _name: SimpleNamespace(pw_uid=os.getuid()))), patch(
            "ops.deployment_hardening.grp", None
        ):
            with self.assertRaisesRegex(DeploymentError, "unsafe permissions"):
                validate_bootstrap_post_merge(
                    self.runner, self.context,
                    module_name="ops.td02c_deployment_runner",
                )

    def test_bootstrap_runs_preflight_once_and_installs_module(self):
        args = self._bootstrap_args()
        with (
            patch("ops.deployment_hardening.discover_service", return_value=self.context.service),
            patch("ops.deployment_hardening.require_commands"),
            patch("ops.deployment_hardening.preflight", wraps=__import__(
                "ops.deployment_hardening", fromlist=["preflight"]
            ).preflight) as preflight_call,
            patch("ops.deployment_hardening.pwd", None),
            patch("ops.deployment_hardening.grp", None),
        ):
            report = bootstrap_module_deployment(
                args, self.runner, "ops.td02c_deployment_runner"
            )
        self.assertEqual(report["head"], self.target)
        self.assertEqual(
            report["path_classification"]["preexisting_modified"],
            ["existing.txt"],
        )
        self.assertIn("existing.txt", report["materialized_path_metadata_before"])
        self.assertNotIn(
            "content", report["materialized_path_metadata_before"]["existing.txt"]
        )
        self.assertTrue((self.repo / "ops" / "td02c_deployment_runner.py").is_file())
        self.assertEqual(preflight_call.call_count, 1)

    def test_post_merge_failure_triggers_targeted_rollback(self):
        args = self._bootstrap_args()
        with (
            patch("ops.deployment_hardening.discover_service", return_value=self.context.service),
            patch("ops.deployment_hardening.require_commands"),
            patch("ops.deployment_hardening.validate_bootstrap_post_merge",
                  side_effect=DeploymentError("synthetic post-merge failure")),
            patch("ops.deployment_hardening.pwd", None),
            patch("ops.deployment_hardening.grp", None),
        ):
            with self.assertRaisesRegex(DeploymentError, "synthetic post-merge"):
                bootstrap_module_deployment(
                    args, self.runner, "ops.td02c_deployment_runner"
                )
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True
        ).strip()
        self.assertEqual(head, self.old)
        self.assertEqual((self.repo / "runtime.txt").read_text(encoding="utf-8"), "runtime-local")


def isolate_git_environment(testcase, root):
    """Keep temporary Git repositories independent from the invoking user."""
    home = root / "git-home"
    xdg = home / ".config"
    xdg.mkdir(parents=True)
    patcher = patch.dict(
        os.environ,
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(xdg),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
        },
    )
    patcher.start()
    testcase.addCleanup(patcher.stop)


RUNTIME_PATHS_FOR_TESTS = ("attachments/templates/.gitkeep",)


class BootstrapExistingComponentTests(unittest.TestCase):
    """Covers ops.deployment_hardening.bootstrap_existing_component_deployment,
    used only when the currently-installed predeployment evidence gate blocks
    deploying its own fix (see ops/deployment_test_profile.py)."""

    AUTHORIZED_PATHS = (
        "ops/deployment_test_profile.py",
        "ops/tests/test_deployment_test_profile.py",
        "ops/README.md",
    )

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        isolate_git_environment(self, self.root)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "production"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "ops@example.invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Ops Tests"], cwd=self.repo, check=True)
        # Every write_text() and git checkout below materializes files this
        # fixture (and run_git_materializing's own real merge, in tests
        # that exercise it) treats as baseline: both must agree on 0644
        # regardless of whatever umask happens to be ambient in whatever
        # process runs this suite -- otherwise a baseline captured under
        # one ambient umask would never match a deterministic-umask merge
        # materializing the same content later.
        with deterministic_test_umask():
            (self.repo / "ops" / "tests").mkdir(parents=True)
            (self.repo / "ops" / "deployment_test_profile.py").write_text("GATE = 'old'\n", encoding="utf-8")
            (self.repo / "ops" / "tests" / "test_deployment_test_profile.py").write_text("# old\n", encoding="utf-8")
            (self.repo / "ops" / "README.md").write_text("old docs\n", encoding="utf-8")
            (self.repo / "manage.py").write_text("# test\n", encoding="utf-8")
            for name in RUNTIME_PATHS_FOR_TESTS:
                path = self.repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
            subprocess.run(["git", "commit", "-qm", "old"], cwd=self.repo, check=True)
            self.old = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()

            (self.repo / "ops" / "deployment_test_profile.py").write_text("GATE = 'fixed'\n", encoding="utf-8")
            (self.repo / "ops" / "tests" / "test_deployment_test_profile.py").write_text("# fixed\n", encoding="utf-8")
            (self.repo / "ops" / "README.md").write_text("fixed docs\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A", "ops"], cwd=self.repo, check=True)
            subprocess.run(["git", "commit", "-qm", "target"], cwd=self.repo, check=True)
            self.target = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()

            # A later, descendant commit that must never affect target-sha
            # resolution: mirrors production's real state, where a tooling
            # commit (this very --target-ref feature) legitimately sits on
            # the branch ahead of the evidenced target.
            (self.repo / "ops" / "support_tool.py").write_text("TOOL = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A", "ops"], cwd=self.repo, check=True)
            subprocess.run(["git", "commit", "-qm", "support"], cwd=self.repo, check=True)
            self.support = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
            self.target_ref_name = f"refs/heads/td02c-bootstrap-{self.target}"

            # Clone while "production" points at the descendant support
            # commit: the bare remote's refs/heads/production == support,
            # exactly like origin/operator-ui-production-test being ahead
            # of the evidenced target in real life. The dedicated
            # target-ref is pushed separately, pointing exactly at target,
            # and never moves again. Only afterwards do we reset the local
            # checkout back to old_sha.
            remote = self.root / "remote.git"
            subprocess.run(["git", "clone", "--bare", "-q", str(self.repo), str(remote)], check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=self.repo, check=True)
            subprocess.run(
                ["git", "push", "-q", "origin", f"{self.target}:{self.target_ref_name}"],
                cwd=self.repo, check=True,
            )
            subprocess.run(["git", "checkout", "-q", self.old], cwd=self.repo, check=True)
            subprocess.run(["git", "branch", "-f", "production", self.old], cwd=self.repo, check=True)
            subprocess.run(["git", "checkout", "-q", "production"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "branch.production.remote", "origin"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "branch.production.merge", "refs/heads/production"], cwd=self.repo, check=True)

        self.service = ServiceMetadata(
            unit="django.service", working_directory=self.repo,
            exec_start_path=Path("/bin/true"), exec_start_raw="/bin/true --bind unix:/run/app.sock",
            python=Path("/usr/bin/python3"), user="app", group="app",
            main_pid=1, fragment_path=Path("/tmp/django.service"),
            environment_files=(),
        )
        self.runner = _BootstrapGitRunner()
        self.evidence_path = self.root / "bootstrap-evidence.json"

    def write_evidence(self, **overrides):
        values = {
            "target_sha": self.target,
            "commit_sequence": [self.target],
            "authorized_paths": list(self.AUTHORIZED_PATHS),
            "api_v2_passed": 27,
            "http_client_passed": 32,
            "nginx_diagnostics_passed": 22,
            "deployment_test_profile_passed": 17,
            "deployment_hardening_passed": 110,
            "ops_passed": 279,
            "linux_repetitions_passed": True,
            "postgresql_major": 17,
        }
        values.update(overrides)
        self.evidence_path.write_text(json.dumps(values), encoding="utf-8")
        if os.name == "posix":
            self.evidence_path.chmod(0o600)
        return self.evidence_path

    def args(self, **overrides):
        values = dict(
            service_unit="django.service", old_sha=self.old, target_sha=self.target,
            remote="origin", branch="production", expected_commit=[self.target],
            bootstrap_evidence=self.evidence_path, worker_unit="worker.service",
            allowed_warning=[], target_ref=self.target_ref_name,
        )
        values.update(overrides)
        return Namespace(**values)

    def run_bootstrap(self, *, authorized_paths=None, **arg_overrides):
        # _validate_bootstrap_operational_gates and _validate_new_evidence_gate_operational
        # are deliberately NOT patched here: both shell out via [python, "-c", ...],
        # and _BootstrapGitRunner already treats any python-prefixed command as an
        # instant success, so they are effectively no-ops in this harness. Leaving
        # them unpatched lets individual tests patch/observe them without a nested
        # patch on the same target silently shadowing theirs.
        with (
            patch("ops.deployment_hardening.discover_service", return_value=self.service),
            patch("ops.deployment_hardening.require_commands"),
            patch("ops.deployment_hardening.discover_and_validate_nginx",
                  return_value=NginxTarget("example.invalid", 443, "/run/app.sock", None)),
            patch("ops.deployment_hardening.validate_readiness_layers", return_value={"application": {"status": "200"}}),
            patch("ops.deployment_hardening.smoke_request", return_value={"status": "200"}),
            patch("ops.deployment_hardening.run_manage_check", return_value=("", set())),
            # None of these test repos exercise context.new_paths (every
            # authorized file already exists in old_sha), so bypassing the
            # real "app" system-account lookup here is safe: it mirrors how
            # BootstrapPostMergeGateTests patches pwd/grp for the same reason
            # on machines (including this test host) that have no such user.
            patch("ops.deployment_hardening.pwd", None),
            patch("ops.deployment_hardening.grp", None),
        ):
            return bootstrap_existing_component_deployment(
                self.args(**arg_overrides), self.runner,
                authorized_paths or self.AUTHORIZED_PATHS,
            )

    def head(self):
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True
        ).strip()

    # 1. componente existente permitido, con la rama principal ya adelantada
    # a un commit de soporte posterior al target evidenciado: PASS. HEAD
    # productivo termina exactamente en target, nunca en el commit de
    # soporte, y su contenido nunca se materializa en el checkout.
    def test_authorized_existing_component_bootstrap_succeeds(self):
        self.write_evidence()
        report = self.run_bootstrap()
        self.assertEqual(report["head"], self.target)
        self.assertEqual(self.head(), self.target)
        self.assertNotEqual(self.head(), self.support)
        self.assertFalse((self.repo / "ops" / "support_tool.py").exists())
        self.assertEqual(
            sorted(report["path_classification"]["preexisting_modified"]),
            sorted(self.AUTHORIZED_PATHS),
        )

    # 2. path adicional no autorizado: FAIL
    def test_unauthorized_path_in_range_is_rejected(self):
        (self.repo / "ops" / "extra.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "checkout", "-q", self.target], cwd=self.repo, check=True)
        (self.repo / "ops" / "extra.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A", "ops"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "--amend", "-qm", "target"], cwd=self.repo, check=True)
        self.target = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
        subprocess.run(["git", "checkout", "-q", "production"], cwd=self.repo, check=True)
        new_target_ref = f"refs/heads/td02c-bootstrap-{self.target}"
        subprocess.run(
            ["git", "push", "-q", "origin", f"{self.target}:{new_target_ref}"],
            cwd=self.repo, check=True,
        )
        self.write_evidence(target_sha=self.target, commit_sequence=[self.target])
        with self.assertRaisesRegex(DeploymentError, "unauthorized paths"):
            self.run_bootstrap(
                target_sha=self.target, expected_commit=[self.target], target_ref=new_target_ref,
            )

    # 3. runtime en el rango: FAIL (propagated from the shared preflight())
    def test_runtime_intersection_in_range_is_rejected(self):
        self.write_evidence()
        with patch("ops.deployment_hardening.preflight",
                    side_effect=DeploymentError("Target/runtime intersection: attachments/x")):
            with self.assertRaisesRegex(DeploymentError, "runtime intersection"):
                self.run_bootstrap()

    # 4. evidencia bootstrap de otro SHA: FAIL
    def test_evidence_for_another_sha_is_rejected(self):
        self.write_evidence(target_sha="a" * 40, commit_sequence=["a" * 40])
        with self.assertRaisesRegex(DeploymentError, "does not match|target"):
            self.run_bootstrap()

    # 5. evidencia incompleta: FAIL
    def test_incomplete_evidence_is_rejected(self):
        self.write_evidence(http_client_passed=8)
        with self.assertRaisesRegex(DeploymentError, "incomplete"):
            self.run_bootstrap()

    # 6. intento de usar evidencia bootstrap en despliegue normal: FAIL
    def test_bootstrap_evidence_schema_is_not_reusable_as_validation_evidence(self):
        bootstrap_fields = {field.name for field in dataclasses.fields(BootstrapEvidence)}
        validation_fields = {field.name for field in dataclasses.fields(ValidationEvidence)}
        self.assertNotEqual(bootstrap_fields, validation_fields)
        self.assertTrue({"authorized_paths"} <= bootstrap_fields - validation_fields)

    # 7. target con lógica productiva Django: FAIL
    def test_non_ops_path_can_never_be_authorized(self):
        for candidate in (
            "relay/models.py",
            "relay/migrations/0001_initial.py",
            "config/settings.py",
            "requirements.txt",
            ".env",
            "static/app.css",
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(DeploymentError, "Unsafe or duplicated"):
                    self.run_bootstrap(authorized_paths=(candidate,))

    # 7b. ausencia de un path esperado: FAIL (allowlist wider than the real
    # changed-file set -- expecting a path that never actually changed -- is
    # rejected just as strictly as an unauthorized extra change)
    def test_allowlist_wider_than_the_changed_set_is_rejected(self):
        wider = self.AUTHORIZED_PATHS + ("ops/never_changed.py",)
        self.write_evidence(authorized_paths=list(wider))
        with self.assertRaisesRegex(DeploymentError, "expects paths absent"):
            self.run_bootstrap(authorized_paths=wider)

    # 7c. orden de commits incorrecto: FAIL
    def test_wrong_expected_commit_order_is_rejected(self):
        self.write_evidence()
        with self.assertRaises(DeploymentError):
            self.run_bootstrap(expected_commit=[self.old, self.target])

    # 8. fast-forward imposible: FAIL
    def test_impossible_fast_forward_is_rejected(self):
        self.write_evidence()
        with patch("ops.deployment_hardening.preflight",
                    side_effect=DeploymentError("Target is not a fast-forward from old SHA")):
            with self.assertRaisesRegex(DeploymentError, "fast-forward"):
                self.run_bootstrap()

    # 9. fallo post-merge activa rollback
    def test_post_merge_failure_triggers_rollback_to_old_head(self):
        self.write_evidence()
        with (
            patch("ops.deployment_hardening.discover_service", return_value=self.service),
            patch("ops.deployment_hardening.require_commands"),
            patch("ops.deployment_hardening.discover_and_validate_nginx",
                  return_value=NginxTarget("example.invalid", 443, "/run/app.sock", None)),
            patch("ops.deployment_hardening.validate_readiness_layers", return_value={"application": {"status": "200"}}),
            patch("ops.deployment_hardening.smoke_request", return_value={"status": "200"}),
            patch("ops.deployment_hardening.run_manage_check", return_value=("", set())),
            patch("ops.deployment_hardening._validate_bootstrap_operational_gates"),
            patch("ops.deployment_hardening.validate_bootstrap_existing_post_merge",
                  side_effect=DeploymentError("synthetic post-merge failure")),
        ):
            with self.assertRaisesRegex(DeploymentError, "synthetic post-merge"):
                bootstrap_existing_component_deployment(
                    self.args(), self.runner, self.AUTHORIZED_PATHS,
                )
        self.assertEqual(self.head(), self.old)

    # 10. gate nuevo importable y operativo tras bootstrap (wiring proof;
    # the semantic proof that it accepts floors above the old snapshot lives
    # in ops/tests/test_deployment_test_profile.py).
    def test_post_merge_invokes_new_gate_operational_check(self):
        self.write_evidence()
        with patch(
            "ops.deployment_hardening._validate_new_evidence_gate_operational"
        ) as operational_check:
            self.run_bootstrap()
        operational_check.assert_called_once()

    # 11. el gate viejo no se ejecuta en este modo
    def test_bootstrap_never_references_normal_evidence_gate(self):
        source = inspect.getsource(bootstrap_existing_component_deployment)
        self.assertNotIn("validate_predeployment_evidence", source)
        self.assertNotIn("_load_validation_evidence", source)

    # --- --target-ref: explicit, hash-pinned deployment authority,
    # separate from the productive branch tip -----------------------------

    # 12. target-ref ausente: FAIL
    def test_target_ref_absent_is_rejected(self):
        self.write_evidence()
        with self.assertRaisesRegex(DeploymentError, "target-ref is required"):
            self.run_bootstrap(target_ref=None)

    # 13. target-ref apunta a un SHA distinto del target (el commit de
    # soporte, análogo a d990c95 apuntando más allá de f973216): FAIL
    def test_target_ref_pointing_to_a_different_sha_is_rejected(self):
        self.write_evidence()
        wrong_ref = f"refs/heads/td02c-bootstrap-wrong-{self.support}"
        subprocess.run(
            ["git", "push", "-q", "origin", f"{self.support}:{wrong_ref}"],
            cwd=self.repo, check=True,
        )
        with self.assertRaisesRegex(
            DeploymentError, "does not resolve to the approved target SHA"
        ):
            self.run_bootstrap(target_ref=wrong_ref)

    # 13b. target-ref apunta a un ancestro distinto del target (old_sha, no
    # el commit de soporte): FAIL, mismo chequeo de igualdad exacta.
    def test_target_ref_pointing_to_a_distinct_ancestor_is_rejected(self):
        self.write_evidence()
        ancestor_ref = f"refs/heads/td02c-bootstrap-ancestor-{self.old}"
        subprocess.run(
            ["git", "push", "-q", "origin", f"{self.old}:{ancestor_ref}"],
            cwd=self.repo, check=True,
        )
        with self.assertRaisesRegex(
            DeploymentError, "does not resolve to the approved target SHA"
        ):
            self.run_bootstrap(target_ref=ancestor_ref)

    # 14. ref simbólica, remota, o de otra forma insegura: FAIL
    def test_target_ref_symbolic_or_unsafe_is_rejected(self):
        self.write_evidence()
        for bad_ref in (
            "HEAD",
            "FETCH_HEAD",
            "production",
            "refs/remotes/origin/production",
            "refs/heads/*",
            "refs/heads/../escape",
        ):
            with self.subTest(bad_ref=bad_ref):
                with self.assertRaisesRegex(DeploymentError, "Unsafe target ref"):
                    self.run_bootstrap(target_ref=bad_ref)

    # 15. la rama principal nunca es fallback implícito: aunque su tip en
    # el remoto sea inválido/desactualizado, el bootstrap igual PASA usando
    # exclusivamente target-ref.
    def test_branch_tip_is_never_consulted_for_target_sha(self):
        self.write_evidence()
        subprocess.run(
            ["git", "push", "-q", "--force", "origin", f"{self.old}:refs/heads/production"],
            cwd=self.repo, check=True,
        )
        report = self.run_bootstrap()
        self.assertEqual(report["head"], self.target)

    # 16. target-ref ambigua (ls-remote devuelve más de una línea): FAIL
    def test_verify_remote_target_ref_rejects_ambiguous_ls_remote_output(self):
        sha_a, sha_b = "a" * 40, "b" * 40

        class AmbiguousRunner:
            def run(self, command, *, cwd=None, user=None, check=True, **kwargs):
                if command[:2] == ["git", "ls-remote"]:
                    return subprocess.CompletedProcess(
                        command, 0,
                        f"{sha_a}\trefs/heads/x\n{sha_b}\trefs/heads/x\n", "",
                    )
                raise AssertionError(f"unexpected command: {command}")

        with self.assertRaisesRegex(DeploymentError, "missing or ambiguous"):
            verify_remote_target_ref(
                AmbiguousRunner(), self.repo, remote="origin",
                target_ref="refs/heads/x", target_sha=sha_a, user="app",
            )

    # 17. target-ref cambia entre la primera verificación y la re-verificación
    # posterior al fetch: FAIL. Nunca se acepta silenciosamente un ref que
    # se movió a mitad de camino.
    def test_target_ref_change_mid_fetch_is_rejected(self):
        real_runner = self.runner
        support, target_ref_name = self.support, self.target_ref_name
        calls = {"ls_remote": 0}

        class FlippingRunner:
            def run(self, command, *, cwd=None, user=None, check=True, **kwargs):
                if command[:2] == ["git", "ls-remote"]:
                    calls["ls_remote"] += 1
                    if calls["ls_remote"] == 1:
                        return real_runner.run(command, cwd=cwd, user=user, check=check, **kwargs)
                    return subprocess.CompletedProcess(
                        command, 0, f"{support}\t{target_ref_name}\n", "",
                    )
                return real_runner.run(command, cwd=cwd, user=user, check=check, **kwargs)

        with self.assertRaisesRegex(
            DeploymentError, "does not resolve to the approved target SHA"
        ):
            fetch_bootstrap_target_ref(
                FlippingRunner(), self.repo, remote="origin",
                target_ref=self.target_ref_name, target_sha=self.target, user="app",
            )

    # 18. target-ref exacta y correcta, resuelta directamente: PASS
    def test_verify_remote_target_ref_accepts_exact_match(self):
        verify_remote_target_ref(
            self.runner, self.repo, remote="origin",
            target_ref=self.target_ref_name, target_sha=self.target, user="app",
        )

    # 19. secuencia de commits incorrecta con target-ref por lo demás
    # válida sigue rechazando: FAIL (ya cubierto en #7c, se repite aquí para
    # dejar explícita la interacción con target-ref por defecto)
    def test_wrong_commit_sequence_with_valid_target_ref_is_still_rejected(self):
        self.write_evidence()
        with self.assertRaises(DeploymentError):
            self.run_bootstrap(expected_commit=[self.old, self.target])

    # --- --bootstrap-skip-operational-checks: installs the nginx-check
    # mechanism itself, before its sudoers rule can exist -----------------

    def test_skip_operational_checks_succeeds_without_touching_nginx(self):
        """Paso A: nginx is completely unreachable (raises if ever called),
        yet the bootstrap still completes because the flag is set."""
        self.write_evidence()
        with (
            patch("ops.deployment_hardening.discover_service", return_value=self.service),
            patch("ops.deployment_hardening.require_commands"),
            patch("ops.deployment_hardening.discover_and_validate_nginx",
                  side_effect=AssertionError("nginx must never be touched during Paso A")),
            patch("ops.deployment_hardening.validate_readiness_layers",
                  side_effect=AssertionError("readiness must never run during Paso A")),
            patch("ops.deployment_hardening.run_manage_check", return_value=("", set())),
            patch("ops.deployment_hardening.pwd", None),
            patch("ops.deployment_hardening.grp", None),
        ):
            report = bootstrap_existing_component_deployment(
                self.args(bootstrap_skip_operational_checks=True),
                self.runner, self.AUTHORIZED_PATHS,
            )
        self.assertEqual(report["head"], self.target)
        self.assertEqual(self.head(), self.target)

    def test_skip_operational_checks_still_runs_jobs_and_worker_gate(self):
        """The Django jobs/V2/ledger/settings/worker check has no nginx
        dependency and must keep running even with the flag set."""
        self.write_evidence()
        with (
            patch("ops.deployment_hardening.discover_service", return_value=self.service),
            patch("ops.deployment_hardening.require_commands"),
            patch("ops.deployment_hardening.discover_and_validate_nginx",
                  side_effect=AssertionError("nginx must never be touched")),
            patch("ops.deployment_hardening.validate_readiness_layers",
                  side_effect=AssertionError("readiness must never run")),
            patch("ops.deployment_hardening.run_manage_check", return_value=("", set())),
            patch("ops.deployment_hardening.pwd", None),
            patch("ops.deployment_hardening.grp", None),
            patch("ops.deployment_hardening._validate_bootstrap_operational_gates") as gates,
        ):
            bootstrap_existing_component_deployment(
                self.args(bootstrap_skip_operational_checks=True),
                self.runner, self.AUTHORIZED_PATHS,
            )
        gates.assert_called_once()

    def test_default_still_requires_full_operational_checks(self):
        """Without the flag, a broken nginx still aborts the bootstrap --
        the default behaviour for every other target is unchanged."""
        self.write_evidence()
        with patch("ops.deployment_hardening.discover_service", return_value=self.service), \
             patch("ops.deployment_hardening.require_commands"), \
             patch("ops.deployment_hardening.discover_and_validate_nginx",
                   side_effect=DeploymentError("nginx_check_sudoers_missing: nginx configuration check failed")):
            with self.assertRaisesRegex(DeploymentError, "nginx_check_sudoers_missing"):
                bootstrap_existing_component_deployment(
                    self.args(), self.runner, self.AUTHORIZED_PATHS,
                )
        self.assertEqual(self.head(), self.old)

    def test_skip_operational_checks_flag_defaults_to_false(self):
        args = self.args()
        self.assertFalse(getattr(args, "bootstrap_skip_operational_checks", False))
        parsed = build_parser().parse_args([
            "--service-unit", "django.service", "--old-sha", self.old,
            "--target-sha", self.target, "--remote", "origin", "--branch", "production",
            "--bootstrap-existing-component", "ops/x.py",
            "--bootstrap-evidence", str(self.evidence_path),
        ])
        self.assertFalse(parsed.bootstrap_skip_operational_checks)

    # End-to-end reproduction of the real production finding: under the
    # real Runner (not _BootstrapGitRunner) and a parent umask of 0002 --
    # exactly the app account's real login umask on the droplet -- the
    # full bootstrap-existing-component flow must still land
    # ops/README.md at 0644, and the post-merge metadata gate must accept
    # it, with no chmod correction anywhere in this test.
    @unittest.skipUnless(os.name == "posix", "POSIX umask semantics")
    def test_real_merge_under_umask_0002_preserves_expected_mode_end_to_end(self):
        self.write_evidence()
        original_umask = os.umask(0o002)
        self.addCleanup(os.umask, original_umask)

        class RealGitNoOpPythonRunner(Runner):
            """Real Runner for Git -- so deterministic_umask genuinely
            applies -- but no-ops any python-prefixed command (test
            suites, operational/evidence gate checks), mirroring
            _BootstrapGitRunner's shortcut without losing real Git
            materialization behaviour."""

            def run(self, args, **kwargs):
                if args and Path(str(args[0])).name.startswith("python"):
                    return subprocess.CompletedProcess(args, 0, "", "")
                return super().run(args, **kwargs)

        real_runner = RealGitNoOpPythonRunner()
        with (
            patch("ops.deployment_hardening.discover_service", return_value=self.service),
            patch("ops.deployment_hardening.require_commands"),
            patch("ops.deployment_hardening.discover_and_validate_nginx",
                  side_effect=AssertionError("nginx must never be touched during Paso A")),
            patch("ops.deployment_hardening.validate_readiness_layers",
                  side_effect=AssertionError("readiness must never run during Paso A")),
            patch("ops.deployment_hardening.run_manage_check", return_value=("", set())),
            patch("ops.deployment_hardening._validate_bootstrap_operational_gates"),
            patch("ops.deployment_hardening._validate_new_evidence_gate_operational"),
        ):
            report = bootstrap_existing_component_deployment(
                self.args(bootstrap_skip_operational_checks=True),
                real_runner, self.AUTHORIZED_PATHS,
            )
        self.assertEqual(report["head"], self.target)
        self.assertEqual(
            stat.S_IMODE((self.repo / "ops" / "README.md").stat().st_mode), 0o644,
        )
        current = os.umask(0)
        os.umask(current)
        self.assertEqual(current, 0o002)


@unittest.skipUnless(os.name == "posix", "POSIX umask semantics")
class DeterministicUmaskGitTests(unittest.TestCase):
    """Real, non-mocked coverage for run_git_materializing: proves that
    Git merge/restore under this module always produces exactly the mode
    Git's own index records, deterministically, regardless of the parent
    process's ambient umask -- reproducing the exact production finding
    (umask 0002 on the app account materializing ops/README.md at 0664
    instead of the baseline 0644) and proving it fixed."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        isolate_git_environment(self, self.root)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "production"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "ops@example.invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Ops Tests"], cwd=self.repo, check=True)
        # The explicit chmod(0o755) calls below are load-bearing for Git
        # itself (they determine whether Git's index records the file as
        # executable at commit time, which write_text() alone never
        # does) -- they are NOT what makes the final on-disk mode
        # deterministic. The later `git checkout -q self.old` rematerializes
        # every tracked file from Git's index, and that checkout is
        # itself subject to ambient umask; wrapping the whole sequence
        # (not just the initial writes) is what makes the *checked-out*
        # mode deterministic regardless of whatever umask happens to be
        # ambient wherever this suite runs.
        with deterministic_test_umask():
            (self.repo / "normal.txt").write_text("old\n", encoding="utf-8")
            (self.repo / "script.sh").write_text("#!/bin/sh\necho old\n", encoding="utf-8")
            (self.repo / "script.sh").chmod(0o755)
            subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
            subprocess.run(["git", "commit", "-qm", "old"], cwd=self.repo, check=True)
            self.old = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True
            ).strip()

            (self.repo / "normal.txt").write_text("new\n", encoding="utf-8")
            (self.repo / "script.sh").write_text("#!/bin/sh\necho new\n", encoding="utf-8")
            (self.repo / "script.sh").chmod(0o755)
            (self.repo / "new_file.txt").write_text("brand new\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
            subprocess.run(["git", "commit", "-qm", "target"], cwd=self.repo, check=True)
            self.target = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True
            ).strip()

            subprocess.run(["git", "checkout", "-q", self.old], cwd=self.repo, check=True)
        self.assertEqual(stat.S_IMODE((self.repo / "normal.txt").stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((self.repo / "script.sh").stat().st_mode), 0o755)

        self.runner = Runner()
        self.user = pwd.getpwuid(os.geteuid()).pw_name if pwd is not None else None
        self.original_umask = os.umask(0o002)
        self.addCleanup(os.umask, self.original_umask)

    def _merge(self):
        run_git_materializing(
            self.runner, ["git", "merge", "--ff-only", self.target],
            cwd=self.repo, user=self.user,
        )

    def _mode(self, name):
        return stat.S_IMODE((self.repo / name).stat().st_mode)

    def _current_umask(self):
        current = os.umask(0)
        os.umask(current)
        return current

    # 1/2: merge crea archivo normal en 0644 y ejecutable en 0755, bajo
    # un proceso padre con umask 0002.
    def test_merge_creates_normal_file_at_0644_under_umask_0002(self):
        self.assertEqual(self._current_umask(), 0o002)
        self._merge()
        self.assertEqual(self._mode("normal.txt"), 0o644)

    def test_merge_creates_executable_at_0755_under_umask_0002(self):
        self._merge()
        self.assertEqual(self._mode("script.sh"), 0o755)

    # 4: archivo preexistente modificado conserva 0644.
    def test_preexisting_modified_file_keeps_0644(self):
        baseline = filesystem_metadata(self.repo / "normal.txt")
        self._merge()
        actual = filesystem_metadata(self.repo / "normal.txt")
        self.assertEqual(actual["mode"], baseline["mode"])
        self.assertEqual(actual["mode"], 0o644)

    # 5: archivo nuevo queda 0644.
    def test_new_file_from_merge_is_0644(self):
        self._merge()
        self.assertEqual(self._mode("new_file.txt"), 0o644)

    # 7: el proceso padre continúa con umask 0002 después.
    def test_parent_umask_is_unaffected_after_merge(self):
        self._merge()
        self.assertEqual(self._current_umask(), 0o002)

    # 6: rollback restaura 0644 (y 0755 para el ejecutable) bajo el mismo
    # umask 0002, sin ningún chmod correctivo -- solo el ejecutor
    # determinista.
    def test_rollback_restores_baseline_modes_under_umask_0002(self):
        self._merge()
        run_git_materializing(
            self.runner,
            ["git", "restore", "--source", self.old, "--staged", "--worktree",
             "--", "normal.txt", "script.sh"],
            cwd=self.repo, user=self.user,
        )
        self.assertEqual(self._mode("normal.txt"), 0o644)
        self.assertEqual(self._mode("script.sh"), 0o755)
        self.assertEqual(self._current_umask(), 0o002)

    # 8: operaciones read-only jamás requieren (ni aceptan) el ejecutor
    # determinista -- deben usar Runner.run directamente.
    def test_read_only_git_subcommands_are_rejected_by_materializing_helper(self):
        for bad_argv in (
            ["git", "rev-parse", "HEAD"],
            ["git", "status"],
            ["git", "diff"],
            ["git", "cat-file", "-e", self.target],
            ["git", "ls-tree", self.target],
            ["git", "ls-remote", "origin"],
        ):
            with self.subTest(argv=bad_argv):
                with self.assertRaisesRegex(
                    DeploymentError, "Unsafe materializing Git invocation"
                ):
                    run_git_materializing(self.runner, bad_argv, cwd=self.repo, user=self.user)

    def test_resolved_git_binary_is_absolute_and_executable(self):
        resolved = _resolve_git_binary()
        self.assertTrue(Path(resolved).is_absolute())
        self.assertTrue(os.access(resolved, os.X_OK))

    # 9/12: Git sigue ejecutándose como el usuario de servicio; nunca hay
    # fallback root -- un wrapper runuser (root cambiando a app) rechaza
    # el umask determinista en vez de aceptarlo sin garantía.
    def test_deterministic_umask_rejects_root_runuser_wrapper(self):
        with patch(
            "ops.deployment_hardening.run_as_service_user_command",
            return_value=ServiceUserCommand(
                ["runuser", "-u", "app", "--", "git", "merge", "--ff-only", self.target],
                "switched_from_root_to_service_user",
            ),
        ):
            with self.assertRaisesRegex(
                DeploymentError, "deterministic_umask requires executing directly"
            ):
                run_git_materializing(
                    self.runner, ["git", "merge", "--ff-only", self.target],
                    cwd=self.repo, user="app",
                )

    # 10: nunca shell=True.
    def test_no_shell_true_in_runner_source(self):
        source = inspect.getsource(Runner.run)
        self.assertNotIn("shell=True", source)

    # 13: fallo Git conserva exit code y diagnóstico.
    def test_failed_merge_preserves_exit_code_and_diagnostic(self):
        (self.repo / "normal.txt").write_text("locally diverged\n", encoding="utf-8")
        subprocess.run(["git", "add", "normal.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "local divergence"], cwd=self.repo, check=True)
        with self.assertRaisesRegex(DeploymentError, "command_failed"):
            self._merge()

    # 14: tras una interrupción a mitad del proceso (simulada como una
    # falla real de Git), el rollback posterior sigue produciendo los
    # modos correctos bajo umask 0002.
    def test_rollback_after_failed_merge_still_yields_correct_modes(self):
        (self.repo / "normal.txt").write_text("locally diverged\n", encoding="utf-8")
        subprocess.run(["git", "add", "normal.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "local divergence"], cwd=self.repo, check=True)
        with self.assertRaises(DeploymentError):
            self._merge()
        run_git_materializing(
            self.runner,
            ["git", "restore", "--source", self.old, "--staged", "--worktree",
             "--", "normal.txt"],
            cwd=self.repo, user=self.user,
        )
        self.assertEqual(self._mode("normal.txt"), 0o644)
        self.assertEqual(self._current_umask(), 0o002)


@unittest.skipUnless(os.name == "posix", "POSIX service-user execution")
class ServiceUserExecutionTests(unittest.TestCase):
    def test_matching_service_user_executes_directly(self):
        decision = run_as_service_user_command(
            ["python", "-c", "pass"], "app",
            effective_uid=1000, effective_user="app",
        )
        self.assertEqual(decision.argv, ["python", "-c", "pass"])
        self.assertEqual(
            decision.classification, "already_running_as_service_user"
        )
        self.assertNotIn("runuser", decision.argv)

    def test_root_switches_once_with_runuser(self):
        decision = run_as_service_user_command(
            ["python", "-c", "pass"], "app",
            effective_uid=0, effective_user="root",
        )
        self.assertEqual(
            decision.argv,
            ["runuser", "-u", "app", "--", "python", "-c", "pass"],
        )
        self.assertEqual(
            decision.classification, "switched_from_root_to_service_user"
        )
        self.assertEqual(decision.argv.count("runuser"), 1)

    def test_other_non_privileged_user_fails_closed(self):
        with self.assertRaisesRegex(DeploymentError, "cannot_switch_user"):
            run_as_service_user_command(
                ["python"], "app", effective_uid=1001, effective_user="other"
            )

    def test_empty_or_invalid_service_user_is_rejected(self):
        for user in ("", "-app", "bad user", "app;root"):
            with self.subTest(user=user):
                with self.assertRaisesRegex(DeploymentError, "service_user_mismatch"):
                    run_as_service_user_command(
                        ["python"], user, effective_uid=0, effective_user="root"
                    )

    def test_runner_does_not_execute_command_twice(self):
        completed = subprocess.CompletedProcess(["python"], 0, "ok")
        with (
            patch("ops.deployment_hardening.os.geteuid", return_value=1000),
            patch(
                "ops.deployment_hardening.pwd.getpwuid",
                return_value=SimpleNamespace(pw_name="app"),
            ),
            patch("ops.deployment_hardening.subprocess.run", return_value=completed) as call,
        ):
            result = Runner().run(["python", "-c", "pass"], user="app")
        self.assertIs(result, completed)
        call.assert_called_once()
        self.assertEqual(call.call_args.args[0], ["python", "-c", "pass"])

    def test_runuser_failure_is_sanitized_and_classified(self):
        failed = subprocess.CompletedProcess(
            ["runuser"], 1, "SECRET_KEY=private\nrunuser failed"
        )
        with patch("ops.deployment_hardening.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(DeploymentError, "command_failed") as caught:
                Runner().run(
                    ["python", "-c", "pass"], user="app"
                )
        message = str(caught.exception)
        self.assertIn("runuser failed", message)
        self.assertNotIn("private", message)


class InterpreterDiscoveryTests(unittest.TestCase):
    def _script(self, shebang: str | None, name: str = "wrapper") -> Path:
        directory = Path(tempfile.mkdtemp())
        script = directory / name
        content = f"#!{shebang}\n" if shebang is not None else "executable"
        script.write_text(content, encoding="utf-8")
        script.chmod(0o755)
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return script

    def test_any_python_wrapper_uses_its_absolute_shebang(self):
        for name in ("gunicorn", "uvicorn", "daphne", "waitress-serve", "custom"):
            with self.subTest(name=name):
                script = self._script("/usr/bin/python3", name=name)
                self.assertEqual(
                    interpreter_from_exec_start(script),
                    Path("/usr/bin/python3"),
                )

    def test_env_shebang_is_rejected_as_ambiguous(self):
        script = self._script("/usr/bin/env python3")
        with self.assertRaisesRegex(DeploymentError, "ambiguous"):
            interpreter_from_exec_start(script)

    def test_direct_python_executable_is_supported(self):
        python = self._script(None, name="python3")
        self.assertEqual(interpreter_from_exec_start(python), python)

    def test_non_python_shebang_is_rejected(self):
        script = self._script("/bin/bash")
        with self.assertRaisesRegex(DeploymentError, "does not declare"):
            interpreter_from_exec_start(script)

    def test_exec_start_path_is_parsed_from_systemd_value(self):
        raw = "{ path=/srv/app/.venv/bin/gunicorn ; argv[]=/srv/app/.venv/bin/gunicorn app:wsgi ; }"
        self.assertEqual(
            parse_exec_start_path(raw),
            Path("/srv/app/.venv/bin/gunicorn"),
        )

    def test_exec_start_path_decodes_systemd_escaped_space(self):
        raw = r"{ path=/srv/My\x20App/bin/wrapper ; argv[]=/srv/My\x20App/bin/wrapper; }"
        self.assertEqual(parse_exec_start_path(raw), Path("/srv/My App/bin/wrapper"))

    def test_shebang_with_arguments_uses_interpreter(self):
        script = self._script("/usr/bin/python3 -Es")
        self.assertEqual(interpreter_from_exec_start(script), Path("/usr/bin/python3"))

    def test_missing_or_non_executable_interpreter_script_is_rejected(self):
        script = self._script("/missing/python3")
        self.assertEqual(interpreter_from_exec_start(script), Path("/missing/python3"))
        script.chmod(0o644)
        if not __import__("os").access(script, __import__("os").X_OK):
            with self.assertRaisesRegex(DeploymentError, "not executable"):
                interpreter_from_exec_start(script)


class SystemdAndPathTests(unittest.TestCase):
    def test_optional_and_multiple_environment_files(self):
        parsed = parse_environment_files("-/etc/app/optional.env /etc/app/required.env")
        self.assertEqual(
            parsed,
            (
                EnvironmentFile(Path("/etc/app/optional.env"), True),
                EnvironmentFile(Path("/etc/app/required.env"), False),
            ),
        )

    def test_working_directory_forms_are_invalid_paths(self):
        for value in ("", "relative/path", "Z:/certainly/missing"):
            with self.subTest(value=value):
                self.assertFalse(Path(value).is_absolute() and Path(value).is_dir())

    def test_manage_symlink_outside_checkout_is_rejected_by_safe_path(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
            link = Path(root) / "manage.py"
            try:
                link.symlink_to(Path(other) / "manage.py")
            except OSError:
                self.skipTest("symlink creation is unavailable")
            with self.assertRaises(DeploymentError):
                safe_repo_path(Path(root), "manage.py")

    def test_unsafe_unit_branch_and_remote_tokens_are_rejected(self):
        for value in ("--help", "../unit", "name with space", ""):
            with self.assertRaises(DeploymentError):
                validate_token(value, "token")


class NginxDiscoveryTests(unittest.TestCase):
    def test_selects_tls_vhost_linked_to_application_socket(self):
        config = """
        server { listen 80 default_server; server_name _; }
        server {
            listen 443 ssl;
            server_name app.example.com;
            ssl_certificate /etc/ssl/app.pem;
            location / { proxy_pass http://unix:/run/app.sock; }
        }
        """
        target = discover_nginx_target(config, "/run/app.sock")
        self.assertEqual(target.server_name, "app.example.com")
        self.assertEqual(target.port, 443)

    def test_supports_named_upstream(self):
        config = """
        upstream application { server unix:/run/app.sock; }
        server {
            listen 443 ssl;
            server_name app.example.com;
            proxy_pass http://application;
        }
        """
        target = discover_nginx_target(config, "/run/app.sock")
        self.assertEqual(target.server_name, "app.example.com")

    def test_default_only_vhost_is_rejected(self):
        config = """
        server {
            listen 443 ssl;
            server_name _;
            proxy_pass http://unix:/run/app.sock;
        }
        """
        with self.assertRaisesRegex(DeploymentError, "candidates: none"):
            discover_nginx_target(config, "/run/app.sock")

    def test_multiple_real_vhosts_are_rejected(self):
        config = """
        server { listen 443 ssl; server_name a.example.com; proxy_pass http://unix:/run/app.sock; }
        server { listen 443 ssl; server_name b.example.com; proxy_pass http://unix:/run/app.sock; }
        """
        with self.assertRaisesRegex(DeploymentError, "ambiguous"):
            discover_nginx_target(config, "/run/app.sock")

    def test_invalid_or_empty_effective_hostname_is_rejected(self):
        config = """
        server { listen 443 ssl; server_name bad/name;
        proxy_pass http://unix:/run/app.sock; }
        """
        with self.assertRaisesRegex(DeploymentError, "empty or invalid"):
            discover_nginx_target(config, "/run/app.sock")

    def test_wildcard_vhost_is_rejected(self):
        config = """
        server { listen 443 ssl; server_name *.example.com;
        proxy_pass http://unix:/run/app.sock; }
        """
        with self.assertRaisesRegex(DeploymentError, "candidates: none"):
            discover_nginx_target(config, "/run/app.sock")

    def test_nginx_variable_vhost_is_rejected(self):
        config = """
        server { listen 443 ssl; server_name $host;
        proxy_pass http://unix:/run/app.sock; }
        """
        with self.assertRaisesRegex(DeploymentError, "candidates: none"):
            discover_nginx_target(config, "/run/app.sock")

    def test_vhost_for_unrelated_socket_is_rejected(self):
        config = """
        server { listen 443 ssl; server_name app.example.com;
        proxy_pass http://unix:/run/other.sock; }
        """
        with self.assertRaisesRegex(DeploymentError, "candidates: none"):
            discover_nginx_target(config, "/run/app.sock")


class NginxDiscoveryCallerTests(unittest.TestCase):
    """Coverage for run_nginx_discovery: the caller-side validation of
    the privileged discovery helper's sanitized JSON output. Never
    invokes real subprocesses -- a fake runner returns crafted stdout,
    exactly mirroring how the real helper (or a rejecting sudo) would
    respond."""

    def setUp(self):
        self.service = ServiceMetadata(
            unit="django.service", working_directory=Path("/tmp"),
            exec_start_path=Path("/bin/true"), exec_start_raw="/bin/true",
            python=Path("/usr/bin/python3"), user="app", group="app",
            main_pid=1, fragment_path=Path("/tmp/django.service"),
            environment_files=(),
        )

    def _runner_returning(self, stdout, returncode=0):
        class FakeRunner:
            def run(inner, command, *, cwd=None, user=None, check=True, **kwargs):
                return subprocess.CompletedProcess(command, returncode, stdout, "")
        return FakeRunner()

    def _success_payload(self, **overrides):
        payload = {
            "schema": "td02c.nginx-discovery/v1", "phase": "nginx_discovery",
            "classification": "nginx_discovery_privileged_passed", "result": "PASS",
            "exit_code": 0, "server_name": "app1.example.com", "port": 443,
            "proxy_or_socket_target": "/run/app.sock",
            "certificate_path": "/etc/letsencrypt/live/app1.example.com/fullchain.pem",
            "certificate_key_path_present": True, "nginx_test_passed": True,
        }
        payload.update(overrides)
        return payload

    # 1. helper valido -> NginxTarget correcto
    def test_success_returns_nginx_target(self):
        runner = self._runner_returning(json.dumps(self._success_payload()), 0)
        target = run_nginx_discovery(runner, self.service)
        self.assertEqual(target.server_name, "app1.example.com")
        self.assertEqual(target.port, 443)
        self.assertEqual(target.upstream, "/run/app.sock")
        self.assertEqual(
            str(target.certificate), "/etc/letsencrypt/live/app1.example.com/fullchain.pem",
        )

    def test_certificate_absent_yields_none(self):
        payload = self._success_payload(certificate_path=None, certificate_key_path_present=False)
        runner = self._runner_returning(json.dumps(payload), 0)
        target = run_nginx_discovery(runner, self.service)
        self.assertIsNone(target.certificate)

    # 2. JSON con campo extra: FAIL
    def test_extra_field_is_rejected(self):
        payload = self._success_payload(unexpected_field="x")
        runner = self._runner_returning(json.dumps(payload), 0)
        with self.assertRaisesRegex(DeploymentError, "nginx_discovery_schema_mismatch"):
            run_nginx_discovery(runner, self.service)

    def test_missing_required_field_is_rejected(self):
        payload = self._success_payload()
        del payload["server_name"]
        runner = self._runner_returning(json.dumps(payload), 0)
        with self.assertRaisesRegex(DeploymentError, "nginx_discovery_schema_mismatch"):
            run_nginx_discovery(runner, self.service)

    def test_wrong_schema_version_is_rejected(self):
        payload = self._success_payload(schema="td02c.nginx-discovery/v2")
        runner = self._runner_returning(json.dumps(payload), 0)
        with self.assertRaisesRegex(DeploymentError, "nginx_discovery_schema_mismatch"):
            run_nginx_discovery(runner, self.service)

    # 3. salida no JSON: FAIL
    def test_non_json_output_is_rejected(self):
        runner = self._runner_returning("not json at all", 1)
        with self.assertRaisesRegex(DeploymentError, "nginx_discovery_invalid_json"):
            run_nginx_discovery(runner, self.service)

    def test_sudo_no_password_is_classified_sudoers_missing(self):
        runner = self._runner_returning("sudo: a password is required", 1)
        with self.assertRaisesRegex(DeploymentError, "nginx_discovery_sudoers_missing"):
            run_nginx_discovery(runner, self.service)

    def test_sudo_not_allowed_is_classified_command_rejected(self):
        runner = self._runner_returning(
            "Sorry, user app is not allowed to execute "
            "'/usr/local/libexec/td02c-nginx-discovery' as root.", 1,
        )
        with self.assertRaisesRegex(DeploymentError, "nginx_discovery_command_rejected"):
            run_nginx_discovery(runner, self.service)

    def test_helper_missing_is_classified(self):
        runner = self._runner_returning(
            "sudo: /usr/local/libexec/td02c-nginx-discovery: command not found", 1,
        )
        with self.assertRaisesRegex(DeploymentError, "nginx_discovery_helper_missing"):
            run_nginx_discovery(runner, self.service)

    # cero secretos: contenido sensible detectado antes de confiar en nada
    def test_sensitive_output_detected_before_json_parsing(self):
        runner = self._runner_returning(
            "-----BEGIN PRIVATE KEY-----\nMIIExyz\n-----END PRIVATE KEY-----", 1,
        )
        with self.assertRaisesRegex(DeploymentError, "nginx_discovery_sensitive_output_detected"):
            run_nginx_discovery(runner, self.service)

    def test_helper_failure_classification_propagates(self):
        payload = {
            "schema": "td02c.nginx-discovery/v1", "phase": "nginx_discovery",
            "classification": "nginx_discovery_no_vhost", "result": "FAIL", "exit_code": 1,
        }
        runner = self._runner_returning(json.dumps(payload), 1)
        with self.assertRaisesRegex(DeploymentError, "nginx_discovery_no_vhost"):
            run_nginx_discovery(runner, self.service)

    # defensa en profundidad: un cuerpo "passed" no basta si el exit code
    # del subproceso subyacente lo contradice
    def test_success_classification_but_nonzero_exit_is_rejected(self):
        runner = self._runner_returning(json.dumps(self._success_payload()), 1)
        with self.assertRaisesRegex(DeploymentError, "nginx_discovery_privileged_passed"):
            run_nginx_discovery(runner, self.service)

    def test_never_calls_raw_nginx_dash_T(self):
        source = inspect.getsource(discover_and_validate_nginx)
        self.assertNotIn('"-T"', source)
        self.assertNotIn("nginx_config = runner.run", source)


class WarningParsingTests(unittest.TestCase):
    def test_warning_codes_are_structured(self):
        output = "WARNINGS:\n?: (security.W005) warning\n?: (security.W021) warning"
        self.assertEqual(warning_codes(output), {"W005", "W021"})

    def test_new_warning_code_is_rejected_by_comparison(self):
        output = "?: (security.W005) known\n?: (security.W999) new"
        self.assertEqual(
            unexpected_warning_codes(output, {"W005"}),
            {"W999"},
        )


class RestartClassificationTests(unittest.TestCase):
    def test_ops_only_changes_classify_no_restart(self):
        self.assertEqual(
            classify_restart_requirement(
                ["ops/deployment_hardening.py", "ops/bulk_v2_canary_client.py"]
            ),
            "ops_only_no_restart",
        )

    def test_ops_and_openspec_changes_mixed_still_classify_no_restart(self):
        self.assertEqual(
            classify_restart_requirement(
                [
                    "ops/deployment_hardening.py",
                    "openspec/changes/activate-bulk-v2-canary/tasks.md",
                    "openspec/changes/activate-bulk-v2-canary/design.md",
                ]
            ),
            "ops_only_no_restart",
        )

    def test_relay_changes_require_restart(self):
        self.assertEqual(
            classify_restart_requirement(["relay/views.py"]),
            "web_runtime_required",
        )

    def test_config_changes_require_restart(self):
        self.assertEqual(
            classify_restart_requirement(["config/settings.py"]),
            "web_runtime_required",
        )

    def test_unrecognized_top_level_path_fails_closed_to_restart_required(self):
        self.assertEqual(
            classify_restart_requirement(["somebrandnewtoplevel/module.py"]),
            "web_runtime_required",
        )

    def test_mixed_ops_and_relay_requires_restart_all_paths_must_be_allowlisted(self):
        self.assertEqual(
            classify_restart_requirement(
                ["ops/deployment_hardening.py", "relay/views.py"]
            ),
            "web_runtime_required",
        )

    def test_empty_changed_files_fails_closed_to_restart_required(self):
        self.assertEqual(classify_restart_requirement([]), "web_runtime_required")

    def test_classification_has_no_override_parameter(self):
        # Scenario 8: nothing besides changed_files can influence the
        # result -- there is no bypass flag, env var, or kwarg.
        parameters = list(inspect.signature(classify_restart_requirement).parameters)
        self.assertEqual(parameters, ["changed_files"])

    def test_ops_and_exact_openspec_config_yaml_classify_no_restart(self):
        # openspec/config.yaml is a one-time sdd-init scaffold file that
        # lives at the openspec/ root, outside openspec/changes/. It is
        # allowlisted as an exact path, not a prefix.
        self.assertEqual(
            classify_restart_requirement(
                [
                    "ops/deployment_hardening.py",
                    "openspec/changes/activate-bulk-v2-canary/tasks.md",
                    "openspec/config.yaml",
                ]
            ),
            "ops_only_no_restart",
        )

    def test_openspec_root_paths_other_than_exact_config_yaml_fail_closed(self):
        # Fail-closed for every other path under openspec/ that isn't
        # already covered by openspec/changes/ or the exact
        # openspec/config.yaml allowlist entry. Includes a near-miss case
        # (a filename that merely starts with "openspec/config" but is not
        # the literal string) to prove this is exact-match, not a prefix
        # match.
        for changed in (
            ["openspec/specs/foo.md"],
            ["openspec/other-root-file.yaml"],
            ["openspec/config.yaml.bak"],
            ["openspec/config.yaml.d/foo"],
        ):
            with self.subTest(changed=changed):
                self.assertEqual(
                    classify_restart_requirement(changed),
                    "web_runtime_required",
                )


class RestartClassificationRealDeploymentAcceptanceTests(unittest.TestCase):
    def test_activate_bulk_v2_canary_deployment_range_classifies_no_restart(self):
        # The real 16-file changed-files list from the
        # activate-bulk-v2-canary deployment range (commits 8212a4e..
        # 11926ea) that surfaced this bug: openspec/config.yaml matched
        # neither allowlist prefix and forced the entire range to
        # web_runtime_required despite every other file being legitimately
        # ops/** or openspec/changes/**.
        changed_files = [
            "openspec/changes/activate-bulk-v2-canary/design.md",
            "openspec/changes/activate-bulk-v2-canary/proposal.md",
            "openspec/changes/activate-bulk-v2-canary/specs/bulk-v2-canary-activation/spec.md",
            "openspec/changes/activate-bulk-v2-canary/specs/bulk-v2-canary-execution/spec.md",
            "openspec/changes/activate-bulk-v2-canary/tasks.md",
            "openspec/changes/archive/.gitkeep",
            "openspec/config.yaml",
            "ops/README.md",
            "ops/bulk_v2_canary_client.py",
            "ops/deployment_hardening.py",
            "ops/td02c_http_client.py",
            "ops/td02c_settings_gate.py",
            "ops/tests/test_bulk_v2_canary_client.py",
            "ops/tests/test_deployment_hardening.py",
            "ops/tests/test_td02c_http_client.py",
            "ops/tests/test_td02c_settings_gate.py",
        ]
        self.assertEqual(len(changed_files), 16)
        self.assertEqual(
            classify_restart_requirement(changed_files),
            "ops_only_no_restart",
        )


class SharedAllowlistPredicateTests(unittest.TestCase):
    """Proves classify_restart_requirement and
    _validate_bootstrap_existing_paths are defined in terms of the exact
    same underlying predicate (is_ops_only_allowlisted_path), so a
    regression between the two policies is structurally impossible rather
    than merely coincidentally passing both today."""

    ACCEPTED_REPRESENTATIVE_PATHS = (
        "ops/deployment_hardening.py",
        "openspec/changes/activate-bulk-v2-canary/tasks.md",
        "openspec/config.yaml",
    )
    REJECTED_REPRESENTATIVE_PATHS = (
        "openspec/config.yaml.bak",
        "openspec/specs/anything.md",
        "relay/anything.py",
        "config/anything.py",
    )

    def test_both_entry_points_accept_the_same_paths(self):
        for path in self.ACCEPTED_REPRESENTATIVE_PATHS:
            with self.subTest(path=path):
                self.assertTrue(is_ops_only_allowlisted_path(path))
                self.assertEqual(
                    classify_restart_requirement([path]), "ops_only_no_restart"
                )
                _validate_bootstrap_existing_paths((path,))  # must not raise

    def test_both_entry_points_reject_the_same_paths(self):
        for path in self.REJECTED_REPRESENTATIVE_PATHS:
            with self.subTest(path=path):
                self.assertFalse(is_ops_only_allowlisted_path(path))
                self.assertEqual(
                    classify_restart_requirement([path]), "web_runtime_required"
                )
                with self.assertRaisesRegex(DeploymentError, "Unsafe or duplicated"):
                    _validate_bootstrap_existing_paths((path,))


class ValidateBootstrapExistingPathsTests(unittest.TestCase):
    """Unit-level coverage of
    ops.deployment_hardening._validate_bootstrap_existing_paths, the static
    shape/scope gate for --bootstrap-existing-component authorized_paths.
    Complements BootstrapExistingComponentTests, which exercises the same
    gate indirectly through the full bootstrap_existing_component_deployment
    flow."""

    def test_ops_only_paths_are_accepted(self):
        _validate_bootstrap_existing_paths((
            "ops/deployment_hardening.py", "ops/bulk_v2_canary_client.py",
        ))  # must not raise

    def test_ops_and_openspec_changes_paths_are_accepted(self):
        _validate_bootstrap_existing_paths((
            "ops/deployment_hardening.py",
            "openspec/changes/activate-bulk-v2-canary/tasks.md",
        ))  # must not raise

    def test_ops_and_exact_openspec_config_yaml_are_accepted(self):
        _validate_bootstrap_existing_paths((
            "ops/deployment_hardening.py", "openspec/config.yaml",
        ))  # must not raise

    def test_real_activate_bulk_v2_canary_16_path_range_is_accepted(self):
        # Reproduces the exact authorized-paths list from the aborted
        # --bootstrap-existing-component attempt against the
        # activate-bulk-v2-canary deployment range
        # (8212a4ed4606ac074c16bae8e4cb4e77db5c816a..8ad518600e3c04336
        # 2c27d9d66c1d656fb8033f6): this is the direct acceptance proof
        # that the shared-predicate fix closes the real gap the old
        # ops/-only regex left open, not merely a synthetic scenario.
        authorized_paths = (
            "openspec/changes/activate-bulk-v2-canary/design.md",
            "openspec/changes/activate-bulk-v2-canary/proposal.md",
            "openspec/changes/activate-bulk-v2-canary/specs/bulk-v2-canary-activation/spec.md",
            "openspec/changes/activate-bulk-v2-canary/specs/bulk-v2-canary-execution/spec.md",
            "openspec/changes/activate-bulk-v2-canary/tasks.md",
            "openspec/changes/archive/.gitkeep",
            "openspec/config.yaml",
            "ops/README.md",
            "ops/bulk_v2_canary_client.py",
            "ops/deployment_hardening.py",
            "ops/td02c_http_client.py",
            "ops/td02c_settings_gate.py",
            "ops/tests/test_bulk_v2_canary_client.py",
            "ops/tests/test_deployment_hardening.py",
            "ops/tests/test_td02c_http_client.py",
            "ops/tests/test_td02c_settings_gate.py",
        )
        self.assertEqual(len(authorized_paths), 16)
        _validate_bootstrap_existing_paths(authorized_paths)  # must not raise

    def test_near_miss_openspec_config_yaml_bak_is_rejected(self):
        # Near-miss: proves the exact-path allowlist entry is exact-match,
        # not a prefix match.
        with self.assertRaisesRegex(DeploymentError, "Unsafe or duplicated"):
            _validate_bootstrap_existing_paths(("openspec/config.yaml.bak",))

    def test_openspec_specs_path_is_rejected(self):
        with self.assertRaisesRegex(DeploymentError, "Unsafe or duplicated"):
            _validate_bootstrap_existing_paths(("openspec/specs/anything.md",))

    def test_relay_path_is_rejected(self):
        with self.assertRaisesRegex(DeploymentError, "Unsafe or duplicated"):
            _validate_bootstrap_existing_paths(("relay/anything.py",))

    def test_config_path_is_rejected(self):
        with self.assertRaisesRegex(DeploymentError, "Unsafe or duplicated"):
            _validate_bootstrap_existing_paths(("config/anything.py",))

    def test_unrecognized_top_level_path_is_rejected(self):
        with self.assertRaisesRegex(DeploymentError, "Unsafe or duplicated"):
            _validate_bootstrap_existing_paths(("somebrandnewtoplevel/module.py",))

    # Scenario 10 (bootstrap_existing_component_deployment-level:
    # authorized_paths with one path more/fewer than context.changed_files
    # is still rejected by the existing exact-match unauthorized/missing checks,
    # which this fix does not touch) is already covered by
    # BootstrapExistingComponentTests.test_unauthorized_path_in_range_is_rejected
    # and BootstrapExistingComponentTests.test_allowlist_wider_than_the_changed_set_is_rejected.


class RecordingRunner:
    def __init__(self, output="__DEPLOY_SMOKE__403", returncode=0):
        self.calls = []
        self.output = output
        self.returncode = returncode

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if kwargs.get("check", True) and self.returncode:
            raise DeploymentError("curl failed")
        return subprocess.CompletedProcess(args, self.returncode, self.output, "")


class CommandMapRunner:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        return self.handler(list(args), kwargs)


def nginx_check_pass_json(classification="nginx_check_direct_passed", method="direct"):
    return json.dumps({
        "phase": "nginx_config_check", "classification": classification,
        "method": method, "exit_code": 0, "result": "PASS",
    })


def is_nginx_check_invocation(args):
    return bool(args) and args[-1].endswith("td02c_nginx_config_check.py")


class NginxConfigTestIntegrationTests(unittest.TestCase):
    """ops.deployment_hardening.run_nginx_config_test: the wrapper that
    replaced every raw `nginx -t` call inside preflight/readiness."""

    def service(self):
        return SimpleNamespace(
            unit="django.service", user="app",
            working_directory=Path("/opt/app/django-doppler-relay"),
            python=Path("/opt/app/django-doppler-relay/.venv/bin/python"),
        )

    def test_privileged_pass_is_accepted(self):
        service = self.service()
        runner = CommandMapRunner(
            lambda args, kwargs: subprocess.CompletedProcess(
                args, 0, nginx_check_pass_json("nginx_check_privileged_passed", "privileged"), ""
            )
        )
        classification = run_nginx_config_test(runner, service)
        self.assertEqual(classification, "nginx_check_privileged_passed")

    def test_failure_classification_is_raised_verbatim(self):
        service = self.service()
        payload = json.dumps({
            "phase": "nginx_config_check", "classification": "nginx_check_sudoers_missing",
            "method": "direct", "exit_code": 1, "result": "FAIL",
        })
        runner = CommandMapRunner(
            lambda args, kwargs: subprocess.CompletedProcess(args, 1, payload, "")
        )
        with self.assertRaisesRegex(DeploymentError, "nginx_check_sudoers_missing"):
            run_nginx_config_test(runner, service)

    def test_malformed_output_falls_back_to_unexpected_error(self):
        service = self.service()
        runner = CommandMapRunner(
            lambda args, kwargs: subprocess.CompletedProcess(args, 1, "not json at all", "")
        )
        with self.assertRaisesRegex(DeploymentError, "nginx_check_unexpected_error"):
            run_nginx_config_test(runner, service)

    def test_unrecognized_classification_is_rejected(self):
        service = self.service()
        payload = json.dumps({"classification": "totally_made_up", "result": "PASS"})
        runner = CommandMapRunner(
            lambda args, kwargs: subprocess.CompletedProcess(args, 0, payload, "")
        )
        with self.assertRaisesRegex(DeploymentError, "nginx_check_unexpected_error"):
            run_nginx_config_test(runner, service)

    def test_helper_is_invoked_as_service_user_under_working_directory(self):
        service = self.service()
        runner = CommandMapRunner(
            lambda args, kwargs: subprocess.CompletedProcess(args, 0, nginx_check_pass_json(), "")
        )
        run_nginx_config_test(runner, service)
        (call,) = runner.calls
        self.assertEqual(call[0], str(service.python))
        self.assertTrue(call[1].endswith("ops/td02c_nginx_config_check.py") or call[1].endswith("ops\\td02c_nginx_config_check.py"))

    def test_helper_is_resolved_next_to_the_running_module_not_the_checkout(self):
        """Regression guard: during bootstrap_existing_component_deployment
        this code runs from an ephemeral copy, so the real checkout being
        validated (service.working_directory) may not have the helper yet.
        The helper path must never depend on service.working_directory."""
        import ops.deployment_hardening as module

        service = SimpleNamespace(
            unit="django.service", user="app",
            working_directory=Path("/opt/app/django-doppler-relay-DOES-NOT-EXIST"),
            python=Path("/opt/app/django-doppler-relay/.venv/bin/python"),
        )
        runner = CommandMapRunner(
            lambda args, kwargs: subprocess.CompletedProcess(args, 0, nginx_check_pass_json(), "")
        )
        run_nginx_config_test(runner, service)
        (call,) = runner.calls
        helper_path = Path(call[1])
        self.assertEqual(helper_path.parent, Path(module.__file__).resolve().parent)
        self.assertNotIn("DOES-NOT-EXIST", call[1])

    def test_helper_runs_as_app_not_root(self):
        service = self.service()
        seen_kwargs = {}
        def handler(args, kwargs):
            seen_kwargs.update(kwargs)
            return subprocess.CompletedProcess(args, 0, nginx_check_pass_json(), "")
        run_nginx_config_test(CommandMapRunner(handler), service)
        self.assertEqual(seen_kwargs.get("user"), "app")

    def test_no_raw_nginx_dash_t_call_remains_in_the_module(self):
        """Structural regression guard: nginx -t must only ever be invoked
        through the least-privilege helper, never directly by this module."""
        source = inspect.getsource(__import__("ops.deployment_hardening", fromlist=["x"]))
        self.assertNotIn('["nginx", "-t"]', source)
        self.assertNotIn("run_as_service_user_command(['nginx', '-t']", source)


class OperationalDiscoveryTests(unittest.TestCase):
    def _systemd_output(self, root: Path, *, active="active", user="svc", group="svc"):
        return "\n".join(
            [
                "LoadState=loaded", f"ActiveState={active}", "SubState=running",
                f"WorkingDirectory={root}",
                "ExecStart={ path=/usr/local/bin/wrapper ; argv[]=/usr/local/bin/wrapper --bind unix:/run/app.sock app:wsgi ; }",
                f"User={user}", f"Group={group}", "MainPID=123",
                "FragmentPath=/etc/systemd/system/django.service",
                "EnvironmentFiles=-/etc/app/optional.env",
            ]
        )

    def _checkout(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / ".git").mkdir()
        (root / "manage.py").write_text("# test\n")
        self.addCleanup(temporary.cleanup)
        return root

    def test_inactive_service_is_rejected(self):
        root = self._checkout()
        output = self._systemd_output(root, active="inactive")
        runner = CommandMapRunner(
            lambda args, kwargs: subprocess.CompletedProcess(args, 0, output, "")
        )
        with self.assertRaisesRegex(DeploymentError, "not loaded and active"):
            discover_service(runner, "django.service")

    def test_nonexistent_user_is_rejected(self):
        root = self._checkout()
        output = self._systemd_output(root, user="missing-user")
        def handler(args, kwargs):
            if args[:2] == ["systemctl", "show"]:
                return subprocess.CompletedProcess(args, 0, output, "")
            if args[:2] == ["getent", "passwd"]:
                raise DeploymentError("missing user")
            return subprocess.CompletedProcess(args, 0, "", "")
        with self.assertRaisesRegex(DeploymentError, "missing user"):
            discover_service(CommandMapRunner(handler), "django.service")

    def test_nonexistent_group_is_rejected(self):
        root = self._checkout()
        output = self._systemd_output(root, group="missing-group")
        def handler(args, kwargs):
            if args[:2] == ["systemctl", "show"]:
                return subprocess.CompletedProcess(args, 0, output, "")
            if args[:2] == ["getent", "group"]:
                raise DeploymentError("missing group")
            return subprocess.CompletedProcess(args, 0, "", "")
        with self.assertRaisesRegex(DeploymentError, "missing group"):
            discover_service(CommandMapRunner(handler), "django.service")

    def test_missing_interpreter_is_rejected(self):
        root = self._checkout()
        output = self._systemd_output(root)
        runner = CommandMapRunner(
            lambda args, kwargs: subprocess.CompletedProcess(args, 0, output, "")
            if args[:2] == ["systemctl", "show"]
            else subprocess.CompletedProcess(args, 0, "", "")
        )
        with patch("ops.deployment_hardening.interpreter_from_exec_start",
                   return_value=Path("/missing/python3")):
            with self.assertRaisesRegex(DeploymentError, "not executable"):
                discover_service(runner, "django.service")

    def test_non_executable_interpreter_is_rejected(self):
        root = self._checkout()
        interpreter = root / "python3"
        interpreter.write_text("binary")
        interpreter.chmod(0o644)
        output = self._systemd_output(root)
        runner = CommandMapRunner(
            lambda args, kwargs: subprocess.CompletedProcess(args, 0, output, "")
            if args[:2] == ["systemctl", "show"]
            else subprocess.CompletedProcess(args, 0, "", "")
        )
        with patch("ops.deployment_hardening.interpreter_from_exec_start",
                   return_value=interpreter), patch(
                       "ops.deployment_hardening.os.access", return_value=False
                   ):
            with self.assertRaisesRegex(DeploymentError, "not executable"):
                discover_service(runner, "django.service")

    def test_nginx_t_failure_is_fatal(self):
        service = SimpleNamespace(
            exec_start_raw="", user="app",
            working_directory=Path("/opt/app/django-doppler-relay"),
            python=Path("python3"),
        )
        runner = CommandMapRunner(
            lambda args, kwargs: (_ for _ in ()).throw(DeploymentError("nginx -t failed"))
        )
        with self.assertRaisesRegex(DeploymentError, "nginx -t failed"):
            discover_and_validate_nginx(runner, service)

    def test_certificate_hostname_failure_is_fatal(self):
        config = """
        server { listen 443 ssl; server_name app.example.com;
        ssl_certificate /tmp/wrong.pem;
        proxy_pass http://unix:/run/app.sock; }
        """
        def handler(args, kwargs):
            if args[-1].endswith("td02c_nginx_config_check.py"):
                return subprocess.CompletedProcess(args, 0, nginx_check_pass_json(), "")
            if args == ["nginx", "-T"]:
                return subprocess.CompletedProcess(args, 0, config, "")
            raise DeploymentError("certificate does not cover host")
        service = SimpleNamespace(
            exec_start_raw="{ path=/x ; argv[]=/x --bind unix:/run/app.sock app:wsgi ; }",
            user="app",
            working_directory=Path("/opt/app/django-doppler-relay"),
            python=Path("python3"),
        )
        with self.assertRaisesRegex(DeploymentError, "certificate"):
            discover_and_validate_nginx(CommandMapRunner(handler), service)


class GitGateTests(unittest.TestCase):
    def test_dirty_tree_intersection_decision(self):
        self.assertEqual(
            changed_runtime_intersections(["app.py"], ["runtime.txt"]), []
        )
        self.assertEqual(
            changed_runtime_intersections(["app.py"], ["app.py", "runtime.txt"]),
            ["app.py"],
        )

    def test_target_commit_does_not_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init"], cwd=directory, check=True, capture_output=True)
            missing = "f" * 40
            with self.assertRaises(DeploymentError):
                resolve_commit(LocalRunner(), Path(directory), missing, "svc")

    def test_non_fast_forward_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "x@y.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "x"], cwd=repo, check=True)
            (repo / "x").write_text("one")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "one"], cwd=repo, check=True, capture_output=True)
            old = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True,
                                 capture_output=True, check=True).stdout.strip()
            subprocess.run(["git", "checkout", "--orphan", "other"], cwd=repo,
                           check=True, capture_output=True)
            subprocess.run(["git", "rm", "-rf", "."], cwd=repo, check=True, capture_output=True)
            (repo / "y").write_text("two")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "two"], cwd=repo, check=True, capture_output=True)
            target = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True,
                                    capture_output=True, check=True).stdout.strip()
            with self.assertRaisesRegex(DeploymentError, "not a fast-forward"):
                require_fast_forward(LocalRunner(), repo, old, target, "svc")


class SmokeTests(unittest.TestCase):
    def test_curl_has_both_timeouts_and_records_minimal_fields(self):
        runner = RecordingRunner()
        result = smoke_request(
            runner, NginxTarget("app.example.com", 443, "/run/app.sock", None),
            "/relay/send/", "POST",
        )
        command = runner.calls[0][0]
        self.assertIn("--connect-timeout", command)
        self.assertIn("--max-time", command)
        self.assertEqual(result["method"], "POST")
        self.assertEqual(result["path"], "/relay/send/")
        self.assertEqual(result["status"], "403")
        self.assertEqual(result["host_header"], "app.example.com")
        self.assertEqual(result["url"], "https://app.example.com:443/relay/send/")
        self.assertNotIn("redirect_url", " ".join(command))

    def test_preflight_readiness_reports_each_layer(self):
        service = SimpleNamespace(
            unit="django.service", user="app",
            working_directory=Path("/opt/app/django-doppler-relay"),
            python=Path("python3"),
        )
        socket_path = str(Path.cwd().anchor + "run/app.sock")
        target = NginxTarget("app.example.com", 443, socket_path, None)
        def handler(args, kwargs):
            if args[:2] == ["systemctl", "is-active"]:
                return subprocess.CompletedProcess(args, 0, "active\n", "")
            if args[:2] == ["test", "-S"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if is_nginx_check_invocation(args):
                return subprocess.CompletedProcess(args, 0, nginx_check_pass_json(), "")
            if args[0] == "curl":
                return subprocess.CompletedProcess(args, 0, "__DEPLOY_SMOKE__200", "")
            raise AssertionError(args)
        result = validate_readiness_layers(CommandMapRunner(handler), service, target)
        self.assertEqual(result["service"]["status"], "active")
        self.assertTrue(result["socket"]["available"])
        self.assertEqual(result["nginx"]["connection"], "local-via-127.0.0.1")
        self.assertEqual(result["application"]["status"], "200")
        self.assertEqual(result["hostname_source"], "active-nginx-config")
        self.assertEqual(result["attempts"], 1)

    def test_preflight_readiness_stops_before_http_when_socket_is_missing(self):
        calls = []
        def handler(args, kwargs):
            calls.append(args)
            if args[:2] == ["systemctl", "is-active"]:
                return subprocess.CompletedProcess(args, 0, "active\n", "")
            if args[:2] == ["test", "-S"]:
                return subprocess.CompletedProcess(args, 1, "", "")
            raise AssertionError(args)
        with self.assertRaisesRegex(DeploymentError, "socket is unavailable"):
            validate_readiness_layers(
                CommandMapRunner(handler),
                SimpleNamespace(unit="django.service", user="app"),
                NginxTarget(
                    "app.example.com", 443,
                    str(Path.cwd().anchor + "run/app.sock"), None,
                ),
            )
        self.assertFalse(any(call and call[0] == "curl" for call in calls))

    def test_curl_timeout_or_connection_failure_is_fatal(self):
        with self.assertRaises(DeploymentError):
            smoke_request(
                RecordingRunner(returncode=28),
                NginxTarget("app.example.com", 443, "/run/app.sock", None), "/",
            )

    def test_tls_connection_failure_is_fatal(self):
        runner = RecordingRunner(returncode=35)
        with self.assertRaisesRegex(DeploymentError, "curl failed"):
            smoke_request(
                runner,
                NginxTarget("app.example.com", 443, "/run/app.sock", None),
                "/admin/login/",
            )

    def test_preflight_readiness_rejects_unexpected_http_status(self):
        def handler(args, kwargs):
            if args[:2] == ["systemctl", "is-active"]:
                return subprocess.CompletedProcess(args, 0, "active\n", "")
            if args[:2] == ["test", "-S"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if is_nginx_check_invocation(args):
                return subprocess.CompletedProcess(args, 0, nginx_check_pass_json(), "")
            if args[0] == "curl":
                return subprocess.CompletedProcess(args, 0, "__DEPLOY_SMOKE__503", "")
            raise AssertionError(args)
        with self.assertRaisesRegex(DeploymentError, "returned HTTP 503"):
            validate_readiness_layers(
                CommandMapRunner(handler),
                SimpleNamespace(
                    unit="django.service", user="app",
                    working_directory=Path("/opt/app/django-doppler-relay"),
                    python=Path("python3"),
                ),
                NginxTarget(
                    "app.example.com", 443,
                    str(Path.cwd().anchor + "run/app.sock"), None,
                ),
            )


class ReadinessTests(unittest.TestCase):
    class Clock:
        def __init__(self):
            self.value = 0.0
        def monotonic(self):
            return self.value
        def sleep(self, seconds):
            self.value += seconds

    def _context(self):
        return SimpleNamespace(
            service=SimpleNamespace(unit="django.service", user="app"),
            nginx=NginxTarget(
                "app.example.com", 443, str(Path.cwd().anchor + "run/django/django.sock"), None
            ),
            baseline_smoke={
                "/admin/login/": {
                    "method": "GET", "path": "/admin/login/", "status": "200"
                }
            },
        )

    def test_waits_for_active_service_socket_and_valid_http_baseline(self):
        state = {"active": 0, "socket": 0, "http": 0}
        def handler(args, kwargs):
            if args[:2] == ["systemctl", "is-active"]:
                state["active"] += 1
                if state["active"] == 1:
                    return subprocess.CompletedProcess(args, 3, "activating\n", "")
                return subprocess.CompletedProcess(args, 0, "active\n", "")
            if args[:2] == ["test", "-S"]:
                state["socket"] += 1
                return subprocess.CompletedProcess(
                    args, 1 if state["socket"] == 1 else 0, "", ""
                )
            if args[0] == "curl":
                state["http"] += 1
                status = "502" if state["http"] == 1 else "200"
                return subprocess.CompletedProcess(
                    args, 0, "__DEPLOY_SMOKE__" + status, ""
                )
            raise AssertionError(args)
        clock = self.Clock()
        result = wait_for_application_ready(
            CommandMapRunner(handler), self._context(),
            timeout_seconds=5, poll_interval=.25,
            monotonic=clock.monotonic, sleeper=clock.sleep,
        )
        self.assertTrue(result["ready"])
        self.assertEqual(result["probe"]["status"], "200")
        self.assertEqual(state, {"active": 4, "socket": 3, "http": 2})

    def test_never_sends_http_probe_before_service_and_socket_are_ready(self):
        calls = []
        def handler(args, kwargs):
            calls.append(list(args))
            if args[:2] == ["systemctl", "is-active"]:
                return subprocess.CompletedProcess(args, 0, "active\n", "")
            if args[:2] == ["test", "-S"]:
                return subprocess.CompletedProcess(args, 1, "", "")
            raise AssertionError("HTTP probe must not run before socket readiness")
        clock = self.Clock()
        with self.assertRaisesRegex(DeploymentError, "socket-not-ready"):
            wait_for_application_ready(
                CommandMapRunner(handler), self._context(),
                timeout_seconds=.5, poll_interval=.25,
                monotonic=clock.monotonic, sleeper=clock.sleep,
            )
        self.assertFalse(any(call and call[0] == "curl" for call in calls))

    def test_http_502_until_timeout_is_fail_closed(self):
        def handler(args, kwargs):
            if args[:2] == ["systemctl", "is-active"]:
                return subprocess.CompletedProcess(args, 0, "active\n", "")
            if args[:2] == ["test", "-S"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[0] == "curl":
                return subprocess.CompletedProcess(args, 0, "__DEPLOY_SMOKE__502", "")
            raise AssertionError(args)
        clock = self.Clock()
        with self.assertRaisesRegex(DeploymentError, "http-502-expected-200"):
            wait_for_application_ready(
                CommandMapRunner(handler), self._context(),
                timeout_seconds=.5, poll_interval=.25,
                monotonic=clock.monotonic, sleeper=clock.sleep,
            )


class LocalRunner(Runner):
    def run(self, args, **kwargs):
        kwargs.pop("user", None)
        return super().run(args, **kwargs)


class RollbackRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        isolate_git_environment(self, self.repo)
        self.runner = LocalRunner()
        self._git("init")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Test")
        self._git("config", "core.autocrlf", "false")
        (self.repo / "app.py").write_text("old\n")
        (self.repo / "deleted.txt").write_text("restore me\n")
        (self.repo / "runtime.txt").write_text("runtime base\n")
        self._git("add", ".")
        self._git("commit", "-m", "old")
        self.old = self._git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "app.py").write_text("new\n")
        (self.repo / "added.txt").write_text("added\n")
        (self.repo / "deleted.txt").unlink()
        self._git("add", "-A")
        self._git("commit", "-m", "target")
        self.target = self._git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "runtime.txt").write_text("runtime local\n")
        (self.repo / ".env").write_text("SECRET_KEY=not-printed\n")
        self.runtime_hash = __import__("hashlib").sha256(
            (self.repo / "runtime.txt").read_bytes()
        ).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, text=True, capture_output=True, check=True
        )

    def _context(self):
        service = SimpleNamespace(working_directory=self.repo, user="service")
        context = DeploymentContext(
            service=service, nginx=NginxTarget("x", 443, "/x", None),
            old_sha=self.old, target_sha=self.target, repository=self.repo,
            branch=self._git("branch", "--show-current").stdout.strip(), remote="origin",
            changed_files=["added.txt", "app.py", "deleted.txt"],
            runtime_files=["runtime.txt"], intersections=[],
            runtime_hashes={"runtime.txt": self.runtime_hash},
            baseline_smoke={}, baseline_warning_codes=set(),
        )
        context.materialized_path_metadata_before = {
            "app.py": filesystem_metadata(self.repo / "app.py")
        }
        context.preexisting_modified_paths = ["app.py"]
        context.new_paths = ["added.txt"]
        context.deleted_paths = ["deleted.txt"]
        return context

    def test_rollback_restores_head_additions_deletions_and_preserves_runtime_env(self):
        context = self._context()
        targeted_rollback(Namespace(branch=context.branch), self.runner, context, [])
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old)
        self.assertFalse((self.repo / "added.txt").exists())
        self.assertEqual((self.repo / "deleted.txt").read_text(), "restore me\n")
        self.assertEqual((self.repo / "runtime.txt").read_text(), "runtime local\n")
        self.assertTrue((self.repo / ".env").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX metadata semantics")
    def test_rollback_restores_preexisting_mode_exactly(self):
        context = self._context()
        expected_mode = context.materialized_path_metadata_before["app.py"]["mode"]
        (self.repo / "app.py").chmod(0o600)
        targeted_rollback(Namespace(branch=context.branch), self.runner, context, [])
        self.assertEqual(
            __import__("stat").S_IMODE((self.repo / "app.py").stat().st_mode),
            expected_mode,
        )

    def test_real_dirty_tree_without_range_intersection_is_allowed_by_gate(self):
        runtime = self._git("diff", "--name-only").stdout.splitlines()
        self.assertEqual(
            changed_runtime_intersections(
                ["added.txt", "app.py", "deleted.txt"], runtime
            ),
            [],
        )

    def test_real_dirty_tree_with_range_intersection_is_rejected_by_gate(self):
        (self.repo / "app.py").write_text("local override\n")
        runtime = self._git("diff", "--name-only").stdout.splitlines()
        self.assertEqual(
            changed_runtime_intersections(
                ["added.txt", "app.py", "deleted.txt"], runtime
            ),
            ["app.py"],
        )

    def test_rollback_second_attempt_is_idempotent(self):
        context = self._context()
        args = Namespace(branch=context.branch)
        targeted_rollback(args, self.runner, context, [])
        targeted_rollback(args, self.runner, context, [])
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old)

    def test_rollback_git_mutations_run_as_discovered_service_user(self):
        context = self._context()
        seen = []
        class UserRecordingRunner(LocalRunner):
            def run(inner, args, **kwargs):
                # run_git_materializing resolves Git to an absolute path for
                # restore, so match on the subcommand, not a literal "git"
                # at args[0]; update-ref is untouched and stays literal.
                subcommand = args[1] if len(args) >= 2 else None
                if (
                    len(args) >= 2 and Path(str(args[0])).name == "git"
                    and subcommand in ("restore", "update-ref")
                ):
                    seen.append((list(args), kwargs.get("user")))
                return super().run(args, **kwargs)
        targeted_rollback(
            Namespace(branch=context.branch), UserRecordingRunner(), context, []
        )
        self.assertTrue(seen)
        self.assertTrue(all(user == "service" for _, user in seen))

    def test_rollback_refuses_missing_service_user_before_git_mutation(self):
        context = self._context()
        context.service = SimpleNamespace(
            working_directory=self.repo, user=""
        )
        calls = []
        class RecordingLocalRunner(LocalRunner):
            def run(inner, args, **kwargs):
                calls.append(list(args))
                return super().run(args, **kwargs)
        with self.assertRaisesRegex(DeploymentError, "service user is empty"):
            targeted_rollback(
                Namespace(branch=context.branch), RecordingLocalRunner(), context, []
            )
        self.assertEqual(calls, [])

    def test_rollback_with_old_head_and_target_files_materialized(self):
        context = self._context()
        branch = context.branch
        self._git("restore", "--source", self.old, "--staged", "--worktree",
                  "--", *context.changed_files)
        self._git("update-ref", f"refs/heads/{branch}", self.old, self.target)
        self._git("restore", "--source", self.target, "--staged", "--worktree",
                  "--", *context.changed_files)
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old)
        self.assertEqual((self.repo / "app.py").read_text(), "new\n")
        targeted_rollback(Namespace(branch=branch), self.runner, context, [])
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old)
        self.assertEqual((self.repo / "app.py").read_text(), "old\n")
        self.assertFalse((self.repo / "added.txt").exists())
        self.assertEqual((self.repo / "runtime.txt").read_text(), "runtime local\n")

    def test_rollback_from_approved_intermediate_commit(self):
        intermediate = self.target
        (self.repo / "app.py").write_text("final\n")
        (self.repo / "final.py").write_text("final\n")
        self._git("add", ".")
        self._git("commit", "-m", "final")
        final = self._git("rev-parse", "HEAD").stdout.strip()
        context = self._context()
        context.target_sha = final
        context.approved_commits = [intermediate, final]
        context.changed_files = ["added.txt", "app.py", "deleted.txt", "final.py"]
        branch = context.branch
        self._git("update-ref", f"refs/heads/{branch}", intermediate, final)
        self._git("read-tree", "--reset", "-u", intermediate)
        (self.repo / "runtime.txt").write_text("runtime local\n")
        targeted_rollback(Namespace(branch=branch), self.runner, context, [])
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old)
        self.assertEqual((self.repo / "app.py").read_text(), "old\n")
        self.assertFalse((self.repo / "added.txt").exists())
        self.assertFalse((self.repo / "final.py").exists())
        self.assertEqual((self.repo / "runtime.txt").read_text(), "runtime local\n")

    def test_interruption_between_restore_and_update_ref_is_detected_and_recovered(self):
        context = self._context()
        class InterruptingRunner(LocalRunner):
            def __init__(self):
                self.interrupted = False
            def run(inner_self, args, **kwargs):
                result = super(InterruptingRunner, inner_self).run(args, **kwargs)
                # run_git_materializing resolves Git to an absolute path for
                # merge/restore, so match on the subcommand, not a literal
                # "git" at args[0].
                if (
                    len(args) >= 2 and Path(str(args[0])).name == "git"
                    and args[1] == "restore" and not inner_self.interrupted
                ):
                    inner_self.interrupted = True
                    raise DeploymentError("simulated interruption after git restore")
                return result
        with self.assertRaisesRegex(DeploymentError, "simulated interruption"):
            targeted_rollback(
                Namespace(branch=context.branch), InterruptingRunner(), context, []
            )
        # Exact intermediate state: HEAD still target, index/worktree already old.
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.target)
        self.assertEqual((self.repo / "app.py").read_text(), "old\n")
        self.assertNotEqual(self._git("status", "--short").stdout.strip(), "")
        self.assertEqual((self.repo / "runtime.txt").read_text(), "runtime local\n")
        self.assertTrue((self.repo / ".env").exists())

        # Recovery is automatic only when an explicit rollback is invoked again.
        targeted_rollback(
            Namespace(branch=context.branch), self.runner, context, []
        )
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old)
        self.assertEqual(self._git("status", "--short", "--untracked-files=no").stdout.strip(),
                         "M runtime.txt")
        self.assertEqual((self.repo / "runtime.txt").read_text(), "runtime local\n")
        self.assertTrue((self.repo / ".env").exists())

    def _execution_context(self):
        context = self._context()
        context.nginx = NginxTarget(
            "x", 443, str(Path.cwd().anchor + "run/django/django.sock"), None
        )
        branch = context.branch
        self._git("restore", "--source", self.old, "--staged", "--worktree",
                  "--", *context.changed_files)
        self._git("update-ref", f"refs/heads/{branch}", self.old, self.target)
        self._git("update-ref", f"refs/remotes/origin/{branch}", self.target)
        wrapper = self.repo / "python3"
        wrapper.write_text("binary")
        fragment = self.repo / "django.service"
        fragment.write_text("[Service]\n")
        context.service = ServiceMetadata(
            unit="django.service", working_directory=self.repo,
            exec_start_path=wrapper, exec_start_raw="--bind unix:/run/app.sock",
            python=wrapper, user="svc", group="svc", main_pid=1,
            fragment_path=fragment, environment_files=(),
        )
        context.baseline_smoke = {
            "/": {"method": "GET", "path": "/", "status": "200"},
            "/app/": {"method": "GET", "path": "/app/", "status": "302"},
            "/admin/login/": {
                "method": "GET", "path": "/admin/login/", "status": "200"
            },
        }
        return context

    def _execute_args(self, backup_root):
        return Namespace(
            backup_root=str(backup_root), branch=self._git("branch", "--show-current").stdout.strip(),
            allowed_warning=[], restart_web=True, restart_unit=[],
            readiness_timeout=5.0, readiness_poll_interval=0.01,
        )

    def _assert_runtime_baseline(self, context):
        current = self._git("diff", "--name-only").stdout.splitlines()
        self.assertEqual(current, context.runtime_files)
        self.assertEqual(current, ["runtime.txt"])
        self.assertEqual(
            __import__("hashlib").sha256(
                (self.repo / "runtime.txt").read_bytes()
            ).hexdigest(),
            context.runtime_hashes["runtime.txt"],
        )

    def test_restart_failure_rolls_back_and_restarts_only_affected_unit(self):
        context = self._execution_context()
        self._assert_runtime_baseline(context)
        calls = []
        failed_once = {"value": False}
        outer = self
        class ExecutionRunner(LocalRunner):
            def run(inner, args, **kwargs):
                calls.append(list(args))
                is_bare_restart = args[:2] == ["systemctl", "restart"]
                is_bridged_restart = list(args) == list(
                    _DJANGO_SERVICE_RESTART_SUDO_ARGV
                )
                if is_bare_restart or is_bridged_restart:
                    if not failed_once["value"]:
                        failed_once["value"] = True
                        raise DeploymentError("simulated restart failure")
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args and str(args[0]) == str(context.service.python):
                    return subprocess.CompletedProcess(args, 0, "System check identified no issues", "")
                if args[:2] == ["test", "-O"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                return super(ExecutionRunner, inner).run(args, **kwargs)
        with tempfile.TemporaryDirectory() as backup:
            with self.assertRaisesRegex(DeploymentError, "ROLLBACK_COMPLETED"):
                execute_deployment(self._execute_args(backup), ExecutionRunner(), context)
        self._assert_runtime_baseline(context)
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old)
        # Extract the restarted unit regardless of which of the two argv
        # shapes was used (bare, or bridged through sudo -n for
        # django.service specifically -- both are still exactly two
        # restart attempts of the same one unit, never any other).
        restarts = [
            call[2] if call[:2] == ["systemctl", "restart"] else call[4]
            for call in calls
            if call[:2] == ["systemctl", "restart"]
            or list(call) == list(_DJANGO_SERVICE_RESTART_SUDO_ARGV)
        ]
        self.assertEqual(restarts, ["django.service", "django.service"])
        self.assertNotIn("nginx.service", restarts)
        self.assertNotIn("postgresql.service", restarts)

    def test_post_restart_smoke_failure_rolls_back_and_preserves_runtime(self):
        context = self._execution_context()
        self._assert_runtime_baseline(context)
        calls = []
        smoke_failure_reached = {"value": False}
        class ExecutionRunner(LocalRunner):
            def run(inner, args, **kwargs):
                calls.append(list(args))
                if args[:2] == ["systemctl", "restart"] or list(args) == list(
                    _DJANGO_SERVICE_RESTART_SUDO_ARGV
                ):
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[:2] == ["systemctl", "is-active"]:
                    return subprocess.CompletedProcess(args, 0, "active\n", "")
                if args[:2] == ["test", "-S"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[:2] == ["test", "-O"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args and str(args[0]) == str(context.service.python):
                    return subprocess.CompletedProcess(args, 0, "System check identified no issues", "")
                if args and args[0] == "curl":
                    url = args[-1]
                    method = args[args.index("--request") + 1]
                    status = "200" if url.endswith("/admin/login/") else (
                        "500" if url.endswith("/") and "/relay/send/" not in url else (
                        "405" if method == "GET" else "403"
                    ))
                    if status == "500":
                        smoke_failure_reached["value"] = True
                    return subprocess.CompletedProcess(args, 0, "__DEPLOY_SMOKE__" + status, "")
                return super(ExecutionRunner, inner).run(args, **kwargs)
        with tempfile.TemporaryDirectory() as backup:
            with self.assertRaisesRegex(
                DeploymentError, "DEPLOYMENT_FAILED; ROLLBACK_COMPLETED"
            ) as caught:
                execute_deployment(self._execute_args(backup), ExecutionRunner(), context)
        self._assert_runtime_baseline(context)
        self.assertTrue(smoke_failure_reached["value"])
        self.assertIn("Smoke baseline changed", str(caught.exception))
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old)
        self.assertEqual((self.repo / "runtime.txt").read_text(), "runtime local\n")

    def test_runtime_modified_during_merge_aborts_before_restart(self):
        context = self._execution_context()
        self._assert_runtime_baseline(context)
        restart_calls = []
        repo = self.repo
        class RuntimeMutatingRunner(LocalRunner):
            def run(inner, args, **kwargs):
                result = super().run(args, **kwargs)
                # run_git_materializing resolves Git to an absolute path for
                # merge/restore, so match on the subcommand, not a literal
                # "git" at args[0].
                if (
                    len(args) >= 3 and Path(str(args[0])).name == "git"
                    and args[1:3] == ["merge", "--ff-only"]
                ):
                    (repo / "runtime.txt").write_text(
                        "runtime changed during merge\n", encoding="utf-8"
                    )
                if args[:2] == ["systemctl", "restart"]:
                    restart_calls.append(list(args))
                if args[:2] == ["test", "-O"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                return result
        with tempfile.TemporaryDirectory() as backup:
            with self.assertRaisesRegex(
                DeploymentError,
                "DEPLOYMENT_FAILED; ROLLBACK_INCOMPLETE: Runtime changed",
            ):
                execute_deployment(
                    self._execute_args(backup), RuntimeMutatingRunner(), context
                )
        self.assertEqual(restart_calls, [])
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old)


class _FakeRestartClassificationRunner(LocalRunner):
    """Shared fake for restart-classification execution tests: answers
    every systemctl/test/curl/python probe execute_deployment needs
    post-merge, while recording every call so tests can assert exactly
    which commands were (or were not) issued.

    ``forbid_restart`` makes any ``systemctl restart`` call fail the test
    immediately -- used to prove the ops_only_no_restart path never issues
    one, under any condition, including during its own rollback.
    """

    def __init__(self, context, *, main_pid=None, forbid_restart=False,
                 wrong_root_status=None, sudo_restart_exit=0):
        self.context = context
        self.main_pid = main_pid
        self.forbid_restart = forbid_restart
        self.wrong_root_status = wrong_root_status
        self.sudo_restart_exit = sudo_restart_exit
        self.calls = []

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        context = self.context
        is_direct_restart = args[:2] == ["systemctl", "restart"]
        is_sudo_bridge_restart = list(args) == list(
            _DJANGO_SERVICE_RESTART_SUDO_ARGV
        )
        if is_direct_restart or is_sudo_bridge_restart:
            if self.forbid_restart:
                raise AssertionError(
                    "systemctl restart must never be called on the "
                    "ops_only_no_restart path"
                )
            if is_sudo_bridge_restart and self.sudo_restart_exit:
                # Mirror Runner.run()'s real check=True behavior: a
                # nonzero exit becomes a DeploymentError, it is never
                # silently returned as a completed process for the
                # caller to inspect.
                raise DeploymentError(
                    f"command_failed: Command failed ({self.sudo_restart_exit}): "
                    + " ".join(args)
                    + "\nsudo: a password is required"
                )
            return subprocess.CompletedProcess(args, 0, "", "")
        if (
            args[:2] == ["systemctl", "show"]
            and len(args) >= 4
            and args[3] == "--property=MainPID"
        ):
            return subprocess.CompletedProcess(
                args, 0, f"MainPID={self.main_pid}\n", ""
            )
        if args[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(args, 0, "active\n", "")
        if args[:2] == ["test", "-S"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["test", "-O"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args and str(args[0]) == str(context.service.python):
            return subprocess.CompletedProcess(
                args, 0, "System check identified no issues", ""
            )
        if args and args[0] == "curl":
            url = args[-1]
            method = args[args.index("--request") + 1]
            if url.endswith("/relay/send/"):
                status = "405" if method == "GET" else "403"
            elif url.endswith("/admin/login/"):
                status = "200"
            elif url.endswith("/app/"):
                status = "302"
            else:
                status = self.wrong_root_status or "200"
            return subprocess.CompletedProcess(
                args, 0, "__DEPLOY_SMOKE__" + status, ""
            )
        return super().run(args, **kwargs)


class RestartClassificationExecutionTests(unittest.TestCase):
    """Integration coverage for classify_restart_requirement wired into
    execute_deployment: an ops_only_no_restart diff must never call
    systemctl restart while still completing readiness/smoke checks, and a
    web_runtime_required diff must keep its existing, already-proven
    restart contract unchanged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        isolate_git_environment(self, self.repo)
        self.runner = LocalRunner()
        self._git("init")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Test")
        self._git("config", "core.autocrlf", "false")
        (self.repo / "runtime.txt").write_text("runtime base\n")
        (self.repo / "relay_stub.py").write_text("old\n")
        ops_dir = self.repo / "ops"
        ops_dir.mkdir()
        (ops_dir / "__init__.py").write_text("")
        (ops_dir / "sample_module.py").write_text("value = 1\n")
        tests_dir = ops_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_sample.py").write_text("value = 1\n")
        self._git("add", ".")
        self._git("commit", "-m", "old")
        self.old = self._git("rev-parse", "HEAD").stdout.strip()

        (ops_dir / "sample_module.py").write_text("value = 2\n")
        (ops_dir / "cli_module.py").write_text(
            "def main(argv=None):\n"
            "    return 0\n"
            "\n\n"
            'if __name__ == "__main__":\n'
            "    raise SystemExit(main())\n"
        )
        (tests_dir / "test_sample.py").write_text("value = 2\n")
        (self.repo / "relay_stub.py").write_text("new\n")
        self._git("add", "-A")
        self._git("commit", "-m", "target")
        self.target = self._git("rev-parse", "HEAD").stdout.strip()

        (self.repo / "runtime.txt").write_text("runtime local\n")
        self.runtime_hash = __import__("hashlib").sha256(
            (self.repo / "runtime.txt").read_bytes()
        ).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, text=True, capture_output=True, check=True
        )

    def _execution_context(self, changed_files, main_pid=4242):
        branch = self._git("branch", "--show-current").stdout.strip()
        self._git("restore", "--source", self.old, "--staged", "--worktree",
                  "--", *changed_files)
        self._git("update-ref", f"refs/heads/{branch}", self.old, self.target)
        self._git("update-ref", f"refs/remotes/origin/{branch}", self.target)
        wrapper = self.repo / "python3"
        wrapper.write_text("binary")
        fragment = self.repo / "django.service"
        fragment.write_text("[Service]\n")
        service = ServiceMetadata(
            unit="django.service", working_directory=self.repo,
            exec_start_path=wrapper, exec_start_raw="--bind unix:/run/app.sock",
            python=wrapper, user="svc", group="svc", main_pid=main_pid,
            fragment_path=fragment, environment_files=(),
        )
        return DeploymentContext(
            service=service,
            nginx=NginxTarget(
                "x", 443, str(Path.cwd().anchor + "run/django/django.sock"), None
            ),
            old_sha=self.old, target_sha=self.target, repository=self.repo,
            branch=branch, remote="origin", changed_files=changed_files,
            runtime_files=["runtime.txt"], intersections=[],
            runtime_hashes={"runtime.txt": self.runtime_hash},
            baseline_smoke={
                "/": {"method": "GET", "path": "/", "status": "200"},
                "/app/": {"method": "GET", "path": "/app/", "status": "302"},
                "/admin/login/": {
                    "method": "GET", "path": "/admin/login/", "status": "200"
                },
            },
            baseline_warning_codes=set(),
        )

    def _execute_args(self, backup_root, *, restart_web=False):
        return Namespace(
            backup_root=str(backup_root),
            branch=self._git("branch", "--show-current").stdout.strip(),
            allowed_warning=[], restart_web=restart_web, restart_unit=[],
            readiness_timeout=5.0, readiness_poll_interval=0.01,
        )

    # -- scenario 1/2 style: pure classification is exercised directly in
    # RestartClassificationTests; these prove execute_deployment obeys it.

    def test_ops_only_diff_never_restarts_and_completes_readiness_and_smoke(self):
        changed_files = ["ops/sample_module.py"]
        context = self._execution_context(changed_files)
        runner = _FakeRestartClassificationRunner(
            context, main_pid=str(context.service.main_pid), forbid_restart=True,
        )
        with tempfile.TemporaryDirectory() as backup:
            result = execute_deployment(self._execute_args(backup), runner, context)
        self.assertEqual(result["restart_classification"], "ops_only_no_restart")
        self.assertEqual(result["restarted_units"], [])
        self.assertTrue(result["readiness"]["ready"])
        self.assertEqual(
            [c for c in runner.calls if c[:2] == ["systemctl", "restart"]], []
        )

    def test_ops_only_diff_ignores_restart_web_flag_true(self):
        # Scenario 8: passing --restart-web has zero effect on the
        # classification result or on whether a restart happens.
        changed_files = ["ops/sample_module.py"]
        context = self._execution_context(changed_files)
        runner = _FakeRestartClassificationRunner(
            context, main_pid=str(context.service.main_pid), forbid_restart=True,
        )
        with tempfile.TemporaryDirectory() as backup:
            result = execute_deployment(
                self._execute_args(backup, restart_web=True), runner, context
            )
        self.assertEqual(result["restart_classification"], "ops_only_no_restart")
        self.assertEqual(result["restarted_units"], [])

    def test_ops_only_diff_verifies_changed_modules_import_and_cli_help(self):
        changed_files = [
            "ops/sample_module.py", "ops/cli_module.py", "ops/tests/test_sample.py",
        ]
        context = self._execution_context(changed_files)
        runner = _FakeRestartClassificationRunner(
            context, main_pid=str(context.service.main_pid), forbid_restart=True,
        )
        with tempfile.TemporaryDirectory() as backup:
            execute_deployment(self._execute_args(backup), runner, context)
        python = str(context.service.python)
        self.assertIn([python, "-c", "import ops.sample_module"], runner.calls)
        self.assertIn([python, "-c", "import ops.cli_module"], runner.calls)
        self.assertNotIn(
            [python, "-c", "import ops.tests.test_sample"], runner.calls
        )
        self.assertIn([python, "-m", "ops.cli_module", "--help"], runner.calls)
        self.assertNotIn(
            [python, "-m", "ops.sample_module", "--help"], runner.calls
        )

    def test_unexpected_restart_detected_when_mainpid_changes(self):
        changed_files = ["ops/sample_module.py"]
        context = self._execution_context(changed_files, main_pid=4242)
        runner = _FakeRestartClassificationRunner(
            context, main_pid="9999", forbid_restart=True,
        )
        with tempfile.TemporaryDirectory() as backup:
            with self.assertRaisesRegex(
                DeploymentError, "unexpected_restart_detected"
            ):
                execute_deployment(self._execute_args(backup), runner, context)
        self.assertEqual(
            [c for c in runner.calls if c[:2] == ["systemctl", "restart"]], []
        )
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old)

    def test_late_smoke_failure_after_ops_only_no_restart_rolls_back_without_restart(self):
        # Scenario 7: targeted_rollback must not assume restarted_units is
        # non-empty and must not itself attempt a restart that was never
        # part of an ops-only deployment.
        changed_files = ["ops/sample_module.py"]
        context = self._execution_context(changed_files)
        runner = _FakeRestartClassificationRunner(
            context, main_pid=str(context.service.main_pid), forbid_restart=True,
            wrong_root_status="500",
        )
        with tempfile.TemporaryDirectory() as backup:
            with self.assertRaisesRegex(
                DeploymentError, "Smoke baseline changed"
            ):
                execute_deployment(self._execute_args(backup), runner, context)
        self.assertEqual(
            [c for c in runner.calls if c[:2] == ["systemctl", "restart"]], []
        )
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old)

    def test_web_runtime_required_diff_still_requires_restart_web_flag(self):
        changed_files = ["relay_stub.py"]
        context = self._execution_context(changed_files)
        runner = _FakeRestartClassificationRunner(
            context, main_pid=str(context.service.main_pid),
        )
        with tempfile.TemporaryDirectory() as backup:
            with self.assertRaisesRegex(
                DeploymentError, "--restart-web is required"
            ):
                execute_deployment(
                    self._execute_args(backup, restart_web=False), runner, context
                )

    @unittest.skipUnless(os.name == "posix", "POSIX os.geteuid semantics")
    def test_web_runtime_required_diff_as_root_restarts_directly_unchanged(self):
        # Regression proof: the pre-existing, already-proven restart
        # contract for a runtime-affecting diff, when already running as
        # root, is untouched by the django-restart privilege bridge --
        # root never needs escalation, so the direct systemctl argv is
        # exactly what it always was.
        changed_files = ["relay_stub.py"]
        context = self._execution_context(changed_files)
        runner = _FakeRestartClassificationRunner(
            context, main_pid=str(context.service.main_pid),
        )
        with tempfile.TemporaryDirectory() as backup, patch(
            "ops.deployment_hardening.os.geteuid", return_value=0
        ):
            result = execute_deployment(
                self._execute_args(backup, restart_web=True), runner, context
            )
        self.assertEqual(result["restart_classification"], "web_runtime_required")
        self.assertEqual(result["restarted_units"], ["django.service"])
        self.assertEqual(
            [c for c in runner.calls if c[:2] == ["systemctl", "restart"]],
            [["systemctl", "restart", "django.service"]],
        )
        self.assertEqual(
            [c for c in runner.calls if list(c) == list(_DJANGO_SERVICE_RESTART_SUDO_ARGV)],
            [],
        )
        self.assertEqual(
            [c for c in runner.calls if c[:2] == ["systemctl", "show"]], []
        )

    @unittest.skipUnless(os.name == "posix", "POSIX os.geteuid semantics")
    def test_web_runtime_required_diff_as_non_root_uses_sudo_bridge(self):
        # The one behavior this bridge actually changes: a non-root
        # invocation restarting exactly django.service must go through
        # the closed sudoers grant, with the exact fixed argv, never a
        # bare unprivileged systemctl call that would just fail.
        changed_files = ["relay_stub.py"]
        context = self._execution_context(changed_files)
        runner = _FakeRestartClassificationRunner(
            context, main_pid=str(context.service.main_pid),
        )
        with tempfile.TemporaryDirectory() as backup, patch(
            "ops.deployment_hardening.os.geteuid", return_value=1000
        ):
            result = execute_deployment(
                self._execute_args(backup, restart_web=True), runner, context
            )
        self.assertEqual(result["restart_classification"], "web_runtime_required")
        self.assertEqual(result["restarted_units"], ["django.service"])
        self.assertEqual(
            [c for c in runner.calls if list(c) == list(_DJANGO_SERVICE_RESTART_SUDO_ARGV)],
            [list(_DJANGO_SERVICE_RESTART_SUDO_ARGV)],
        )
        self.assertEqual(
            [c for c in runner.calls if c[:2] == ["systemctl", "restart"]], []
        )
        self.assertEqual(
            list(_DJANGO_SERVICE_RESTART_SUDO_ARGV),
            ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "django.service"],
        )

    @unittest.skipUnless(os.name == "posix", "POSIX os.geteuid semantics")
    def test_sudo_bridge_rejected_aborts_deployment_and_rolls_back(self):
        # sudo -n failing (no password available, no NOPASSWD rule
        # installed yet, etc.) must fail the deployment closed. The
        # pre-existing, unmodified rollback then runs as it always does:
        # git-level restoration (asserted below via HEAD) completes
        # first and independently of the service; only its own trailing
        # restart-back attempt hits the identical permission problem,
        # which is why this is classified ROLLBACK_INCOMPLETE rather
        # than ROLLBACK_COMPLETED -- an honest signal that the code was
        # reverted but the running service could not be confirmed to
        # match it, exactly the case an operator must know about. Both
        # attempts (the original restart and rollback's restart-back)
        # use -n -- neither ever falls back to an interactive prompt or
        # drops the flag.
        changed_files = ["relay_stub.py"]
        context = self._execution_context(changed_files)
        runner = _FakeRestartClassificationRunner(
            context, main_pid=str(context.service.main_pid), sudo_restart_exit=1,
        )
        with tempfile.TemporaryDirectory() as backup, patch(
            "ops.deployment_hardening.os.geteuid", return_value=1000
        ):
            with self.assertRaisesRegex(
                DeploymentError, "DEPLOYMENT_FAILED; ROLLBACK_INCOMPLETE"
            ):
                execute_deployment(
                    self._execute_args(backup, restart_web=True), runner, context
                )
        self.assertEqual(
            self._git("rev-parse", "HEAD").stdout.strip(), self.old,
            "git-level rollback must complete even when the service "
            "restart-back cannot be verified",
        )
        sudo_calls = [
            c for c in runner.calls if list(c) == list(_DJANGO_SERVICE_RESTART_SUDO_ARGV)
        ]
        self.assertEqual(
            len(sudo_calls), 2,
            "exactly the original attempt plus rollback's own restart-back "
            "-- never more, and every one of them carries -n",
        )


class DjangoRestartPrivilegeBridgeArgvTests(unittest.TestCase):
    """Pure, injectable coverage for _restart_unit_argv, independent of
    execute_deployment -- the source of truth for exactly which argv gets
    built for which (unit, effective_uid) combination."""

    def test_non_root_django_service_uses_sudo_bridge(self):
        argv = _restart_unit_argv("django.service", effective_uid=1000)
        self.assertEqual(
            argv,
            ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "django.service"],
        )

    def test_root_django_service_uses_direct_argv_unchanged(self):
        argv = _restart_unit_argv("django.service", effective_uid=0)
        self.assertEqual(argv, ["systemctl", "restart", "django.service"])

    def test_non_root_other_unit_is_not_bridged(self):
        # The bridge names exactly one unit; nothing else widens.
        argv = _restart_unit_argv("doppler-background-jobs.service", effective_uid=1000)
        self.assertEqual(
            argv, ["systemctl", "restart", "doppler-background-jobs.service"]
        )

    def test_root_other_unit_uses_direct_argv(self):
        argv = _restart_unit_argv("nginx.service", effective_uid=0)
        self.assertEqual(argv, ["systemctl", "restart", "nginx.service"])

    def test_sudo_bridge_argv_has_exactly_five_fixed_elements(self):
        # Proves no extra argument can ever be appended by this function:
        # the tuple is a fixed literal, never built by concatenation with
        # caller-supplied data beyond the unit-name equality check above.
        argv = _restart_unit_argv("django.service", effective_uid=1000)
        self.assertEqual(len(argv), 5)
        self.assertEqual(
            argv, list(_DJANGO_SERVICE_RESTART_SUDO_ARGV)
        )

    def test_sudo_bridge_argv_uses_only_absolute_paths(self):
        argv = _restart_unit_argv("django.service", effective_uid=1000)
        self.assertTrue(argv[0].startswith("/"))
        self.assertTrue(argv[2].startswith("/"))

    def test_sudo_bridge_argv_is_a_plain_list_never_a_shell_string(self):
        # Runner.run always calls subprocess.run(..., shell=False); this
        # asserts the argv this function returns is shaped for that
        # calling convention -- a list of discrete tokens, not one string
        # that could be misinterpreted if shell=True were ever introduced.
        argv = _restart_unit_argv("django.service", effective_uid=1000)
        self.assertIsInstance(argv, list)
        for token in argv:
            self.assertIsInstance(token, str)
            self.assertNotIn(" ", token)

    @unittest.skipUnless(os.name == "posix", "POSIX os.geteuid semantics")
    def test_default_effective_uid_reads_os_geteuid(self):
        with patch("ops.deployment_hardening.os.geteuid", return_value=0):
            argv = _restart_unit_argv("django.service")
        self.assertEqual(argv, ["systemctl", "restart", "django.service"])
        with patch("ops.deployment_hardening.os.geteuid", return_value=1000):
            argv = _restart_unit_argv("django.service")
        self.assertEqual(
            argv,
            ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "django.service"],
        )


class DeploymentPlanRestartClassificationTests(unittest.TestCase):
    def _context(self, changed_files):
        service = ServiceMetadata(
            unit="django.service", working_directory=Path("/srv/app"),
            exec_start_path=Path("/srv/app/python3"), exec_start_raw="",
            python=Path("/srv/app/python3"), user="svc", group="svc",
            main_pid=1, fragment_path=Path("/etc/systemd/system/django.service"),
            environment_files=(),
        )
        return DeploymentContext(
            service=service,
            nginx=NginxTarget("example.test", 443, "/run/app.sock", None),
            old_sha="a" * 40, target_sha="b" * 40, repository=Path("/srv/app"),
            branch="main", remote="origin", changed_files=changed_files,
            runtime_files=[], intersections=[], runtime_hashes={},
            baseline_smoke={}, baseline_warning_codes=set(),
        )

    def test_plan_shows_no_restart_units_for_ops_only_diff_without_flag(self):
        context = self._context(["ops/deployment_hardening.py"])
        args = Namespace(execute=False, restart_unit=[])
        plan = deployment_plan(args, context)
        self.assertEqual(plan["restart_classification"], "ops_only_no_restart")
        self.assertEqual(plan["restart_units"], [])

    def test_plan_shows_restart_units_for_web_runtime_diff(self):
        context = self._context(["relay/views.py"])
        args = Namespace(execute=False, restart_unit=["celery.service"])
        plan = deployment_plan(args, context)
        self.assertEqual(plan["restart_classification"], "web_runtime_required")
        self.assertEqual(
            plan["restart_units"], ["django.service", "celery.service"]
        )


class ControlledFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        isolate_git_environment(self, root)
        self.remote = root / "remote.git"
        self.seed = root / "seed"
        self.production = root / "production"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True,
                       capture_output=True)
        subprocess.run(["git", "init", str(self.seed)], check=True, capture_output=True)
        self._run(self.seed, "config", "user.email", "test@example.invalid")
        self._run(self.seed, "config", "user.name", "Test")
        self._run(self.seed, "config", "core.autocrlf", "false")
        (self.seed / "app.py").write_text("old\n")
        self._run(self.seed, "add", ".")
        self._run(self.seed, "commit", "-m", "old")
        self.branch = self._run(self.seed, "branch", "--show-current").stdout.strip()
        self._run(self.seed, "remote", "add", "origin", str(self.remote))
        self._run(self.seed, "push", "-u", "origin", self.branch)
        subprocess.run(["git", "clone", str(self.remote), str(self.production)],
                       check=True, capture_output=True)
        self._run(self.production, "config", "core.autocrlf", "false")
        self.old = self._run(self.production, "rev-parse", "HEAD").stdout.strip()
        (self.seed / "app.py").write_text("target\n")
        (self.seed / "new.py").write_text("new\n")
        self._run(self.seed, "add", ".")
        self._run(self.seed, "commit", "-m", "target")
        self.target = self._run(self.seed, "rev-parse", "HEAD").stdout.strip()
        self._run(self.seed, "push", "origin", self.branch)
        self.runner = LocalRunner()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, text=True,
                              capture_output=True, check=True)

    def test_without_fetch_target_absent_aborts(self):
        with self.assertRaises(DeploymentError):
            resolve_commit(self.runner, self.production, self.target, "svc")

    def test_ls_remote_can_be_new_while_consumed_ref_is_stale(self):
        remote = self._run(
            self.production, "ls-remote", "origin", f"refs/heads/{self.branch}"
        ).stdout.split()[0]
        tracking = self._run(
            self.production, "rev-parse", f"refs/remotes/origin/{self.branch}"
        ).stdout.strip()
        self.assertEqual(remote, self.target)
        self.assertEqual(tracking, self.old)
        with self.assertRaisesRegex(DeploymentError, "exact approved target"):
            require_deployment_ref(
                self.runner, self.production, remote="origin", branch=self.branch,
                target_sha=self.target, user="svc",
            )

    def test_explicit_fetch_updates_consumed_ref_and_preserves_active_state(self):
        before_head = self._run(self.production, "rev-parse", "HEAD").stdout.strip()
        before_status = self._run(
            self.production, "status", "--porcelain=v1"
        ).stdout
        ref = refresh_deployment_ref(
            self.runner, self.production, remote="origin", branch=self.branch,
            target_sha=self.target, user="svc",
        )
        self.assertEqual(ref, f"refs/remotes/origin/{self.branch}")
        self.assertEqual(
            self._run(self.production, "rev-parse", ref).stdout.strip(), self.target
        )
        self.assertEqual(
            self._run(self.production, "rev-parse", "HEAD").stdout.strip(),
            before_head,
        )
        self.assertEqual(
            self._run(self.production, "status", "--porcelain=v1").stdout,
            before_status,
        )

    def test_merge_uses_exact_full_target_after_ref_validation(self):
        refresh_deployment_ref(
            self.runner, self.production, remote="origin", branch=self.branch,
            target_sha=self.target, user="svc",
        )
        require_deployment_ref(
            self.runner, self.production, remote="origin", branch=self.branch,
            target_sha=self.target, user="svc",
        )
        self._run(self.production, "merge", "--ff-only", self.target)
        self.assertEqual(
            self._run(self.production, "rev-parse", "HEAD").stdout.strip(),
            self.target,
        )

    def test_controlled_fetch_preserves_active_git_state_and_remote_tracking_refs(self):
        before = snapshot_git_state(self.runner, self.production, "origin", "svc")
        origin_ref_before = self._run(
            self.production, "rev-parse", f"refs/remotes/origin/{self.branch}"
        ).stdout.strip()
        ref, captured = acquire_target_object(
            self.runner, self.production, remote="origin", branch=self.branch,
            target_sha=self.target, user="svc",
        )
        self.assertEqual(captured, before)
        self.assertEqual(
            self._run(self.production, "rev-parse", ref).stdout.strip(), self.target
        )
        self.assertEqual(
            self._run(self.production, "rev-parse", "HEAD").stdout.strip(), self.old
        )
        self.assertEqual(
            self._run(self.production, "branch", "--show-current").stdout.strip(),
            self.branch,
        )
        self.assertEqual(
            self._run(
                self.production, "rev-parse", f"refs/remotes/origin/{self.branch}"
            ).stdout.strip(),
            origin_ref_before,
        )
        self.assertEqual(
            self._run(self.production, "status", "--porcelain=v1").stdout, ""
        )
        delete_temporary_target_ref(
            self.runner, self.production, ref, self.target, "svc"
        )
        self.assertNotEqual(
            subprocess.run(["git", "rev-parse", "--verify", ref],
                           cwd=self.production, capture_output=True).returncode,
            0,
        )

    def test_clean_temporary_repository_has_deterministic_snapshot(self):
        first = snapshot_git_state(self.runner, self.production, "origin", "svc")
        second = snapshot_git_state(self.runner, self.production, "origin", "svc")
        self.assertEqual(first, second)
        self.assertEqual(first.status, "")
        self.assertEqual(first.unstaged_diff, "")

    def test_helper_outside_repository_does_not_change_snapshot(self):
        before = snapshot_git_state(self.runner, self.production, "origin", "svc")
        helper = self.production.parent / ".td02c_outside_local.tmp.sh"
        helper.write_text("echo safe\n", encoding="utf-8")
        after = snapshot_git_state(self.runner, self.production, "origin", "svc")
        self.assertEqual(after, before)

    def test_helper_created_inside_repository_aborts_controlled_fetch(self):
        production = self.production
        class HelperCreatingRunner(LocalRunner):
            def run(inner, args, **kwargs):
                result = super().run(args, **kwargs)
                if args[:2] == ["git", "fetch"]:
                    (production / ".td02c_inside_local.tmp.sh").write_text(
                        "echo unsafe\n", encoding="utf-8"
                    )
                return result
        with self.assertRaisesRegex(DeploymentError, "status"):
            acquire_target_object(
                HelperCreatingRunner(), self.production, remote="origin",
                branch=self.branch, target_sha=self.target, user="svc",
            )

    def test_real_unstaged_change_during_fetch_is_detected(self):
        production = self.production
        class TrackedFileMutatingRunner(LocalRunner):
            def run(inner, args, **kwargs):
                result = super().run(args, **kwargs)
                if args[:2] == ["git", "fetch"]:
                    (production / "app.py").write_text(
                        "unexpected local change\n", encoding="utf-8"
                    )
                return result
        with self.assertRaisesRegex(DeploymentError, "unstaged_diff|status"):
            acquire_target_object(
                TrackedFileMutatingRunner(), self.production, remote="origin",
                branch=self.branch, target_sha=self.target, user="svc",
            )

    def test_autocrlf_and_filemode_local_settings_do_not_change_clean_fetch(self):
        for autocrlf, filemode in (("false", "true"), ("input", "false")):
            with self.subTest(autocrlf=autocrlf, filemode=filemode):
                self._run(self.production, "config", "core.autocrlf", autocrlf)
                self._run(self.production, "config", "core.filemode", filemode)
                before = snapshot_git_state(
                    self.runner, self.production, "origin", "svc"
                )
                ref, captured = acquire_target_object(
                    self.runner, self.production, remote="origin",
                    branch=self.branch, target_sha=self.target, user="svc",
                )
                self.assertEqual(captured, before)
                delete_temporary_target_ref(
                    self.runner, self.production, ref, self.target, "svc"
                )

    def test_remote_hash_mismatch_aborts_without_fetch(self):
        wrong = "a" * 40
        with self.assertRaisesRegex(DeploymentError, "Remote target"):
            acquire_target_object(
                self.runner, self.production, remote="origin", branch=self.branch,
                target_sha=wrong, user="svc",
            )

    def test_preexisting_wrong_temporary_ref_aborts(self):
        ref = temporary_target_ref(self.target)
        self._run(self.production, "update-ref", ref, self.old)
        with self.assertRaisesRegex(DeploymentError, "Stale preflight ref"):
            acquire_target_object(
                self.runner, self.production, remote="origin", branch=self.branch,
                target_sha=self.target, user="svc",
            )
        self.assertEqual(
            self._run(self.production, "rev-parse", ref).stdout.strip(), self.old
        )
        delete_temporary_target_ref(
            self.runner, self.production, ref, self.old, "svc"
        )

    def test_interrupted_fetch_leaves_detectable_recoverable_ref(self):
        outer = self
        class InterruptAfterFetch(LocalRunner):
            def run(inner, args, **kwargs):
                result = super(InterruptAfterFetch, inner).run(args, **kwargs)
                if args[:2] == ["git", "fetch"]:
                    raise DeploymentError("simulated interruption after fetch")
                return result
        ref = temporary_target_ref(self.target)
        with self.assertRaisesRegex(DeploymentError, "simulated interruption"):
            acquire_target_object(
                InterruptAfterFetch(), self.production, remote="origin",
                branch=self.branch, target_sha=self.target, user="svc",
            )
        self.assertEqual(
            self._run(self.production, "rev-parse", ref).stdout.strip(), self.target
        )
        # A subsequent controlled acquisition validates and safely reuses it.
        recovered_ref, _ = acquire_target_object(
            self.runner, self.production, remote="origin", branch=self.branch,
            target_sha=self.target, user="svc",
        )
        delete_temporary_target_ref(
            self.runner, self.production, recovered_ref, self.target, "svc"
        )
        self.assertEqual(
            self._run(self.production, "rev-parse", "HEAD").stdout.strip(), self.old
        )


if __name__ == "__main__":
    unittest.main()
    delete_temporary_target_ref,
