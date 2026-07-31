"""Safe HTTP metadata and temporary-artifact helpers for the TD-02C canary.

This module deliberately does not perform a canary request.  It keeps response
classification and secret-bearing curl artifacts deterministic and testable.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit, urlunsplit


CANARY_HOST = "app1.ramirezvi.com"
CANARY_URL = f"https://{CANARY_HOST}/api/bulk-sends/"
CANARY_ORIGIN = f"https://{CANARY_HOST}"
CANARY_REFERER = f"https://{CANARY_HOST}/app/"


@dataclass(frozen=True)
class ResponseMetadata:
    method: str
    path: str
    status: int
    content_type: str
    location: str
    redirects: int
    duration_seconds: float


@dataclass(frozen=True)
class ResponseDecision:
    allowed: bool
    code: str


def sanitize_location(value: str) -> str:
    """Keep only scheme, host and path; drop credentials, query and fragment."""
    if not value:
        return ""
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, hostname + port, parsed.path, "", ""))


def classify_canary_response(
    metadata: ResponseMetadata, *, idempotent_retry: bool = False
) -> ResponseDecision:
    content_type = metadata.content_type.partition(";")[0].strip().lower()
    if metadata.redirects:
        return ResponseDecision(False, "redirect_followed")
    if metadata.status in {301, 302, 303, 307, 308}:
        return ResponseDecision(False, "redirect_response")
    if content_type != "application/json":
        return ResponseDecision(False, "unexpected_content_type")
    expected = 200 if idempotent_retry else 201
    if metadata.status != expected:
        return ResponseDecision(False, "unexpected_status")
    return ResponseDecision(True, "response_expected")


@contextmanager
def secure_cookie_workspace(prefix: str = "td02c-http-") -> Iterator[Path]:
    """Yield a private workspace and remove cookies/configs on every exit."""
    directory = Path(tempfile.mkdtemp(prefix=prefix))
    os.chmod(directory, 0o700)
    cookie_jar = directory / "cookies.txt"
    cookie_jar.touch(mode=0o600)
    os.chmod(cookie_jar, 0o600)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=False)


def write_post_curl_config(
    directory: Path,
    *,
    csrf_token: str,
    csv_path: Path,
) -> Path:
    """Write secret-bearing curl options to a mode-0600 file, never argv."""
    cookie_jar = directory / "cookies.txt"
    if not cookie_jar.is_file() or (
        os.name != "nt" and stat.S_IMODE(cookie_jar.stat().st_mode) != 0o600
    ):
        raise ValueError("cookie jar must exist with mode 0600")
    if not csrf_token:
        raise ValueError("csrf token is required")
    config = directory / "post.curl.conf"
    lines = [
        f'url = "{CANARY_URL}"',
        f'resolve = "{CANARY_HOST}:443:127.0.0.1"',
        f'cookie = "{cookie_jar}"',
        f'cookie-jar = "{cookie_jar}"',
        f'header = "Host: {CANARY_HOST}"',
        f'header = "Origin: {CANARY_ORIGIN}"',
        f'header = "Referer: {CANARY_REFERER}"',
        f'header = "X-CSRFToken: {csrf_token}"',
        'header = "Accept: application/json"',
        'request = "POST"',
        'connect-timeout = 3',
        'max-time = 30',
        'output = "/dev/null"',
        'silent',
        'show-error',
        'form = "engine_version=v2"',
        'form = "client_request_id=td02c-canary-import-v1-20260731"',
        'form = "template_id=td02c-canary-import-only"',
        'form = "template_name=TD-02C Canary Import Only"',
        'form = "subject=TD-02C Canary Import Only"',
        'form = "send_now=False"',
        'form = "scheduled_at="',
        'form = "variables={\\\"name\\\":\\\"name\\\",\\\"amount\\\":\\\"amount\\\",\\\"code\\\":\\\"code\\\",\\\"note\\\":\\\"note\\\"}"',
        f'form = "recipients_file=@{csv_path};type=text/csv"',
    ]
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(config, 0o600)
    return config


def safe_curl_argv(config: Path) -> list[str]:
    """Return an argv without tokens, cookies, credentials or redirect flags."""
    return ["curl", "--config", str(config)]
