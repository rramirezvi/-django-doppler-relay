#!/usr/bin/env python3
"""Least-privilege Nginx configuration check for TD-02C preflight.

Runs `nginx -t` unprivileged first. If, and only if, that fails with a
certificate-read permission error, escalates through the single narrow
sudoers rule installed at /etc/sudoers.d/td02c-nginx-check, which permits
exactly one fixed command as root: /usr/sbin/nginx -t. No other command,
argument, flag, or binary is ever attempted. This script takes no
arguments and never invokes a shell.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

NGINX_BINARY = "/usr/sbin/nginx"
SUDO_BINARY = "/usr/bin/sudo"
MINIMAL_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
TIMEOUT_SECONDS = 15

# Canonical content for /etc/sudoers.d/td02c-nginx-check. This is the single
# source of truth: the file installed on the server must match this exactly
# (owner root, group root, mode 0440). It permits exactly one fixed command
# as root -- the nginx binary with a single -t argument -- and nothing else:
# no shell, no other flags, no other binaries, no environment passthrough.
SUDOERS_FILENAME = "td02c-nginx-check"
SUDOERS_CONTENT = (
    "# Managed by ops/td02c_nginx_config_check.py. Do not edit by hand.\n"
    "# Grants app the single fixed command needed to test the Nginx\n"
    "# configuration as root, and nothing else.\n"
    f"app ALL=(root) NOPASSWD: NOEXEC: {NGINX_BINARY} -t\n"
)

PERMISSION_DENIED_PATTERN = re.compile(r"permission denied", re.IGNORECASE)
NOT_ALLOWED_PATTERN = re.compile(r"is not allowed to (run|execute)|sorry,? user", re.IGNORECASE)
NO_PASSWORD_PATTERN = re.compile(r"a password is required|no tty present|sudoers? entry", re.IGNORECASE)
SUDOERS_SYNTAX_PATTERN = re.compile(r"syntax error|unable to parse", re.IGNORECASE)

CLASSIFICATIONS = frozenset({
    "nginx_check_direct_passed",
    "nginx_check_privileged_passed",
    "nginx_check_permission_denied",
    "nginx_check_sudoers_missing",
    "nginx_check_sudoers_invalid",
    "nginx_check_command_rejected",
    "nginx_check_config_invalid",
    "nginx_check_unexpected_error",
})


class NginxCheckResult:
    def __init__(self, classification: str, method: str, exit_code: int, detail: str = ""):
        if classification not in CLASSIFICATIONS:
            classification = "nginx_check_unexpected_error"
        self.classification = classification
        self.method = method
        self.exit_code = exit_code
        self.detail = detail

    @property
    def passed(self) -> bool:
        return self.classification.endswith("_passed")

    def as_dict(self) -> dict[str, object]:
        value = {
            "phase": "nginx_config_check",
            "classification": self.classification,
            "method": self.method,
            "exit_code": self.exit_code,
            "result": "PASS" if self.passed else "FAIL",
        }
        if self.detail:
            value["detail"] = self.detail
        return value


def _sanitize(output: str, limit: int = 200) -> str:
    """Return one short line; never the full nginx -t output or config."""
    line = next((candidate.strip() for candidate in output.splitlines() if candidate.strip()), "")
    return line[:limit]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=dict(MINIMAL_ENV),
        shell=False,
        check=False,
        timeout=TIMEOUT_SECONDS,
    )


def _classify_sudo_rejection(output: str, exit_code: int) -> NginxCheckResult:
    if NOT_ALLOWED_PATTERN.search(output):
        return NginxCheckResult("nginx_check_command_rejected", "privileged", exit_code, _sanitize(output))
    if SUDOERS_SYNTAX_PATTERN.search(output):
        return NginxCheckResult("nginx_check_sudoers_invalid", "privileged", exit_code, _sanitize(output))
    if NO_PASSWORD_PATTERN.search(output):
        return NginxCheckResult("nginx_check_sudoers_missing", "privileged", exit_code, _sanitize(output))
    if PERMISSION_DENIED_PATTERN.search(output):
        return NginxCheckResult("nginx_check_permission_denied", "privileged", exit_code, _sanitize(output))
    return NginxCheckResult("nginx_check_config_invalid", "privileged", exit_code, _sanitize(output))


def check() -> NginxCheckResult:
    if not (os.path.isfile(NGINX_BINARY) and os.access(NGINX_BINARY, os.X_OK)):
        return NginxCheckResult("nginx_check_unexpected_error", "none", 1, "nginx binary missing or not executable")

    try:
        direct = _run([NGINX_BINARY, "-t"])
    except FileNotFoundError:
        return NginxCheckResult("nginx_check_unexpected_error", "direct", 1, "nginx binary not found")
    except subprocess.SubprocessError as exc:
        return NginxCheckResult("nginx_check_unexpected_error", "direct", 1, _sanitize(str(exc)))

    if direct.returncode == 0:
        return NginxCheckResult("nginx_check_direct_passed", "direct", 0)

    if not PERMISSION_DENIED_PATTERN.search(direct.stdout):
        # A syntax or configuration error, not a permission problem: never
        # escalate for this -- escalation only ever helps a permission gap.
        return NginxCheckResult("nginx_check_config_invalid", "direct", direct.returncode, _sanitize(direct.stdout))

    if not (os.path.isfile(SUDO_BINARY) and os.access(SUDO_BINARY, os.X_OK)):
        return NginxCheckResult("nginx_check_sudoers_missing", "direct", direct.returncode, "sudo unavailable")

    try:
        privileged = _run([SUDO_BINARY, "-n", NGINX_BINARY, "-t"])
    except FileNotFoundError:
        return NginxCheckResult("nginx_check_sudoers_missing", "direct", direct.returncode, "sudo not found")
    except subprocess.SubprocessError as exc:
        return NginxCheckResult("nginx_check_unexpected_error", "privileged", 1, _sanitize(str(exc)))

    if privileged.returncode == 0:
        return NginxCheckResult("nginx_check_privileged_passed", "privileged", 0)

    return _classify_sudo_rejection(privileged.stdout, privileged.returncode)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        result = NginxCheckResult("nginx_check_command_rejected", "none", 2, "unexpected arguments")
    else:
        result = check()
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
