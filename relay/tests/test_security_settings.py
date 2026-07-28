import json
import os
import subprocess
import sys
from pathlib import Path

from django.test import RequestFactory, SimpleTestCase, override_settings


BASE_DIR = Path(__file__).resolve().parents[2]
SECURITY_ENV_NAMES = {
    "SECRET_KEY",
    "SECURE_PROXY_SSL_HEADER_ENABLED",
    "SECURE_SSL_REDIRECT",
    "SESSION_COOKIE_SECURE",
    "CSRF_COOKIE_SECURE",
    "SECURE_HSTS_SECONDS",
    "SECURE_HSTS_INCLUDE_SUBDOMAINS",
    "SECURE_HSTS_PRELOAD",
}


class SecuritySettingsTests(SimpleTestCase):
    def run_settings(self, **environment):
        process_environment = os.environ.copy()
        for name in SECURITY_ENV_NAMES:
            process_environment.pop(name, None)
        process_environment.update(environment)

        script = """
import json
import runpy
import environ

environ.Env.read_env = lambda *args, **kwargs: None
settings = runpy.run_path("config/settings.py")
names = [
    "SECURE_PROXY_SSL_HEADER",
    "SECURE_SSL_REDIRECT",
    "SESSION_COOKIE_SECURE",
    "CSRF_COOKIE_SECURE",
    "SECURE_HSTS_SECONDS",
    "SECURE_HSTS_INCLUDE_SUBDOMAINS",
    "SECURE_HSTS_PRELOAD",
]
print(json.dumps({name: settings[name] for name in names}))
"""
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=BASE_DIR,
            env=process_environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_secret_key_is_required(self):
        result = self.run_settings()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Set the SECRET_KEY environment variable", result.stderr)

    def test_secret_key_cannot_be_empty(self):
        result = self.run_settings(SECRET_KEY="")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SECRET_KEY must not be empty", result.stderr)

    def test_local_http_security_defaults_remain_disabled(self):
        result = self.run_settings(SECRET_KEY="test-only-not-for-production")

        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        self.assertIsNone(values["SECURE_PROXY_SSL_HEADER"])
        self.assertFalse(values["SECURE_SSL_REDIRECT"])
        self.assertFalse(values["SESSION_COOKIE_SECURE"])
        self.assertFalse(values["CSRF_COOKIE_SECURE"])
        self.assertEqual(values["SECURE_HSTS_SECONDS"], 0)
        self.assertFalse(values["SECURE_HSTS_INCLUDE_SUBDOMAINS"])
        self.assertFalse(values["SECURE_HSTS_PRELOAD"])

    def test_production_https_security_is_loaded_from_environment(self):
        result = self.run_settings(
            SECRET_KEY="test-only-not-for-production",
            SECURE_PROXY_SSL_HEADER_ENABLED="True",
            SECURE_SSL_REDIRECT="True",
            SESSION_COOKIE_SECURE="True",
            CSRF_COOKIE_SECURE="True",
            SECURE_HSTS_SECONDS="300",
            SECURE_HSTS_INCLUDE_SUBDOMAINS="False",
            SECURE_HSTS_PRELOAD="False",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        self.assertEqual(
            values["SECURE_PROXY_SSL_HEADER"],
            ["HTTP_X_FORWARDED_PROTO", "https"],
        )
        self.assertTrue(values["SECURE_SSL_REDIRECT"])
        self.assertTrue(values["SESSION_COOKIE_SECURE"])
        self.assertTrue(values["CSRF_COOKIE_SECURE"])
        self.assertEqual(values["SECURE_HSTS_SECONDS"], 300)
        self.assertFalse(values["SECURE_HSTS_INCLUDE_SUBDOMAINS"])
        self.assertFalse(values["SECURE_HSTS_PRELOAD"])

    @override_settings(
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https")
    )
    def test_forwarded_https_request_is_secure(self):
        request = RequestFactory().get(
            "/",
            HTTP_X_FORWARDED_PROTO="https",
        )

        self.assertTrue(request.is_secure())
