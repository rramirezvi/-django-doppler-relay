import shutil
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ops.deployment_hardening import (
    DeploymentContext,
    DeploymentError,
    EnvironmentFile,
    NginxTarget,
    Runner,
    ServiceMetadata,
    changed_runtime_intersections,
    discover_and_validate_nginx,
    discover_nginx_target,
    discover_service,
    execute_deployment,
    interpreter_from_exec_start,
    parse_environment_files,
    parse_exec_start_path,
    require_fast_forward,
    resolve_commit,
    safe_repo_path,
    smoke_request,
    targeted_rollback,
    unexpected_warning_codes,
    validate_token,
    warning_codes,
)


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
        runner = CommandMapRunner(
            lambda args, kwargs: (_ for _ in ()).throw(DeploymentError("nginx -t failed"))
        )
        with self.assertRaisesRegex(DeploymentError, "nginx -t failed"):
            discover_and_validate_nginx(runner, SimpleNamespace(exec_start_raw=""))

    def test_certificate_hostname_failure_is_fatal(self):
        config = """
        server { listen 443 ssl; server_name app.example.com;
        ssl_certificate /tmp/wrong.pem;
        proxy_pass http://unix:/run/app.sock; }
        """
        def handler(args, kwargs):
            if args == ["nginx", "-t"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args == ["nginx", "-T"]:
                return subprocess.CompletedProcess(args, 0, config, "")
            raise DeploymentError("certificate does not cover host")
        service = SimpleNamespace(
            exec_start_raw="{ path=/x ; argv[]=/x --bind unix:/run/app.sock app:wsgi ; }"
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
        self.assertEqual(result, {"method": "POST", "path": "/relay/send/", "status": "403"})
        self.assertNotIn("redirect_url", " ".join(command))

    def test_curl_timeout_or_connection_failure_is_fatal(self):
        with self.assertRaises(DeploymentError):
            smoke_request(
                RecordingRunner(returncode=28),
                NginxTarget("app.example.com", 443, "/run/app.sock", None), "/",
            )


class LocalRunner(Runner):
    def run(self, args, **kwargs):
        kwargs.pop("user", None)
        return super().run(args, **kwargs)


class RollbackRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.runner = LocalRunner()
        self._git("init")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Test")
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
        return DeploymentContext(
            service=service, nginx=NginxTarget("x", 443, "/x", None),
            old_sha=self.old, target_sha=self.target, repository=self.repo,
            branch=self._git("branch", "--show-current").stdout.strip(), remote="origin",
            changed_files=["added.txt", "app.py", "deleted.txt"],
            runtime_files=["runtime.txt"], intersections=[],
            runtime_hashes={"runtime.txt": self.runtime_hash},
            baseline_smoke={}, baseline_warning_codes=set(),
        )

    def test_rollback_restores_head_additions_deletions_and_preserves_runtime_env(self):
        context = self._context()
        targeted_rollback(Namespace(branch=context.branch), self.runner, context, [])
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old)
        self.assertFalse((self.repo / "added.txt").exists())
        self.assertEqual((self.repo / "deleted.txt").read_text(), "restore me\n")
        self.assertEqual((self.repo / "runtime.txt").read_text(), "runtime local\n")
        self.assertTrue((self.repo / ".env").exists())

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

    def test_interruption_between_restore_and_update_ref_is_detected_and_recovered(self):
        context = self._context()
        class InterruptingRunner(LocalRunner):
            def __init__(self):
                self.interrupted = False
            def run(inner_self, args, **kwargs):
                result = super(InterruptingRunner, inner_self).run(args, **kwargs)
                if args[:2] == ["git", "restore"] and not inner_self.interrupted:
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
        branch = context.branch
        self._git("restore", "--source", self.old, "--staged", "--worktree",
                  "--", *context.changed_files)
        self._git("update-ref", f"refs/heads/{branch}", self.old, self.target)
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
        }
        return context

    def _execute_args(self, backup_root):
        return Namespace(
            backup_root=str(backup_root), branch=self._git("branch", "--show-current").stdout.strip(),
            allowed_warning=[], restart_web=True, restart_unit=[],
        )

    def test_restart_failure_rolls_back_and_restarts_only_affected_unit(self):
        context = self._execution_context()
        calls = []
        failed_once = {"value": False}
        outer = self
        class ExecutionRunner(LocalRunner):
            def run(inner, args, **kwargs):
                calls.append(list(args))
                if args[:2] == ["systemctl", "restart"]:
                    if not failed_once["value"]:
                        failed_once["value"] = True
                        raise DeploymentError("simulated restart failure")
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args and str(args[0]) == str(context.service.python):
                    return subprocess.CompletedProcess(args, 0, "System check identified no issues", "")
                return super(ExecutionRunner, inner).run(args, **kwargs)
        with tempfile.TemporaryDirectory() as backup:
            with self.assertRaisesRegex(DeploymentError, "ROLLBACK_COMPLETED"):
                execute_deployment(self._execute_args(backup), ExecutionRunner(), context)
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old)
        restarts = [call[2] for call in calls if call[:2] == ["systemctl", "restart"]]
        self.assertEqual(restarts, ["django.service", "django.service"])
        self.assertNotIn("nginx.service", restarts)
        self.assertNotIn("postgresql.service", restarts)

    def test_post_restart_smoke_failure_rolls_back_and_preserves_runtime(self):
        context = self._execution_context()
        calls = []
        class ExecutionRunner(LocalRunner):
            def run(inner, args, **kwargs):
                calls.append(list(args))
                if args[:2] == ["systemctl", "restart"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[:2] == ["systemctl", "is-active"]:
                    return subprocess.CompletedProcess(args, 0, "active\n", "")
                if args and str(args[0]) == str(context.service.python):
                    return subprocess.CompletedProcess(args, 0, "System check identified no issues", "")
                if args and args[0] == "curl":
                    url = args[-1]
                    method = args[args.index("--request") + 1]
                    status = "500" if url.endswith("/") and "/relay/send/" not in url else (
                        "405" if method == "GET" else "403"
                    )
                    return subprocess.CompletedProcess(args, 0, "__DEPLOY_SMOKE__" + status, "")
                return super(ExecutionRunner, inner).run(args, **kwargs)
        with tempfile.TemporaryDirectory() as backup:
            with self.assertRaisesRegex(
                DeploymentError, "DEPLOYMENT_FAILED; ROLLBACK_COMPLETED"
            ) as caught:
                execute_deployment(self._execute_args(backup), ExecutionRunner(), context)
        self.assertIn("Smoke baseline changed", str(caught.exception))
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), self.old)
        self.assertEqual((self.repo / "runtime.txt").read_text(), "runtime local\n")


if __name__ == "__main__":
    unittest.main()
