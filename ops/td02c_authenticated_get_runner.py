#!/usr/bin/env python3
"""Versioned, fail-closed runner for the TD-02C authenticated GET gate."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, TextIO

try:
    import pwd
except ImportError:  # pragma: no cover - operational runner is Linux-only
    pwd = None  # type: ignore[assignment]

from ops.deployment_hardening import (
    DeploymentError,
    Runner,
    application_bind_from_exec_start,
    discover_and_validate_nginx,
    discover_service,
    redact_output,
)
from ops.td02c_http_client import (
    AuthenticatedGetFailure,
    NginxTarget,
    ResponseMetadata,
    SafeDiagnosticLog,
    StageDiagnostic,
    run_authenticated_get_gate,
    sanitize_location,
)

EXPECTED_MODULE = "ops.td02c_authenticated_get_runner"


class RunnerFailure(RuntimeError):
    pass


def validate_module_entrypoint(service_unit: str) -> None:
    """Require package-module execution from the discovered service checkout."""
    if pwd is None:
        raise RunnerFailure("posix_required")
    if __package__ != "ops" or __spec__ is None or __spec__.name != EXPECTED_MODULE:
        raise RunnerFailure("module_entrypoint_required")

    service = discover_service(Runner(), service_unit)
    repository = service.working_directory.resolve(strict=True)
    current_directory = Path.cwd().resolve(strict=True)
    if current_directory != repository:
        raise RunnerFailure("working_directory_mismatch")
    if not (repository / "ops" / "td02c_authenticated_get_runner.py").is_file():
        raise RunnerFailure("runner_module_missing")

    import_roots = {
        Path(value or current_directory).resolve(strict=False) for value in sys.path
    }
    if repository not in import_roots:
        raise RunnerFailure("repository_not_importable")

    effective_user = pwd.getpwuid(os.geteuid()).pw_name
    if effective_user != service.user:
        raise RunnerFailure("effective_user_mismatch")


def resolve_authorized_user(user_id: int, username: str, *, lookup, can_operate_bulk_sends):
    """Pure, injectable authorization check, extracted from DjangoState so it
    is unit-testable with plain Python fakes.

    ops/tests/*.py must import cleanly under bare `python -m unittest
    discover` (no DJANGO_SETTINGS_MODULE, no configured apps) -- that is
    exactly how the production bootstrap gate runs the permitted suite.
    A module-level Django ORM import anywhere in this test file breaks
    that for every test in it, not just the ones exercising it, so the
    validation logic itself must not require Django to be importable to
    be tested.

    ``lookup(user_id, username)`` must return the matching user object or
    None (never raise) if no user has exactly that id and username.
    ``can_operate_bulk_sends`` is the real permission check from
    relay.services.operator_permissions; it alone decides which
    permission(s) satisfy the gate, never reimplemented here.
    """
    if not username or not username.strip():
        raise RunnerFailure("authorized_user_invalid")
    user = lookup(user_id, username)
    if user is None:
        raise RunnerFailure("authorized_user_invalid")
    if not user.is_active or not user.is_staff or not can_operate_bulk_sends(user):
        raise RunnerFailure("authorized_user_invalid")
    return user


@dataclass(frozen=True)
class Baseline:
    total_sessions: int
    user_sessions: frozenset[str]
    bulk_sends: int
    bulk_sends_v2: int
    recipients: int
    jobs: int
    messages: int


def emit(log: SafeDiagnosticLog, stage: str, result: str, started: float, classification: str = "") -> None:
    log.emit(StageDiagnostic(stage, result, 0 if result == "PASS" else 1, time.monotonic() - started, classification))


def emit_counts(stream: TextIO, label: str, value: Baseline) -> None:
    stream.write(json.dumps({
        "phase": "authenticated_get", "substage": label, "result": "PASS",
        "counts": {"django_session": value.total_sessions, "user_1_sessions": len(value.user_sessions),
                   "bulk_send": value.bulk_sends, "bulk_send_v2": value.bulk_sends_v2,
                   "bulk_send_recipient": value.recipients, "background_job": value.jobs,
                   "email_message": value.messages},
    }, sort_keys=True) + "\n")
    stream.flush()


def validate_credential_file(path: Path, repository: Path, service_user: str) -> tuple[Path, tuple[int, int, int]]:
    if pwd is None:
        raise RunnerFailure("posix_required")
    if not path.is_absolute() or path.is_symlink():
        raise RunnerFailure("credential_file_unsafe")
    resolved = path.resolve(strict=True)
    repository = repository.resolve(strict=True)
    if resolved == repository or repository in resolved.parents or not resolved.is_file():
        raise RunnerFailure("credential_file_unsafe")
    account = pwd.getpwnam(service_user)
    metadata = resolved.stat(follow_symlinks=False)
    if metadata.st_uid != account.pw_uid or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RunnerFailure("credential_file_unsafe")
    if not resolved.read_bytes():
        raise RunnerFailure("credential_missing")
    return resolved, (metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns)


def delete_exact_file(path: Path, identity: tuple[int, int, int]) -> None:
    if path.is_symlink() or not path.is_file():
        raise RunnerFailure("credential_cleanup_failed")
    metadata = path.stat(follow_symlinks=False)
    if (metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns) != identity:
        raise RunnerFailure("credential_cleanup_failed")
    path.unlink()
    if path.exists():
        raise RunnerFailure("credential_cleanup_failed")


class DjangoState:
    def __init__(self, *, user_id: int, username: str) -> None:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django

        django.setup()
        from django.contrib.auth import get_user_model
        from django.contrib.sessions.models import Session
        from relay.models import BackgroundJob, BulkSend, BulkSendRecipient, EmailMessage
        from relay.services.operator_permissions import can_operate_bulk_sends

        self.Session = Session
        self.BackgroundJob = BackgroundJob
        self.BulkSend = BulkSend
        self.BulkSendRecipient = BulkSendRecipient
        self.EmailMessage = EmailMessage
        self.user_id = user_id
        User = get_user_model()

        def _lookup(uid, uname):
            try:
                return User.objects.get(pk=uid, username=uname)
            except User.DoesNotExist:
                return None

        self.user = resolve_authorized_user(
            user_id, username, lookup=_lookup, can_operate_bulk_sends=can_operate_bulk_sends
        )

    def user_session_keys(self) -> frozenset[str]:
        keys = set()
        for session in self.Session.objects.all().iterator():
            if str(session.get_decoded().get("_auth_user_id", "")) == str(self.user_id):
                keys.add(session.session_key)
        return frozenset(keys)

    def baseline(self) -> Baseline:
        return Baseline(
            self.Session.objects.count(), self.user_session_keys(),
            self.BulkSend.objects.count(), self.BulkSend.objects.filter(engine_version="v2").count(),
            self.BulkSendRecipient.objects.count(), self.BackgroundJob.objects.count(), self.EmailMessage.objects.count(),
        )

    def identify_new_session(self, before: Baseline, key: str) -> None:
        new_keys = self.user_session_keys() - before.user_sessions
        if new_keys != {key}:
            raise RunnerFailure("session_identification_ambiguous")
        session = self.Session.objects.filter(session_key=key).first()
        if session is None or str(session.get_decoded().get("_auth_user_id", "")) != str(self.user_id):
            raise RunnerFailure("session_identification_failed")

    def delete_session(self, key: str) -> None:
        self.Session.objects.filter(session_key=key).delete()
        if self.Session.objects.filter(session_key=key).exists():
            raise RunnerFailure("session_cleanup_failed")

    def assert_functional_unchanged(self, before: Baseline) -> None:
        after = self.baseline()
        if (after.bulk_sends, after.bulk_sends_v2, after.recipients, after.jobs, after.messages) != (
            before.bulk_sends, before.bulk_sends_v2, before.recipients, before.jobs, before.messages
        ):
            raise RunnerFailure("functional_data_changed")
        if after.user_sessions != before.user_sessions or after.total_sessions != before.total_sessions:
            raise RunnerFailure("session_cleanup_incomplete")


_RESOLVABLE_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
)

_DISCOVERY_SUBSTAGES = (
    "service_metadata_loaded",
    "service_working_directory_validated",
    "nginx_config_tested",
    "nginx_config_dumped",
    "vhost_candidates_parsed",
    "unique_vhost_selected",
    "certificate_paths_discovered",
    "certificate_hostname_validated",
    "local_resolution_prepared",
    "nginx_target_validated",
)

# Maps a discover_and_validate_nginx() failure classification (from either
# run_nginx_config_test or run_nginx_discovery in ops.deployment_hardening)
# onto (an existing ops.td02c_http_client.ERROR_CODES entry, the closest
# matching substage in _DISCOVERY_SUBSTAGES above). AuthenticatedGetFailure
# normalizes any error_class outside ERROR_CODES to "unknown_failure", and
# that closed vocabulary is intentionally not extended here -- nginx
# discovery is delegated to shared privileged helpers, but the runner's own
# diagnostic taxonomy stays exactly what it already was. Unknown/future
# deployment_hardening classifications fail closed onto
# _DEFAULT_NGINX_FAILURE rather than being silently dropped.
_NGINX_FAILURE_MAP = {
    "nginx_check_permission_denied": ("command_permission_denied", "nginx_config_tested"),
    "nginx_check_sudoers_missing": ("command_permission_denied", "nginx_config_tested"),
    "nginx_check_sudoers_invalid": ("command_permission_denied", "nginx_config_tested"),
    "nginx_check_command_rejected": ("command_permission_denied", "nginx_config_tested"),
    "nginx_check_config_invalid": ("nginx_test_failed", "nginx_config_tested"),
    "nginx_check_unexpected_error": ("unexpected_discovery_error", "nginx_config_tested"),
    "nginx_discovery_command_rejected": ("command_permission_denied", "nginx_config_dumped"),
    "nginx_discovery_helper_missing": ("command_not_found", "nginx_config_dumped"),
    "nginx_discovery_sudoers_missing": ("command_permission_denied", "nginx_config_dumped"),
    "nginx_discovery_invalid_json": ("nginx_dump_failed", "nginx_config_dumped"),
    "nginx_discovery_schema_mismatch": ("nginx_dump_failed", "nginx_config_dumped"),
    "nginx_discovery_sensitive_output_detected": ("nginx_dump_failed", "nginx_config_dumped"),
    "nginx_discovery_config_test_failed": ("nginx_test_failed", "nginx_config_dumped"),
    "nginx_discovery_unexpected_error": ("unexpected_discovery_error", "nginx_config_dumped"),
    "nginx_discovery_no_vhost": ("no_vhost_found", "vhost_candidates_parsed"),
    "nginx_discovery_hostname_rejected": ("no_vhost_found", "vhost_candidates_parsed"),
    "nginx_discovery_multiple_vhosts": ("multiple_vhosts_found", "unique_vhost_selected"),
    "nginx_discovery_certificate_mismatch": ("certificate_hostname_mismatch", "certificate_hostname_validated"),
    "nginx_discovery_socket_missing": ("local_resolution_invalid", "local_resolution_prepared"),
}
_DEFAULT_NGINX_FAILURE = ("unexpected_discovery_error", "nginx_config_dumped")

# The compatible substages emitted as a single PASS batch when
# discover_and_validate_nginx() succeeds -- it already performed all of
# these checks privileged and atomically, so they are reported, not re-run.
_NGINX_SUCCESS_SUBSTAGE_BATCH = (
    "nginx_config_tested",
    "nginx_config_dumped",
    "vhost_candidates_parsed",
    "unique_vhost_selected",
    "certificate_paths_discovered",
    "certificate_hostname_validated",
)


class _CommandExecutionError(Exception):
    """Internal-only: carries a sanitized classification for a failed exec."""

    def __init__(self, classification: str):
        super().__init__(classification)
        self.classification = classification


def _sanitize_stream(output: str, limit: int = 200) -> str:
    """Return one short, redacted line; never the full nginx/openssl output."""
    line = next((candidate.strip() for candidate in output.splitlines() if candidate.strip()), "")
    return redact_output(line)[:limit]


def _safe_for_local_resolution(target: NginxTarget) -> bool:
    if target.port != 443:
        return False
    if not target.server_name or not _RESOLVABLE_HOSTNAME.fullmatch(target.server_name):
        return False
    if not target.upstream or not Path(target.upstream).is_absolute():
        return False
    return True


class CurlOperations:
    def __init__(self, *, service_unit: str, credential_file: Path, state: DjangoState, baseline: Baseline, log: SafeDiagnosticLog):
        self.service = discover_service(Runner(), service_unit)
        self.target: NginxTarget | None = None
        self.credential_file = credential_file
        self.state = state
        self.baseline = baseline
        self.log = log
        self.session_key = ""
        self.session_identified = False
        self._current_discovery_substage = _DISCOVERY_SUBSTAGES[0]

    def _emit_substage(
        self, name: str, started: float, *, classification: str = "", command: str = "", detail: str = ""
    ) -> None:
        result = "FAIL" if classification else "PASS"
        exit_code = 1 if classification else 0
        self.log.emit(
            StageDiagnostic(
                name, result, exit_code, time.monotonic() - started, classification,
                command=command, detail=detail,
            )
        )

    def _run_diagnosed_command(
        self, argv: list[str], *, timeout: int = 15
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, check=False, timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise _CommandExecutionError("command_not_found") from exc
        except PermissionError as exc:
            raise _CommandExecutionError("command_permission_denied") from exc
        except subprocess.SubprocessError as exc:
            raise _CommandExecutionError("subprocess_failed") from exc

    def discover_target(self) -> NginxTarget:
        self._current_discovery_substage = _DISCOVERY_SUBSTAGES[0]
        started = time.monotonic()
        try:
            target = self._discover_nginx_target_diagnosed()
        except AuthenticatedGetFailure:
            raise
        except Exception:
            self._emit_substage(
                self._current_discovery_substage, started, classification="unexpected_discovery_error"
            )
            raise AuthenticatedGetFailure("unexpected_discovery_error") from None
        self.target = target
        return target

    def _discover_nginx_target_diagnosed(self) -> NginxTarget:
        service = self.service

        self._current_discovery_substage = "service_metadata_loaded"
        started = time.monotonic()
        if not service.unit or not service.user or not service.exec_start_raw:
            self._emit_substage("service_metadata_loaded", started, classification="service_metadata_invalid")
            raise AuthenticatedGetFailure("service_metadata_invalid")
        command = f"systemctl is-active {service.unit}"
        try:
            result = self._run_diagnosed_command(["systemctl", "is-active", service.unit])
        except _CommandExecutionError as exc:
            self._emit_substage(
                "service_metadata_loaded", started, classification=exc.classification, command=command
            )
            raise AuthenticatedGetFailure(exc.classification) from None
        if result.returncode or result.stdout.strip() != "active":
            detail = _sanitize_stream(result.stdout or result.stderr)
            self._emit_substage(
                "service_metadata_loaded", started, classification="systemctl_failed", command=command, detail=detail
            )
            raise AuthenticatedGetFailure("systemctl_failed")
        self._emit_substage("service_metadata_loaded", started, command=command)

        self._current_discovery_substage = "service_working_directory_validated"
        started = time.monotonic()
        if not service.working_directory.is_absolute() or not service.working_directory.is_dir():
            self._emit_substage("service_working_directory_validated", started, classification="working_directory_invalid")
            raise AuthenticatedGetFailure("working_directory_invalid")
        self._emit_substage("service_working_directory_validated", started)

        # Nginx discovery is delegated entirely to the shared, already-
        # deployed privileged mechanism in ops.deployment_hardening (the
        # same one used by the normal preflight path): this runner must
        # never run nginx -t/-T -- or openssl against the root-only
        # certificate -- itself. discover_and_validate_nginx() performs
        # config-test, dump+parse, vhost selection, certificate discovery,
        # and hostname validation atomically as root; on success it is
        # reported here as a single compatible PASS batch (no command is
        # re-run), and on failure its one classification is mapped onto
        # the closest matching substage below.
        self._current_discovery_substage = "nginx_config_tested"
        started = time.monotonic()
        try:
            target = discover_and_validate_nginx(Runner(), service)
        except DeploymentError as exc:
            deployment_classification = str(exc).split(":", 1)[0].strip()
            classification, substage = _NGINX_FAILURE_MAP.get(deployment_classification, _DEFAULT_NGINX_FAILURE)
            self._current_discovery_substage = substage
            self._emit_substage(
                substage, started, classification=classification, detail=_sanitize_stream(str(exc))
            )
            raise AuthenticatedGetFailure(classification) from None

        for substage in _NGINX_SUCCESS_SUBSTAGE_BATCH:
            self._current_discovery_substage = substage
            self._emit_substage(
                substage, started,
                detail=f"hostname={target.server_name}" if substage == "unique_vhost_selected" else "",
            )

        self._current_discovery_substage = "local_resolution_prepared"
        started = time.monotonic()
        if not _safe_for_local_resolution(target):
            self._emit_substage("local_resolution_prepared", started, classification="local_resolution_invalid")
            raise AuthenticatedGetFailure("local_resolution_invalid")
        self._emit_substage("local_resolution_prepared", started)

        self._current_discovery_substage = "nginx_target_validated"
        started = time.monotonic()
        self._emit_substage("nginx_target_validated", started, detail=f"hostname={target.server_name}")
        return target

    def prepare_tls(self, target: NginxTarget) -> None:
        bind = Path(application_bind_from_exec_start(self.service.exec_start_raw))
        if not bind.is_socket():
            raise AuthenticatedGetFailure("connection_failed")

    @staticmethod
    def _cookie(workspace: Path, name: str) -> str:
        for line in (workspace / "cookies.txt").read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            if len(fields) >= 7 and fields[-2] == name:
                return fields[-1]
        return ""

    def has_cookie(self, workspace: Path, name: str) -> bool:
        value = self._cookie(workspace, name)
        return bool(value)

    def _curl(self, workspace: Path, target: NginxTarget, *, method: str, path: str, config: Path | None = None) -> ResponseMetadata:
        output = workspace / f"{method.lower()}.metadata"
        output.touch(mode=0o600); os.chmod(output, 0o600)
        argv = ["curl", "--silent", "--show-error", "--connect-timeout", "3", "--max-time", "20", "--resolve", f"{target.server_name}:443:127.0.0.1", "--cookie", str(workspace / "cookies.txt"), "--cookie-jar", str(workspace / "cookies.txt"), "--output", "/dev/null", "--write-out", "%{http_code}\t%{content_type}\t%{redirect_url}\t%{num_redirects}\t%{ssl_verify_result}\t%{time_total}"]
        if config is not None:
            argv += ["--config", str(config)]
        argv.append(f"https://{target.server_name}{path}")
        started = time.monotonic()
        result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, check=False, timeout=25)
        if result.returncode:
            raise AuthenticatedGetFailure("tls_failed" if result.returncode == 60 else "connection_failed", exit_code=result.returncode)
        fields = result.stdout.split("\t")
        if len(fields) != 6:
            raise AuthenticatedGetFailure("authenticated_get_failed")
        return ResponseMetadata(method, path, int(fields[0]), fields[1], fields[2], int(fields[3]), float(fields[5]) or time.monotonic() - started, int(fields[4]))

    def get_login(self, workspace: Path, target: NginxTarget) -> ResponseMetadata:
        return self._curl(workspace, target, method="GET", path="/admin/login/")

    def authenticate(self, workspace: Path, target: NginxTarget) -> ResponseMetadata:
        csrf = self._cookie(workspace, "csrftoken")
        if not csrf:
            raise AuthenticatedGetFailure("csrf_cookie_missing")
        config = workspace / "login.curl.conf"
        password = workspace / "password"
        password.write_bytes(self.credential_file.read_bytes()); os.chmod(password, 0o600)
        origin = f"https://{target.server_name}"
        config.write_text("\n".join([
            'request = "POST"', f'header = "Origin: {origin}"', f'header = "Referer: {origin}/admin/login/"',
            f'header = "X-CSRFToken: {csrf}"', f'data-urlencode = "username={self.state.user.username}"',
            f'data-urlencode = "password@{password}"', 'data-urlencode = "next=/app/"'
        ]) + "\n", encoding="utf-8"); os.chmod(config, 0o600)
        response = self._curl(workspace, target, method="POST", path="/admin/login/", config=config)
        key = self._cookie(workspace, "sessionid")
        started = time.monotonic()
        try:
            if not key:
                raise RunnerFailure("session_not_created")
            self.state.identify_new_session(self.baseline, key)
        except RunnerFailure:
            emit(self.log, "session_identified", "FAIL", started, "session_identification_failed")
            raise AuthenticatedGetFailure("authentication_failed") from None
        self.session_key = key
        self.session_identified = True
        emit(self.log, "session_identified", "PASS", started)
        return response

    def authenticated_get(self, workspace: Path, target: NginxTarget) -> ResponseMetadata:
        return self._curl(workspace, target, method="GET", path="/app/")


def run(args: argparse.Namespace, stream: TextIO = sys.stdout) -> int:
    log = SafeDiagnosticLog(stream)
    credential: Path | None = None
    identity: tuple[int, int, int] | None = None
    state: DjangoState | None = None
    baseline: Baseline | None = None
    operations: CurlOperations | None = None
    failure = False
    try:
        service = discover_service(Runner(), args.service_unit)
        started = time.monotonic()
        try:
            credential, identity = validate_credential_file(args.credential_file, service.working_directory, service.user)
        except Exception:
            emit(log, "credential_file_validated", "FAIL", started, "credential_file_unsafe")
            raise
        emit(log, "credential_file_validated", "PASS", started)
        state = DjangoState(user_id=args.user_id, username=args.username)
        baseline = state.baseline(); emit_counts(stream, "baseline_before", baseline)
        operations = CurlOperations(service_unit=args.service_unit, credential_file=credential, state=state, baseline=baseline, log=log)
        run_authenticated_get_gate(operations, log)
    except (
        AuthenticatedGetFailure,
        DeploymentError,
        RunnerFailure,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        failure = True
        if not isinstance(exc, AuthenticatedGetFailure):
            emit(log, "runner", "FAIL", time.monotonic(), str(exc) if str(exc) in {"credential_missing"} else "runner_failed")
    finally:
        if state is not None and baseline is not None:
            started = time.monotonic()
            try:
                if operations is not None and operations.session_identified:
                    state.delete_session(operations.session_key)
                state.assert_functional_unchanged(baseline)
                emit_counts(stream, "baseline_after", state.baseline())
                emit(log, "session_cleanup_completed", "PASS", started)
            except Exception:
                failure = True; emit(log, "session_cleanup_completed", "FAIL", started, "session_cleanup_failed")
        if credential is not None and identity is not None:
            started = time.monotonic()
            try:
                delete_exact_file(credential, identity)
            except Exception:
                failure = True; emit(log, "credential_cleanup_completed", "FAIL", started, "credential_cleanup_failed")
            else:
                emit(log, "credential_cleanup_completed", "PASS", started)
    return 1 if failure else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-unit", default="django.service")
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--user-id", required=True, type=int)
    parser.add_argument("--username", required=True, type=str)
    args = parser.parse_args(argv)
    log = SafeDiagnosticLog(sys.stdout)
    started = time.monotonic()
    try:
        validate_module_entrypoint(args.service_unit)
    except (DeploymentError, OSError, RunnerFailure, subprocess.SubprocessError) as exc:
        classification = str(exc).split(":", 1)[0]
        allowed = {
            "posix_required", "module_entrypoint_required",
            "working_directory_mismatch", "runner_module_missing",
            "repository_not_importable", "effective_user_mismatch",
            "already_running_as_service_user",
            "switched_from_root_to_service_user", "cannot_switch_user",
            "service_user_mismatch", "command_failed",
        }
        emit(log, "entrypoint_validated", "FAIL", started,
             classification if classification in allowed else "entrypoint_validation_failed")
        return 1
    emit(log, "entrypoint_validated", "PASS", started)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
