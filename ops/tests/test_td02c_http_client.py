from __future__ import annotations

import os
import stat
import unittest

from ops.td02c_http_client import (
    ResponseMetadata,
    classify_canary_response,
    safe_curl_argv,
    sanitize_location,
    secure_cookie_workspace,
    write_post_curl_config,
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
            config = write_post_curl_config(
                directory, csrf_token="secret-csrf-token", csv_path=csv_path
            )
            argv = safe_curl_argv(config)
            joined = " ".join(argv)
            self.assertNotIn("secret-csrf-token", joined)
            self.assertNotIn("--location", argv)
            self.assertNotIn("-k", argv)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)

    def test_cookie_jar_cleanup_occurs_on_exception(self):
        with self.assertRaisesRegex(RuntimeError, "stop"):
            with secure_cookie_workspace() as directory:
                raise RuntimeError("stop")
        self.assertFalse(directory.exists())
