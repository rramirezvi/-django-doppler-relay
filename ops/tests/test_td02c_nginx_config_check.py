from __future__ import annotations

import inspect
import json
import subprocess
import unittest
from unittest.mock import patch

from ops.td02c_nginx_config_check import (
    CLASSIFICATIONS,
    MINIMAL_ENV,
    NGINX_BINARY,
    SUDO_BINARY,
    SUDOERS_CONTENT,
    SUDOERS_FILENAME,
    check,
    main,
)


def completed(argv, returncode=0, stdout=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, "")


def patch_binaries(*, nginx_present=True, sudo_present=True):
    return patch(
        "ops.td02c_nginx_config_check.os.path.isfile",
        side_effect=lambda path: {NGINX_BINARY: nginx_present, SUDO_BINARY: sudo_present}.get(path, False),
    ), patch("ops.td02c_nginx_config_check.os.access", return_value=True)


class NginxConfigCheckTests(unittest.TestCase):
    # 1. nginx -t directo PASS
    def test_direct_pass(self):
        isfile, access = patch_binaries()
        with isfile, access, patch(
            "ops.td02c_nginx_config_check.subprocess.run", return_value=completed([NGINX_BINARY, "-t"], 0, "")
        ) as run:
            result = check()
        self.assertEqual(result.classification, "nginx_check_direct_passed")
        self.assertEqual(result.method, "direct")
        self.assertTrue(result.passed)
        run.assert_called_once()

    # 2. directo permission denied -> helper privilegiado PASS
    def test_direct_permission_denied_then_privileged_pass(self):
        isfile, access = patch_binaries()
        calls = []

        def side_effect(argv, **kwargs):
            calls.append(argv)
            if argv[0] == NGINX_BINARY:
                return completed(argv, 1, "cannot load certificate: Permission denied calling fopen(...)")
            if argv[:2] == [SUDO_BINARY, "-n"]:
                return completed(argv, 0, "")
            raise AssertionError(argv)

        with isfile, access, patch("ops.td02c_nginx_config_check.subprocess.run", side_effect=side_effect):
            result = check()
        self.assertEqual(result.classification, "nginx_check_privileged_passed")
        self.assertEqual(result.method, "privileged")
        self.assertEqual(calls[1], [SUDO_BINARY, "-n", NGINX_BINARY, "-t"])

    # config invalida (sin escalar) -- nunca debe intentar sudo
    def test_syntax_error_never_escalates(self):
        isfile, access = patch_binaries()
        with isfile, access, patch(
            "ops.td02c_nginx_config_check.subprocess.run",
            return_value=completed([NGINX_BINARY, "-t"], 1, "nginx: [emerg] unexpected end of file"),
        ) as run:
            result = check()
        self.assertEqual(result.classification, "nginx_check_config_invalid")
        self.assertEqual(result.method, "direct")
        run.assert_called_once()  # never escalated

    # helper (sudo binary) ausente
    def test_sudo_binary_missing(self):
        isfile, access = patch_binaries(sudo_present=False)
        with isfile, access, patch(
            "ops.td02c_nginx_config_check.subprocess.run",
            return_value=completed([NGINX_BINARY, "-t"], 1, "Permission denied"),
        ):
            result = check()
        self.assertEqual(result.classification, "nginx_check_sudoers_missing")

    # sudoers ausente (sudo exige password / no hay entrada NOPASSWD)
    def test_sudoers_missing_entry(self):
        isfile, access = patch_binaries()

        def side_effect(argv, **kwargs):
            if argv[0] == NGINX_BINARY:
                return completed(argv, 1, "Permission denied")
            return completed(argv, 1, "sudo: a password is required")

        with isfile, access, patch("ops.td02c_nginx_config_check.subprocess.run", side_effect=side_effect):
            result = check()
        self.assertEqual(result.classification, "nginx_check_sudoers_missing")

    # sudoers presente pero rechaza el comando exacto
    def test_command_rejected_by_sudoers(self):
        isfile, access = patch_binaries()

        def side_effect(argv, **kwargs):
            if argv[0] == NGINX_BINARY:
                return completed(argv, 1, "Permission denied")
            return completed(argv, 1, "Sorry, user app is not allowed to execute '/usr/sbin/nginx -t' as root.")

        with isfile, access, patch("ops.td02c_nginx_config_check.subprocess.run", side_effect=side_effect):
            result = check()
        self.assertEqual(result.classification, "nginx_check_command_rejected")

    # sudoers.d con error de sintaxis
    def test_sudoers_file_invalid(self):
        isfile, access = patch_binaries()

        def side_effect(argv, **kwargs):
            if argv[0] == NGINX_BINARY:
                return completed(argv, 1, "Permission denied")
            return completed(argv, 1, ">>> /etc/sudoers.d/td02c-nginx-check: syntax error near line 3 <<<")

        with isfile, access, patch("ops.td02c_nginx_config_check.subprocess.run", side_effect=side_effect):
            result = check()
        self.assertEqual(result.classification, "nginx_check_sudoers_invalid")

    # config invalida detectada solo tras escalar (privilegiado)
    def test_privileged_config_invalid(self):
        isfile, access = patch_binaries()

        def side_effect(argv, **kwargs):
            if argv[0] == NGINX_BINARY:
                return completed(argv, 1, "Permission denied")
            return completed(argv, 1, "nginx: [emerg] invalid directive")

        with isfile, access, patch("ops.td02c_nginx_config_check.subprocess.run", side_effect=side_effect):
            result = check()
        self.assertEqual(result.classification, "nginx_check_config_invalid")
        self.assertEqual(result.method, "privileged")

    # nginx binario ausente
    def test_nginx_binary_missing(self):
        isfile, access = patch_binaries(nginx_present=False)
        with isfile, access:
            result = check()
        self.assertEqual(result.classification, "nginx_check_unexpected_error")

    # excepcion inesperada (timeout) durante el intento directo
    def test_unexpected_subprocess_error(self):
        isfile, access = patch_binaries()
        with isfile, access, patch(
            "ops.td02c_nginx_config_check.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=[NGINX_BINARY, "-t"], timeout=15),
        ):
            result = check()
        self.assertEqual(result.classification, "nginx_check_unexpected_error")

    # exit code preservado
    def test_exit_code_is_preserved(self):
        isfile, access = patch_binaries()
        with isfile, access, patch(
            "ops.td02c_nginx_config_check.subprocess.run",
            return_value=completed([NGINX_BINARY, "-t"], 42, "nginx: [emerg] boom"),
        ):
            result = check()
        self.assertEqual(result.exit_code, 42)

    # main() rechaza cualquier argumento (defensa adicional)
    def test_main_rejects_any_argument(self):
        stdout = []
        with patch("builtins.print", side_effect=lambda value: stdout.append(value)):
            code = main(["--anything"])
        self.assertEqual(code, 1)
        payload = json.loads(stdout[0])
        self.assertEqual(payload["classification"], "nginx_check_command_rejected")

    # main() con exito imprime JSON PASS y retorna 0
    def test_main_success_returns_zero(self):
        isfile, access = patch_binaries()
        stdout = []
        with isfile, access, patch(
            "ops.td02c_nginx_config_check.subprocess.run", return_value=completed([NGINX_BINARY, "-t"], 0, "")
        ), patch("builtins.print", side_effect=lambda value: stdout.append(value)):
            code = main([])
        self.assertEqual(code, 0)
        payload = json.loads(stdout[0])
        self.assertEqual(payload["result"], "PASS")

    # cero secretos: el detalle sanitizado nunca contiene mas que una linea corta
    def test_detail_is_sanitized_short_single_line(self):
        isfile, access = patch_binaries()
        long_secret_like = "SECRET_KEY=abcd1234\n" + ("x" * 5000)
        with isfile, access, patch(
            "ops.td02c_nginx_config_check.subprocess.run",
            return_value=completed([NGINX_BINARY, "-t"], 1, long_secret_like),
        ):
            result = check()
        self.assertLessEqual(len(result.detail), 200)
        self.assertNotIn("\n", result.detail)

    # cobertura de clasificaciones: nunca se emite algo fuera del set cerrado
    def test_classification_always_in_closed_set(self):
        isfile, access = patch_binaries()
        with isfile, access, patch(
            "ops.td02c_nginx_config_check.subprocess.run",
            return_value=completed([NGINX_BINARY, "-t"], 0, ""),
        ):
            result = check()
        self.assertIn(result.classification, CLASSIFICATIONS)


class NginxConfigCheckSecurityTests(unittest.TestCase):
    """Structural proofs that the helper cannot be used for anything beyond
    the one fixed `nginx -t` command, matching the sudoers contract."""

    def setUp(self):
        import ops.td02c_nginx_config_check as module
        self.source = inspect.getsource(module)

    def test_never_uses_shell(self):
        self.assertNotIn("shell=True", self.source)

    def test_subprocess_calls_use_fixed_argv_lists_only(self):
        # Every _run call site passes a literal list, never a str or user input.
        self.assertNotIn("_run(sys.argv", self.source)
        self.assertNotIn("os.system", self.source)
        self.assertNotIn("os.popen", self.source)

    def test_sudo_invocation_never_includes_extra_flags(self):
        self.assertIn(f"[SUDO_BINARY, \"-n\", NGINX_BINARY, \"-t\"]", self.source)
        self.assertNotIn("-T", self.source.split("SUDOERS_CONTENT")[0])  # no -T anywhere before the sudoers doc string

    def test_env_is_fixed_and_minimal(self):
        self.assertEqual(MINIMAL_ENV, {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
        self.assertNotIn("os.environ", self.source.replace("# ", ""))

    def test_sudoers_content_permits_exactly_one_command(self):
        self.assertEqual(SUDOERS_FILENAME, "td02c-nginx-check")
        lines = [line for line in SUDOERS_CONTENT.splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "app ALL=(root) NOPASSWD: NOEXEC: /usr/sbin/nginx -t")
        self.assertNotIn("ALL=(ALL)", SUDOERS_CONTENT)
        self.assertNotIn("-T", SUDOERS_CONTENT)
        self.assertNotIn("-s", SUDOERS_CONTENT)
        self.assertNotIn("*", SUDOERS_CONTENT)

    def test_helper_never_reads_certificate_paths(self):
        self.assertNotIn("letsencrypt", self.source.lower())
        self.assertNotIn(".pem", self.source)

    def test_helper_module_has_no_reload_or_restart_capability(self):
        for forbidden in ("reload", "restart", "-s ", "systemctl"):
            self.assertNotIn(forbidden, self.source)


if __name__ == "__main__":
    unittest.main()
