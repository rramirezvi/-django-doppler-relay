from __future__ import annotations

import os
import stat
import unittest

from ops.deployment_hardening import DeploymentError
from ops.td02c_http_client import (
    ResponseMetadata,
    classify_canary_response,
    discover_canary_target,
    safe_curl_argv,
    sanitize_location,
    secure_cookie_workspace,
    write_post_curl_config,
)


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
