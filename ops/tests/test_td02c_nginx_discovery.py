from __future__ import annotations

import inspect
import json
import subprocess
import unittest
from unittest.mock import patch

from ops.td02c_nginx_discovery import (
    CLASSIFICATIONS,
    INSTALLED_PATH,
    MINIMAL_ENV,
    NGINX_BINARY,
    OPENSSL_BINARY,
    SCHEMA,
    SERVICE_UNIT,
    SUDOERS_CONTENT,
    SUDOERS_FILENAME,
    SYSTEMCTL_BINARY,
    TEST_BINARY,
    discover,
    main,
)

SAMPLE_EXEC_START = (
    "{ path=/opt/app/django-doppler-relay/.venv/bin/gunicorn ; "
    "argv[]=gunicorn config.wsgi --bind unix:/run/app.sock ; ignore_errors=no }"
)

SAMPLE_CONFIG_ONE_VHOST = """
server { listen 80 default_server; server_name _; }
server {
    listen 443 ssl;
    server_name app1.example.com;
    ssl_certificate /etc/letsencrypt/live/app1.example.com/fullchain.pem;
    location / { proxy_pass http://unix:/run/app.sock; }
}
"""

SAMPLE_CONFIG_NO_VHOST = """
server { listen 80 default_server; server_name _; }
"""

SAMPLE_CONFIG_MULTIPLE_VHOSTS = """
server { listen 443 ssl; server_name a.example.com; proxy_pass http://unix:/run/app.sock; }
server { listen 443 ssl; server_name b.example.com; proxy_pass http://unix:/run/app.sock; }
"""

SAMPLE_CONFIG_WILDCARD = """
server { listen 443 ssl; server_name *.example.com; proxy_pass http://unix:/run/app.sock; }
"""

SAMPLE_CONFIG_NO_CERT = """
server {
    listen 443 ssl;
    server_name app1.example.com;
    proxy_pass http://unix:/run/app.sock;
}
"""


def completed(argv, returncode=0, stdout=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, "")


def dispatch(*, systemctl_ok=True, nginx_t_ok=True, nginx_T_stdout=SAMPLE_CONFIG_ONE_VHOST,
             nginx_T_ok=True, socket_ok=True, openssl_ok=True, privkey_present=True):
    def side_effect(argv, **kwargs):
        if argv[0] == SYSTEMCTL_BINARY:
            return completed(argv, 0 if systemctl_ok else 1, SAMPLE_EXEC_START if systemctl_ok else "")
        if argv[0] == NGINX_BINARY and argv[1:] == ["-t"]:
            return completed(argv, 0 if nginx_t_ok else 1, "" if nginx_t_ok else "nginx: [emerg] boom")
        if argv[0] == NGINX_BINARY and argv[1:] == ["-T"]:
            return completed(argv, 0 if nginx_T_ok else 1, nginx_T_stdout if nginx_T_ok else "boom")
        if argv[0] == TEST_BINARY:
            return completed(argv, 0 if socket_ok else 1, "")
        if argv[0] == OPENSSL_BINARY:
            # openssl x509 -checkhost always exits 0, match or not (confirmed
            # against real openssl); the result lives only in stdout text.
            stdout = "Hostname x does match certificate" if openssl_ok else "Hostname x does NOT match certificate"
            return completed(argv, 0, stdout)
        raise AssertionError(f"unexpected command: {argv}")
    return side_effect


class NginxDiscoveryHelperTests(unittest.TestCase):
    """Real-logic coverage for the privileged helper's own discover()."""

    def _run_discover(self, *, is_root=True, is_file=lambda path: True, **dispatch_kwargs):
        with (
            patch("ops.td02c_nginx_discovery.os.geteuid", return_value=0 if is_root else 1000, create=True),
            patch("ops.td02c_nginx_discovery.subprocess.run", side_effect=dispatch(**dispatch_kwargs)),
            patch("ops.td02c_nginx_discovery.Path.is_file", is_file),
        ):
            return discover()

    # 1. exito completo: helper valido devuelve JSON minimo
    def test_full_success_returns_sanitized_minimal_fields(self):
        result = self._run_discover()
        self.assertEqual(result.classification, "nginx_discovery_privileged_passed")
        payload = result.as_dict()
        self.assertEqual(
            set(payload),
            {"schema", "phase", "classification", "result", "exit_code",
             "server_name", "port", "proxy_or_socket_target",
             "certificate_path", "certificate_key_path_present", "nginx_test_passed"},
        )
        self.assertEqual(payload["server_name"], "app1.example.com")
        self.assertEqual(payload["port"], 443)
        self.assertEqual(payload["proxy_or_socket_target"], "/run/app.sock")
        self.assertTrue(payload["certificate_key_path_present"])
        self.assertNotIn("dump", payload)
        self.assertNotIn("config", payload)
        self.assertNotIn("raw", payload)

    # 2. no root: rechazado sin ejecutar nada privilegiado
    def test_not_root_is_rejected(self):
        result = self._run_discover(is_root=False)
        self.assertEqual(result.classification, "nginx_discovery_unexpected_error")

    def test_systemctl_show_failure(self):
        result = self._run_discover(systemctl_ok=False)
        self.assertEqual(result.classification, "nginx_discovery_unexpected_error")

    def test_nginx_t_failure_is_config_test_failed(self):
        result = self._run_discover(nginx_t_ok=False)
        self.assertEqual(result.classification, "nginx_discovery_config_test_failed")

    def test_nginx_T_failure_is_config_test_failed(self):
        result = self._run_discover(nginx_T_ok=False)
        self.assertEqual(result.classification, "nginx_discovery_config_test_failed")

    # 3. multiples vhosts: FAIL
    def test_multiple_vhosts_rejected(self):
        result = self._run_discover(nginx_T_stdout=SAMPLE_CONFIG_MULTIPLE_VHOSTS)
        self.assertEqual(result.classification, "nginx_discovery_multiple_vhosts")

    # 4. cero candidatos: FAIL
    def test_no_vhost_rejected(self):
        result = self._run_discover(nginx_T_stdout=SAMPLE_CONFIG_NO_VHOST)
        self.assertEqual(result.classification, "nginx_discovery_no_vhost")

    # 5. wildcard/variable: FAIL
    def test_wildcard_vhost_rejected(self):
        result = self._run_discover(nginx_T_stdout=SAMPLE_CONFIG_WILDCARD)
        self.assertEqual(result.classification, "nginx_discovery_no_vhost")

    # 6. socket inexistente: FAIL
    def test_socket_missing_rejected(self):
        result = self._run_discover(socket_ok=False)
        self.assertEqual(result.classification, "nginx_discovery_socket_missing")

    # 7. certificado incorrecto: FAIL
    def test_certificate_mismatch_rejected(self):
        result = self._run_discover(openssl_ok=False)
        self.assertEqual(result.classification, "nginx_discovery_certificate_mismatch")

    def test_no_certificate_directive_still_succeeds_with_null_fields(self):
        result = self._run_discover(nginx_T_stdout=SAMPLE_CONFIG_NO_CERT)
        self.assertEqual(result.classification, "nginx_discovery_privileged_passed")
        payload = result.as_dict()
        self.assertIsNone(payload["certificate_path"])
        self.assertFalse(payload["certificate_key_path_present"])

    def test_privkey_absent_reports_false(self):
        result = self._run_discover(is_file=lambda path: False)
        self.assertFalse(result.as_dict()["certificate_key_path_present"])

    # 8. cobertura de clasificaciones: nunca fuera del set cerrado
    def test_classification_always_in_closed_set(self):
        result = self._run_discover()
        self.assertIn(result.classification, CLASSIFICATIONS)

    def test_main_rejects_any_argument(self):
        stdout = []
        with patch("builtins.print", side_effect=lambda value: stdout.append(value)):
            code = main(["--anything"])
        self.assertEqual(code, 1)
        payload = json.loads(stdout[0])
        self.assertEqual(payload["classification"], "nginx_discovery_command_rejected")

    def test_main_success_returns_zero(self):
        stdout = []
        with (
            patch("ops.td02c_nginx_discovery.os.geteuid", return_value=0, create=True),
            patch("ops.td02c_nginx_discovery.subprocess.run", side_effect=dispatch()),
            patch("ops.td02c_nginx_discovery.Path.is_file", lambda path: True),
            patch("builtins.print", side_effect=lambda value: stdout.append(value)),
        ):
            code = main([])
        self.assertEqual(code, 0)
        payload = json.loads(stdout[0])
        self.assertEqual(payload["result"], "PASS")

    # 9. cero secretos: detalle sanitizado, una linea, corto
    def test_detail_is_sanitized_short_single_line(self):
        result = self._run_discover(nginx_t_ok=False)
        payload = result.as_dict()
        detail = payload.get("detail", "")
        self.assertLessEqual(len(detail), 200)
        self.assertNotIn("\n", detail)


class NginxDiscoveryHelperSecurityTests(unittest.TestCase):
    """Structural proofs about the helper source itself."""

    def setUp(self):
        import ops.td02c_nginx_discovery as module
        self.source = inspect.getsource(module)

    def test_never_uses_shell(self):
        self.assertNotIn("shell=True", self.source)

    def test_no_os_system_or_popen(self):
        self.assertNotIn("os.system", self.source)
        self.assertNotIn("os.popen", self.source)

    def test_env_is_fixed_and_minimal(self):
        self.assertEqual(MINIMAL_ENV, {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})

    def test_service_unit_is_hardcoded_not_an_argument(self):
        self.assertEqual(SERVICE_UNIT, "django.service")
        # argv is only ever inspected to reject it, never to select a unit
        self.assertIn("if argv:", self.source)

    def test_helper_never_reads_certificate_or_key_content(self):
        # only existence of privkey.pem is checked (.is_file()), never opened
        self.assertNotIn("privkey.pem\").read", self.source)
        self.assertNotIn("certificate).read", self.source)
        self.assertNotIn("open(", self.source)

    def test_no_reload_or_restart_capability(self):
        for forbidden in ("-s reload", "-s restart", "-s stop", "systemctl restart", "systemctl reload"):
            self.assertNotIn(forbidden, self.source)

    def test_no_write_capability(self):
        for forbidden in ("\"w\"", "'w'", "os.remove", "os.unlink", "shutil.copy"):
            self.assertNotIn(forbidden, self.source)

    def test_sudoers_content_permits_exactly_one_absolute_path_zero_args(self):
        self.assertEqual(SUDOERS_FILENAME, "td02c-nginx-discovery")
        lines = [line for line in SUDOERS_CONTENT.splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], f'app ALL=(root) NOPASSWD: {INSTALLED_PATH} ""')
        self.assertNotIn("ALL=(ALL)", SUDOERS_CONTENT)
        self.assertNotIn("*", SUDOERS_CONTENT)
        self.assertNotIn("python", SUDOERS_CONTENT.lower())

    def test_sudoers_rule_does_not_permit_arguments(self):
        # a bare command path in sudoers permits ANY arguments -- confirmed
        # for real: `sudo -n <path> --anything` succeeded until this
        # trailing `""` (sudoers' own "zero arguments" marker) was added.
        # Without it, argument rejection would rest solely on this script's
        # own argv check, a single mutable layer for what must be an
        # OS-enforced invariant.
        rule_line = next(
            line for line in SUDOERS_CONTENT.splitlines()
            if line and not line.startswith("#")
        )
        self.assertTrue(rule_line.endswith(f'{INSTALLED_PATH} ""'))

    def test_installed_path_is_outside_the_checkout(self):
        self.assertTrue(INSTALLED_PATH.startswith("/usr/local/"))
        self.assertNotIn("django-doppler-relay", INSTALLED_PATH)

    def test_schema_is_versioned(self):
        self.assertEqual(SCHEMA, "td02c.nginx-discovery/v1")


if __name__ == "__main__":
    unittest.main()
