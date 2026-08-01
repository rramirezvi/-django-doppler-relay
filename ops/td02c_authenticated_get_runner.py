#!/usr/bin/env python3
"""Versioned, fail-closed runner for the TD-02C authenticated GET gate."""

from __future__ import annotations

import argparse
import json
import os
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
    Runner,
    application_bind_from_exec_start,
    discover_and_validate_nginx,
    discover_service,
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

EXPECTED_USER_ID = 1
EXPECTED_USERNAME = "ricardo"


class RunnerFailure(RuntimeError):
    pass


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
    def __init__(self) -> None:
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
        self.user = get_user_model().objects.get(pk=EXPECTED_USER_ID, username=EXPECTED_USERNAME)
        if not self.user.is_active or not self.user.is_staff or not can_operate_bulk_sends(self.user):
            raise RunnerFailure("authorized_user_invalid")

    def user_session_keys(self) -> frozenset[str]:
        keys = set()
        for session in self.Session.objects.all().iterator():
            if str(session.get_decoded().get("_auth_user_id", "")) == str(EXPECTED_USER_ID):
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
        if session is None or str(session.get_decoded().get("_auth_user_id", "")) != str(EXPECTED_USER_ID):
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

    def discover_target(self) -> NginxTarget:
        self.target = discover_and_validate_nginx(Runner(), self.service)
        return self.target

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
            f'header = "X-CSRFToken: {csrf}"', f'data-urlencode = "username={EXPECTED_USERNAME}"',
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
        state = DjangoState(); baseline = state.baseline(); emit_counts(stream, "baseline_before", baseline)
        operations = CurlOperations(service_unit=args.service_unit, credential_file=credential, state=state, baseline=baseline, log=log)
        run_authenticated_get_gate(operations, log)
    except (AuthenticatedGetFailure, RunnerFailure, OSError, subprocess.SubprocessError) as exc:
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
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
