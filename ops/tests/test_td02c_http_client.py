from __future__ import annotations

import os
import io
import json
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from ops.deployment_hardening import DeploymentError
from ops.td02c_http_client import (
    AuthenticatedGetFailure,
    ResponseMetadata,
    SafeDiagnosticLog,
    classify_canary_response,
    discover_canary_target,
    run_authenticated_get_gate,
    safe_curl_argv,
    sanitized_command,
    sanitize_location,
    secure_cookie_workspace,
    write_post_curl_config,
)


class FakeAuthenticatedOperations:
    def __init__(self, **changes):
        self.login = metadata(method="GET", path="/admin/login/", status=200, content_type="text/html")
        self.auth = metadata(method="POST", path="/admin/login/", status=200, content_type="text/html")
        self.get = metadata(method="GET", path="/api/bulk-sends/", status=200)
        self.cookies = {"sessionid", "csrftoken"}
        self.fail_at = ""
        self.exit_code = 1
        for key, value in changes.items():
            setattr(self, key, value)

    def discover_target(self):
        if self.fail_at == "discover":
            raise RuntimeError("private detail")
        return discover_canary_target(nginx_config("canary.example.com"), "/run/django.sock")

    def prepare_tls(self, target):
        if self.fail_at == "tls":
            raise AuthenticatedGetFailure("tls_failed", exit_code=self.exit_code)

    def get_login(self, workspace, target):
        if self.fail_at == "login":
            raise AuthenticatedGetFailure("login_page_failed", exit_code=self.exit_code)
        return self.login

    def authenticate(self, workspace, target):
        if self.fail_at == "authenticate":
            raise AuthenticatedGetFailure("authentication_failed", exit_code=self.exit_code)
        return self.auth

    def has_cookie(self, workspace, name):
        return name in self.cookies

    def authenticated_get(self, workspace, target):
        if self.fail_at == "get":
            raise AuthenticatedGetFailure("authenticated_get_failed", exit_code=self.exit_code)
        return self.get


class GateHarness:
    def __init__(self, operations, *, cleanup_fails=False):
        self.stream = io.StringIO()
        self.log = SafeDiagnosticLog(self.stream)
        self.operations = operations
        self.cleanup_fails = cleanup_fails
        self.workspace = None

    @contextmanager
    def workspace_factory(self):
        with secure_cookie_workspace() as directory:
            self.workspace = directory
            yield directory
            if self.cleanup_fails:
                raise OSError("sensitive cleanup detail")

    def run(self):
        return run_authenticated_get_gate(
            self.operations, self.log, workspace_factory=self.workspace_factory
        )

    def json_lines(self):
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]


def nginx_config(*server_names: str, socket: str = "/run/django.sock") -> str:
    return "\n".join(
        f"""
        server {{
            listen 443 ssl;
            server_name {server_name};
            ssl_certificate /etc/ssl/cert.pem;
            location / {{ proxy_pass http://unix:{socket}; }}
        }}
        """
        for server_name in server_names
    )


def metadata(**changes) -> ResponseMetadata:
    values = {
        "method": "POST",
        "path": "/api/bulk-sends/",
        "status": 201,
        "content_type": "application/json; charset=utf-8",
        "location": "",
        "redirects": 0,
        "duration_seconds": 0.1,
    }
    values.update(changes)
    return ResponseMetadata(**values)


class Td02cHttpClientTests(unittest.TestCase):
    def test_unique_valid_hostname_is_discovered(self):
        target = discover_canary_target(
            nginx_config("canary.example.com"), "/run/django.sock"
        )
        self.assertEqual(target.server_name, "canary.example.com")

    def test_empty_hostname_is_rejected(self):
        with self.assertRaises(DeploymentError):
            discover_canary_target(nginx_config("_"), "/run/django.sock")

    def test_ambiguous_hostname_is_rejected(self):
        with self.assertRaises(DeploymentError):
            discover_canary_target(
                nginx_config("one.example.com", "two.example.com"),
                "/run/django.sock",
            )

    def test_wildcard_hostname_is_rejected(self):
        with self.assertRaises(DeploymentError):
            discover_canary_target(
                nginx_config("*.example.com"), "/run/django.sock"
            )

    def test_nginx_variable_hostname_is_rejected(self):
        with self.assertRaises(DeploymentError):
            discover_canary_target(
                nginx_config("$host"), "/run/django.sock"
            )

    def test_hostname_different_from_validated_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            discover_canary_target(
                nginx_config("canary.example.com"),
                "/run/django.sock",
                asserted_hostname="other.example.com",
            )

    def test_initial_json_201_is_expected(self):
        self.assertTrue(classify_canary_response(metadata()).allowed)

    def test_idempotent_json_200_is_expected_only_for_retry(self):
        response = metadata(status=200)
        self.assertTrue(classify_canary_response(response, idempotent_retry=True).allowed)
        self.assertFalse(classify_canary_response(response).allowed)

    def test_html_response_is_rejected(self):
        result = classify_canary_response(metadata(status=403, content_type="text/html"))
        self.assertEqual(result.code, "unexpected_content_type")

    def test_redirect_is_not_hidden(self):
        direct = classify_canary_response(metadata(status=302, location="/admin/login/"))
        followed = classify_canary_response(metadata(status=200, redirects=1))
        self.assertEqual(direct.code, "redirect_response")
        self.assertEqual(followed.code, "redirect_followed")

    def test_location_drops_query_fragment_and_credentials(self):
        value = "https://user:secret@example.com/login/?next=/private#token"
        self.assertEqual(sanitize_location(value), "https://example.com/login/")

    def test_cookie_jar_is_0600_and_workspace_is_removed(self):
        with secure_cookie_workspace() as directory:
            jar = directory / "cookies.txt"
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(jar.stat().st_mode), 0o600)
        self.assertFalse(directory.exists())

    def test_sensitive_values_stay_out_of_curl_argv(self):
        with secure_cookie_workspace() as directory:
            csv_path = directory / "canary.csv"
            csv_path.write_text("email\nsynthetic@example.invalid\n", encoding="utf-8")
            target = discover_canary_target(
                nginx_config("canary.example.com"), "/run/django.sock"
            )
            config = write_post_curl_config(
                directory,
                target=target,
                csrf_token="secret-csrf-token",
                csv_path=csv_path,
            )
            argv = safe_curl_argv(config)
            joined = " ".join(argv)
            self.assertNotIn("secret-csrf-token", joined)
            self.assertNotIn("--location", argv)
            self.assertNotIn("-k", argv)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)

    def test_discovered_hostname_propagates_to_all_http_fields(self):
        with secure_cookie_workspace() as directory:
            csv_path = directory / "canary.csv"
            csv_path.write_text("email\nsynthetic@example.invalid\n", encoding="utf-8")
            target = discover_canary_target(
                nginx_config("canary.example.com"), "/run/django.sock"
            )
            config = write_post_curl_config(
                directory,
                target=target,
                csrf_token="csrf-token",
                csv_path=csv_path,
            ).read_text(encoding="utf-8")

            self.assertIn('url = "https://canary.example.com/api/bulk-sends/"', config)
            self.assertIn('resolve = "canary.example.com:443:127.0.0.1"', config)
            self.assertIn('header = "Host: canary.example.com"', config)
            self.assertIn('header = "Origin: https://canary.example.com"', config)
            self.assertIn('header = "Referer: https://canary.example.com/app/"', config)

    def test_cookie_jar_cleanup_occurs_on_exception(self):
        with self.assertRaisesRegex(RuntimeError, "stop"):
            with secure_cookie_workspace() as directory:
                raise RuntimeError("stop")
        self.assertFalse(directory.exists())


class AuthenticatedGetDiagnosticTests(unittest.TestCase):
    def assert_failure(self, operations, expected, *, exit_code=1):
        harness = GateHarness(operations)
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            harness.run()
        self.assertEqual(caught.exception.error_class, expected)
        self.assertEqual(caught.exception.exit_code, exit_code)
        self.assertFalse(harness.workspace.exists())
        self.assertEqual(harness.json_lines()[-1]["stage"], "temporaries_removed")
        self.assertEqual(harness.json_lines()[-1]["result"], "PASS")
        return harness

    def test_success_records_every_required_stage_and_http_metadata(self):
        harness = GateHarness(FakeAuthenticatedOperations())
        harness.run()
        records = harness.json_lines()
        stages = [record["stage"] for record in records]
        self.assertEqual(
            stages,
            [
                "workspace_created",
                "cookie_jar_protected",
                "vhost_discovered",
                "tls_and_local_resolution_prepared",
                "login_get",
                "authentication",
                "sessionid_present",
                "csrftoken_present",
                "authenticated_get",
                "response_classified",
                "temporaries_removed",
            ],
        )
        self.assertTrue(all(record["result"] == "PASS" for record in records))
        http = next(record["http"] for record in records if record["stage"] == "authenticated_get")
        command = next(record["command"] for record in records if record["stage"] == "authenticated_get")
        self.assertEqual(http["method"], "GET")
        self.assertEqual(http["path"], "/api/bulk-sends/")
        self.assertEqual(http["status"], 200)
        self.assertEqual(http["content_type"], "application/json; charset=utf-8")
        self.assertEqual(http["redirects"], 0)
        self.assertEqual(http["ssl_verify_result"], 0)
        self.assertEqual(http["time_total"], 0.1)
        self.assertIn("--cookie <redacted>", command)
        self.assertNotIn("sessionid", command)
        self.assertNotIn("csrftoken", command)

    def test_bad_credentials_are_classified(self):
        self.assert_failure(FakeAuthenticatedOperations(fail_at="authenticate", exit_code=22), "authentication_failed", exit_code=22)

    def test_session_workspace_creation_failure_is_classified(self):
        @contextmanager
        def broken_workspace():
            raise OSError("private filesystem detail")
            yield  # pragma: no cover

        harness = GateHarness(FakeAuthenticatedOperations())
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            run_authenticated_get_gate(
                harness.operations, harness.log, workspace_factory=broken_workspace
            )
        self.assertEqual(caught.exception.error_class, "session_creation_failed")
        self.assertEqual(harness.json_lines()[0]["stage"], "workspace_created")
        self.assertEqual(harness.json_lines()[0]["result"], "FAIL")
        self.assertEqual(harness.json_lines()[-1]["stage"], "temporaries_removed")

    def test_symlink_workspace_is_rejected(self):
        if os.name == "nt":
            self.skipTest("POSIX symlink validation")
        with tempfile.TemporaryDirectory() as root:
            real = os.path.join(root, "real")
            link = os.path.join(root, "link")
            os.mkdir(real, 0o700)
            jar = os.path.join(real, "cookies.txt")
            open(jar, "w", encoding="utf-8").close()
            os.chmod(jar, 0o600)
            os.symlink(real, link)

            @contextmanager
            def symlink_workspace():
                yield Path(link)

            harness = GateHarness(FakeAuthenticatedOperations())
            with self.assertRaises(AuthenticatedGetFailure) as caught:
                run_authenticated_get_gate(
                    harness.operations,
                    harness.log,
                    workspace_factory=symlink_workspace,
                )
            self.assertEqual(
                caught.exception.error_class, "session_creation_failed"
            )

    def test_login_page_failure_is_classified(self):
        self.assert_failure(
            FakeAuthenticatedOperations(fail_at="login", exit_code=28),
            "login_page_failed",
            exit_code=28,
        )

    def test_authentication_redirect_is_not_followed(self):
        response = metadata(
            method="POST",
            status=302,
            content_type="text/html",
            location="https://canary.example.com/admin/login/",
        )
        self.assert_failure(
            FakeAuthenticatedOperations(auth=response), "authentication_failed"
        )

    def test_expired_session_redirect_is_authentication_failure(self):
        response = metadata(method="GET", status=302, content_type="text/html", location="https://canary.example.com/admin/login/?next=/api/private")
        harness = self.assert_failure(FakeAuthenticatedOperations(get=response), "authentication_failed")
        record = next(item for item in harness.json_lines() if item["stage"] == "authenticated_get")
        self.assertEqual(record["http"]["location"], "https://canary.example.com/admin/login/")
        self.assertEqual(record["http"]["redirects"], 0)

    def test_sessionid_missing(self):
        self.assert_failure(FakeAuthenticatedOperations(cookies={"csrftoken"}), "session_cookie_missing")

    def test_csrftoken_missing(self):
        self.assert_failure(FakeAuthenticatedOperations(cookies={"sessionid"}), "csrf_cookie_missing")

    def test_tls_failure_preserves_stage_and_exit_code(self):
        harness = self.assert_failure(FakeAuthenticatedOperations(fail_at="tls", exit_code=60), "tls_failed", exit_code=60)
        failed = next(item for item in harness.json_lines() if item["result"] == "FAIL")
        self.assertEqual(failed["stage"], "tls_and_local_resolution_prepared")
        self.assertEqual(failed["exit_code"], 60)

    def test_connection_failure(self):
        response = metadata(method="GET", status=0, content_type="", ssl_verify_result=0)
        self.assert_failure(FakeAuthenticatedOperations(get=response), "connection_failed")

    def test_http_403_and_500_are_unexpected_status(self):
        for status in (403, 500):
            with self.subTest(status=status):
                self.assert_failure(FakeAuthenticatedOperations(get=metadata(method="GET", status=status)), "unexpected_status")

    def test_unexpected_content_type_and_html_are_rejected(self):
        for content_type in ("text/plain", "text/html"):
            with self.subTest(content_type=content_type):
                self.assert_failure(FakeAuthenticatedOperations(get=metadata(method="GET", status=200, content_type=content_type)), "unexpected_content_type")

    def test_authenticated_get_execution_failure(self):
        self.assert_failure(FakeAuthenticatedOperations(fail_at="get", exit_code=7), "authenticated_get_failed", exit_code=7)

    def test_cleanup_failure_is_fail_closed_and_sanitized(self):
        harness = GateHarness(FakeAuthenticatedOperations(), cleanup_fails=True)
        with self.assertRaises(AuthenticatedGetFailure) as caught:
            harness.run()
        self.assertEqual(caught.exception.error_class, "cleanup_failed")
        self.assertNotIn("sensitive cleanup detail", harness.stream.getvalue())
        self.assertFalse(harness.workspace.exists())

    def test_secrets_and_bodies_never_enter_diagnostics(self):
        operation = FakeAuthenticatedOperations()
        operation.secret = "password=top-secret csrftoken=private sessionid=private"
        harness = GateHarness(operation)
        harness.run()
        output = harness.stream.getvalue()
        for forbidden in ("top-secret", "csrftoken=", "sessionid=", "password="):
            self.assertNotIn(forbidden, output)

    def test_safe_command_is_prebuilt_without_secret_values(self):
        command = sanitized_command("GET", "/api/bulk-sends/", "canary.example.com")
        self.assertIn("--cookie <redacted>", command)
        self.assertIn("--no-location", command)
        self.assertNotIn("sessionid", command)
        self.assertNotIn("csrftoken", command)
