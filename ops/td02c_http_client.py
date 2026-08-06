"""Safe HTTP metadata and temporary-artifact helpers for the TD-02C canary.

This module deliberately does not perform a canary request.  It keeps response
classification and secret-bearing curl artifacts deterministic and testable.
"""

from __future__ import annotations

import os
import json
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ContextManager, Iterator, Protocol, TextIO
from urllib.parse import urlsplit, urlunsplit

from ops.deployment_hardening import NginxTarget, discover_nginx_target


def discover_canary_target(
    nginx_config: str,
    bind_path: str,
    *,
    asserted_hostname: str | None = None,
) -> NginxTarget:
    """Return the unique target selected by the deployment preflight rules."""
    target = discover_nginx_target(nginx_config, bind_path)
    if asserted_hostname is not None and asserted_hostname != target.server_name:
        raise ValueError("asserted hostname does not match validated Nginx target")
    return target


@dataclass(frozen=True)
class ResponseMetadata:
    method: str
    path: str
    status: int
    content_type: str
    location: str
    redirects: int
    duration_seconds: float
    ssl_verify_result: int = 0


@dataclass(frozen=True)
class ResponseDecision:
    allowed: bool
    code: str


ERROR_CODES = {
    "session_creation_failed",
    "login_page_failed",
    "authentication_failed",
    "session_cookie_missing",
    "csrf_cookie_missing",
    "tls_failed",
    "connection_failed",
    "redirect_detected",
    "unexpected_status",
    "unexpected_content_type",
    "authenticated_get_failed",
    "cleanup_failed",
    "unknown_failure",
    # Nginx-discovery diagnostic taxonomy (see CurlOperations.discover_target).
    "systemctl_failed",
    "service_metadata_invalid",
    "working_directory_invalid",
    "nginx_test_failed",
    "nginx_dump_failed",
    "nginx_output_empty",
    "nginx_output_unparseable",
    "no_vhost_found",
    "multiple_vhosts_found",
    "wildcard_vhost_rejected",
    "variable_vhost_rejected",
    "certificate_not_found",
    "certificate_hostname_mismatch",
    "local_resolution_invalid",
    "command_permission_denied",
    "command_not_found",
    "subprocess_failed",
    "unexpected_discovery_error",
}


@dataclass(frozen=True)
class StageDiagnostic:
    stage: str
    result: str
    exit_code: int
    duration_seconds: float
    error_class: str = ""
    http: ResponseMetadata | None = None
    command: str = ""
    detail: str = ""

    def as_safe_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "phase": "authenticated_get",
            "substage": self.stage,
            "stage": self.stage,
            "result": self.result,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 6),
            "classification": self.error_class,
            "error_class": self.error_class,
        }
        if self.http is not None:
            value["http"] = {
                "method": self.http.method,
                "path": self.http.path,
                "status": self.http.status,
                "content_type": self.http.content_type,
                "location": sanitize_location(self.http.location),
                "redirects": self.http.redirects,
                "ssl_verify_result": self.http.ssl_verify_result,
                "time_total": round(self.http.duration_seconds, 6),
            }
        if self.command:
            value["command"] = self.command
        if self.detail:
            value["detail"] = self.detail
        return value


class AuthenticatedGetFailure(RuntimeError):
    def __init__(self, error_class: str, *, exit_code: int = 1):
        if error_class not in ERROR_CODES:
            error_class = "unknown_failure"
        super().__init__(error_class)
        self.error_class = error_class
        self.exit_code = exit_code or 1


class AuthenticatedGetOperations(Protocol):
    def discover_target(self) -> NginxTarget: ...
    def prepare_tls(self, target: NginxTarget) -> None: ...
    def get_login(self, workspace: Path, target: NginxTarget) -> ResponseMetadata: ...
    def authenticate(self, workspace: Path, target: NginxTarget) -> ResponseMetadata: ...
    def has_cookie(self, workspace: Path, name: str) -> bool: ...
    def authenticated_get(
        self, workspace: Path, target: NginxTarget
    ) -> ResponseMetadata: ...


class SafeDiagnosticLog:
    """Emit one JSON object per stage, with an intentionally small schema."""

    def __init__(self, stream: TextIO):
        self.stream = stream
        self.records: list[StageDiagnostic] = []

    def emit(self, record: StageDiagnostic) -> None:
        self.records.append(record)
        self.stream.write(json.dumps(record.as_safe_dict(), sort_keys=True) + "\n")
        self.stream.flush()


def sanitized_command(method: str, path: str, host: str) -> str:
    """Return the prebuilt safe command representation; never accept secrets."""
    if method not in {"GET", "POST"} or not path.startswith("/"):
        raise ValueError("invalid safe command metadata")
    return (
        f"curl --request {method} https://{host}{path} "
        f"--resolve {host}:443:127.0.0.1 --cookie <redacted> "
        "--header 'Host: <validated-vhost>' --no-location"
    )


def _response_error(metadata: ResponseMetadata, *, authenticated: bool) -> str:
    content_type = metadata.content_type.partition(";")[0].strip().lower()
    if metadata.ssl_verify_result != 0:
        return "tls_failed"
    if metadata.status == 0:
        return "connection_failed"
    if metadata.redirects or metadata.status in {301, 302, 303, 307, 308}:
        location = sanitize_location(metadata.location)
        if authenticated and ("login" in location or metadata.status == 302):
            return "authentication_failed"
        return "redirect_detected"
    if metadata.status != 200:
        return "unexpected_status"
    allowed_content_types = (
        ({"text/html"} if metadata.path == "/app/" else {"application/json"})
        if authenticated
        else {"text/html", "application/json"}
    )
    if content_type not in allowed_content_types:
        return "unexpected_content_type"
    return ""


def _login_post_error(metadata: ResponseMetadata) -> str:
    """Accept only Django's explicit, unfollowed successful login redirect."""
    if metadata.ssl_verify_result != 0:
        return "tls_failed"
    if metadata.status == 0:
        return "connection_failed"
    if metadata.redirects:
        return "redirect_detected"
    location = urlsplit(sanitize_location(metadata.location)).path
    if metadata.status == 302 and location == "/app/":
        return ""
    return "authentication_failed"


def run_authenticated_get_gate(
    operations: AuthenticatedGetOperations,
    log: SafeDiagnosticLog,
    *,
    workspace_factory: Callable[[], ContextManager[Path]] | None = None,
) -> None:
    """Run the read-only authenticated GET gate with fail-closed diagnostics.

    Secret-bearing operations remain injected; this coordinator records only
    fixed stage names and allow-listed HTTP metadata.
    """
    workspace: Path | None = None
    manager = None
    manager_entered = False

    def stage(name: str, action: Callable[[], object], error: str) -> object:
        started = time.monotonic()
        try:
            result = action()
        except AuthenticatedGetFailure as exc:
            log.emit(StageDiagnostic(name, "FAIL", exc.exit_code, time.monotonic() - started, exc.error_class))
            raise
        except Exception as exc:
            failure = AuthenticatedGetFailure(error, exit_code=getattr(exc, "returncode", 1))
            log.emit(StageDiagnostic(name, "FAIL", failure.exit_code, time.monotonic() - started, failure.error_class))
            raise failure from None
        log.emit(StageDiagnostic(name, "PASS", 0, time.monotonic() - started))
        return result

    try:
        manager = (workspace_factory or secure_cookie_workspace)()
        workspace = stage("workspace_created", lambda: manager.__enter__(), "session_creation_failed")  # type: ignore[assignment]
        manager_entered = True
        stage("cookie_jar_created", lambda: _validate_workspace(workspace), "session_creation_failed")
        target = stage("nginx_target_discovered", operations.discover_target, "connection_failed")
        stage("tls_resolution_prepared", lambda: operations.prepare_tls(target), "tls_failed")  # type: ignore[arg-type]
        login = _http_stage("login_page_loaded", lambda: operations.get_login(workspace, target), log, authenticated=False, fallback="login_page_failed", command=sanitized_command("GET", "/admin/login/", target.server_name))  # type: ignore[arg-type,union-attr]
        if _response_error(login, authenticated=False):
            raise AuthenticatedGetFailure(_response_error(login, authenticated=False))
        auth = _http_stage("login_post_completed", lambda: operations.authenticate(workspace, target), log, authenticated=False, fallback="authentication_failed", command=sanitized_command("POST", "/admin/login/", target.server_name), classifier=_login_post_error)  # type: ignore[arg-type,union-attr]
        error = _login_post_error(auth)
        if error:
            raise AuthenticatedGetFailure("authentication_failed" if error == "redirect_detected" else error)
        stage("authentication_confirmed", lambda: _require_cookie(operations, workspace, "sessionid", "session_cookie_missing"), "session_cookie_missing")
        stage("csrf_cookie_present", lambda: _require_cookie(operations, workspace, "csrftoken", "csrf_cookie_missing"), "csrf_cookie_missing")
        response = _http_stage("authenticated_get_completed", lambda: operations.authenticated_get(workspace, target), log, authenticated=True, fallback="authenticated_get_failed", command=sanitized_command("GET", "/app/", target.server_name))  # type: ignore[arg-type,union-attr]
        error = _response_error(response, authenticated=True)
        if error:
            log.emit(StageDiagnostic("response_classified", "FAIL", 1, 0.0, error, response))
            raise AuthenticatedGetFailure(error)
        log.emit(StageDiagnostic("response_classified", "PASS", 0, 0.0, http=response))
    except AuthenticatedGetFailure:
        raise
    except Exception as exc:
        log.emit(StageDiagnostic("unknown", "FAIL", getattr(exc, "returncode", 1) or 1, 0.0, "unknown_failure"))
        raise AuthenticatedGetFailure("unknown_failure") from None
    finally:
        started = time.monotonic()
        if manager is not None and manager_entered:
            try:
                manager.__exit__(None, None, None)
                log.emit(StageDiagnostic("temporary_files_cleanup_completed", "PASS", 0, time.monotonic() - started))
            except Exception:
                log.emit(StageDiagnostic("temporary_files_cleanup_completed", "FAIL", 1, time.monotonic() - started, "cleanup_failed"))
                raise AuthenticatedGetFailure("cleanup_failed") from None
        elif manager is not None:
            log.emit(StageDiagnostic("temporary_files_cleanup_completed", "PASS", 0, time.monotonic() - started))


def _http_stage(
    name: str,
    action: Callable[[], ResponseMetadata],
    log: SafeDiagnosticLog,
    *,
    authenticated: bool,
    fallback: str,
    command: str,
    classifier: Callable[[ResponseMetadata], str] | None = None,
) -> ResponseMetadata:
    started = time.monotonic()
    try:
        metadata = action()
    except AuthenticatedGetFailure as exc:
        log.emit(StageDiagnostic(name, "FAIL", exc.exit_code, time.monotonic() - started, exc.error_class, command=command))
        raise
    except Exception as exc:
        error = AuthenticatedGetFailure(fallback, exit_code=getattr(exc, "returncode", 1))
        log.emit(StageDiagnostic(name, "FAIL", error.exit_code, time.monotonic() - started, error.error_class, command=command))
        raise error from None
    error = (classifier or (lambda item: _response_error(item, authenticated=authenticated)))(metadata)
    log.emit(StageDiagnostic(name, "PASS" if not error else "FAIL", 0 if not error else 1, time.monotonic() - started, error, metadata, command))
    return metadata


def _validate_workspace(workspace: Path) -> None:
    cookie_jar = workspace / "cookies.txt"
    if (
        workspace.is_symlink()
        or cookie_jar.is_symlink()
        or not workspace.is_dir()
        or not cookie_jar.is_file()
    ):
        raise AuthenticatedGetFailure("session_creation_failed")
    if os.name != "nt":
        if stat.S_IMODE(workspace.stat().st_mode) != 0o700:
            raise AuthenticatedGetFailure("session_creation_failed")
        if stat.S_IMODE(cookie_jar.stat().st_mode) != 0o600:
            raise AuthenticatedGetFailure("session_creation_failed")


def _require_cookie(
    operations: AuthenticatedGetOperations, workspace: Path, name: str, error: str
) -> None:
    if not operations.has_cookie(workspace, name):
        raise AuthenticatedGetFailure(error)


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
    target: NginxTarget,
    csrf_token: str,
    csv_path: Path,
    client_request_id: str,
    template_id: str,
    template_name: str,
    subject: str,
) -> Path:
    """Write secret-bearing curl options to a mode-0600 file, never argv."""
    cookie_jar = directory / "cookies.txt"
    if not cookie_jar.is_file() or (
        os.name != "nt" and stat.S_IMODE(cookie_jar.stat().st_mode) != 0o600
    ):
        raise ValueError("cookie jar must exist with mode 0600")
    if not csrf_token:
        raise ValueError("csrf token is required")
    host = target.server_name
    port = target.port
    origin = f"https://{host}" if port == 443 else f"https://{host}:{port}"
    url = f"{origin}/api/bulk-sends/"
    referer = f"{origin}/app/"
    config = directory / "post.curl.conf"
    lines = [
        f'url = "{url}"',
        f'resolve = "{host}:{port}:127.0.0.1"',
        f'cookie = "{cookie_jar}"',
        f'cookie-jar = "{cookie_jar}"',
        f'header = "Host: {host}"',
        f'header = "Origin: {origin}"',
        f'header = "Referer: {referer}"',
        f'header = "X-CSRFToken: {csrf_token}"',
        'header = "Accept: application/json"',
        'request = "POST"',
        'connect-timeout = 3',
        'max-time = 30',
        'output = "/dev/null"',
        'silent',
        'show-error',
        'form = "engine_version=v2"',
        f'form = "client_request_id={client_request_id}"',
        f'form = "template_id={template_id}"',
        f'form = "template_name={template_name}"',
        f'form = "subject={subject}"',
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
