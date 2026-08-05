#!/usr/bin/env python3
"""Least-privilege Nginx discovery helper for TD-02C.

Installed as a fixed, root-owned executable outside the mutable checkout
(conventionally /usr/local/libexec/td02c-nginx-discovery) and authorized
by a single closed sudoers rule that permits exactly that absolute path,
with zero arguments, NOPASSWD, as root. This is the only thing on the
host allowed to read `nginx -T`'s full output and the certificate it
names -- it parses that output internally, as root, and emits nothing
but a small, schema-versioned, sanitized JSON summary. The caller (the
unprivileged app account) never sees the raw config dump, certificate
contents, or any other vhost's configuration.

Deliberately NOT NOEXEC: NOEXEC makes every execve() call from within
the sudo-invoked process fail, indiscriminately -- it does not
distinguish "legitimate internal subprocess" from "shell escape". This
helper's entire job is to orchestrate a fixed set of subprocess calls
(systemctl, nginx, openssl, test), so NOEXEC would break it outright.
Unlike the raw nginx -t rule (a leaf command, correctly NOEXEC), this is
an orchestrator. Security instead comes from: the sudoers command match
being an exact absolute path with zero arguments (nothing the caller
supplies is ever part of what executes); this file being root-owned and
unwritable by app; and every subprocess this script runs being a fixed,
hardcoded, absolute-path command built from no external input at all --
no argv, no stdin, no inherited environment beyond a minimal fixed PATH.

Takes no arguments and reads no stdin. The one service unit it inspects
(SERVICE_UNIT below) is hardcoded, not supplied by the caller, so the
sudoers rule's "zero arguments" guarantee is never weakened by a value
the caller could otherwise have chosen.

Deliberately self-contained (no `ops.*` imports): the installed copy at
INSTALLED_PATH lives outside the checkout, alongside no `ops` package, so
any cross-module import would fail at the one place it must never fail --
the privileged path. The vhost-parsing helpers below are therefore a
private duplicate of ops.deployment_hardening's discover_nginx_target /
extract_blocks / application_bind_from_exec_start, not a shared import.
Keep them behaviorally identical to that module if either changes.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SYSTEMCTL_BINARY = "/usr/bin/systemctl"
NGINX_BINARY = "/usr/sbin/nginx"
OPENSSL_BINARY = "/usr/bin/openssl"
TEST_BINARY = "/usr/bin/test"
SERVICE_UNIT = "django.service"
MINIMAL_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
TIMEOUT_SECONDS = 15
SCHEMA = "td02c.nginx-discovery/v1"

# Fixed, root-owned installation path outside the mutable checkout. The
# versioned source below (this file) is never executed directly by app;
# it is installed as this exact path, root:root, mode 0755, before the
# sudoers rule that authorizes it is ever installed.
INSTALLED_PATH = "/usr/local/libexec/td02c-nginx-discovery"

# Canonical content for /etc/sudoers.d/td02c-nginx-discovery. Single
# source of truth: the installed file must match this exactly (owner
# root, group root, mode 0440). Permits exactly one fixed absolute path
# as root, with zero arguments -- no wildcard, no shell, no other
# binary. The trailing `""` is not decorative: in sudoers, a command
# spec with NO argument list matches that command with ANY arguments
# (confirmed against a real installed rule and a real sudo -n call --
# `sudo -n <path> --anything` succeeded and reached the script until
# this was added). `""` is sudoers' own syntax for "this command,
# invoked with zero arguments, and nothing else" -- verified for real
# via `sudo -n -l` and a real rejected `--anything` invocation, which
# then fails at the sudo layer itself ("a password is required") and
# never reaches this script. Without it, zero-argument enforcement
# would rest solely on this script's own argv check in main(), a
# single mutable layer for what must be an OS-enforced invariant.
# Deliberately NOT NOEXEC (see module docstring): this is an
# orchestrator, not a leaf command, and NOEXEC would break its own
# internal subprocess calls.
SUDOERS_FILENAME = "td02c-nginx-discovery"
SUDOERS_CONTENT = (
    "# Managed by ops/td02c_nginx_discovery.py. Do not edit by hand.\n"
    "# Grants app the single fixed command needed to run the privileged\n"
    "# Nginx discovery helper as root, with zero arguments, and nothing\n"
    "# else. The trailing \"\" enforces zero arguments at the sudoers\n"
    "# level (a bare path would permit any arguments). Not NOEXEC: this\n"
    "# helper legitimately spawns systemctl/nginx/openssl/test internally,\n"
    "# using only fixed, hardcoded, absolute-path commands with no\n"
    "# argument, stdin, or environment the caller controls.\n"
    f'app ALL=(root) NOPASSWD: {INSTALLED_PATH} ""\n'
)


class DeploymentError(Exception):
    """Raised by the private vhost-parsing duplicate below."""


@dataclasses.dataclass(frozen=True)
class NginxTarget:
    server_name: str
    port: int
    upstream: str
    certificate: Path | None


def application_bind_from_exec_start(raw: str) -> str:
    match = re.search(r"(?:--bind(?:=|\s+))(?P<bind>unix:)?(?P<path>/[^\s;]+)", raw)
    if not match:
        raise DeploymentError("Could not derive an absolute application bind from ExecStart")
    return match.group("path")


def extract_blocks(config: str, keyword: str) -> list[str]:
    blocks: list[str] = []
    pattern = re.compile(rf"\b{re.escape(keyword)}(?:\s+[^{{\s]+)?\s*\{{")
    for match in pattern.finditer(config):
        depth = 0
        for index in range(match.end() - 1, len(config)):
            if config[index] == "{":
                depth += 1
            elif config[index] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(config[match.start() : index + 1])
                    break
    return blocks


def discover_nginx_target(config: str, bind_path: str) -> NginxTarget:
    upstreams: dict[str, str] = {}
    for block in extract_blocks(config, "upstream"):
        name_match = re.match(r"\s*upstream\s+([^\s{]+)", block)
        if name_match and bind_path in block:
            upstreams[name_match.group(1)] = bind_path

    candidates: list[NginxTarget] = []
    for block in extract_blocks(config, "server"):
        if not re.search(r"\blisten\s+[^;]*443[^;]*\bssl\b", block):
            continue
        proxy_targets = re.findall(r"\bproxy_pass\s+([^;]+);", block)
        direct_match = bind_path in block
        named_match = any(
            target.strip().removeprefix("http://").removeprefix("https://") in upstreams
            for target in proxy_targets
        )
        if not direct_match and not named_match:
            continue
        names: list[str] = []
        for directive in re.findall(r"\bserver_name\s+([^;]+);", block):
            names.extend(directive.split())
        names = [
            name
            for name in names
            if name and name != "_" and "*" not in name and "$" not in name
        ]
        certificates = re.findall(r"\bssl_certificate\s+([^;]+);", block)
        certificate = Path(certificates[0].strip()) if certificates else None
        for name in names:
            candidates.append(
                NginxTarget(
                    server_name=name,
                    port=443,
                    upstream=bind_path,
                    certificate=certificate,
                )
            )

    unique = {(item.server_name, item.port, item.upstream): item for item in candidates}
    if len(unique) != 1:
        rendered = ", ".join(sorted(name for name, _, _ in unique)) or "none"
        raise DeploymentError(f"Nginx target is ambiguous; candidates: {rendered}")
    target = next(iter(unique.values()))
    if not re.fullmatch(
        r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
        target.server_name,
    ):
        raise DeploymentError("Discovered Nginx server_name is empty or invalid")
    return target


CLASSIFICATIONS = frozenset({
    "nginx_discovery_privileged_passed",
    "nginx_discovery_command_rejected",
    "nginx_discovery_no_vhost",
    "nginx_discovery_multiple_vhosts",
    "nginx_discovery_hostname_rejected",
    "nginx_discovery_certificate_mismatch",
    "nginx_discovery_socket_missing",
    "nginx_discovery_config_test_failed",
    "nginx_discovery_unexpected_error",
})

_BASE_FIELDS = ("schema", "phase", "classification", "result", "exit_code")
SUCCESS_FIELDS = _BASE_FIELDS + (
    "server_name", "port", "proxy_or_socket_target",
    "certificate_path", "certificate_key_path_present", "nginx_test_passed",
)


class NginxDiscoveryResult:
    def __init__(self, classification: str, exit_code: int, detail: str = "", fields: dict | None = None):
        if classification not in CLASSIFICATIONS:
            classification = "nginx_discovery_unexpected_error"
        self.classification = classification
        self.exit_code = exit_code
        self.detail = detail
        self.fields = fields or {}

    @property
    def passed(self) -> bool:
        return self.classification == "nginx_discovery_privileged_passed"

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": SCHEMA,
            "phase": "nginx_discovery",
            "classification": self.classification,
            "result": "PASS" if self.passed else "FAIL",
            "exit_code": self.exit_code,
        }
        if self.passed:
            value.update(self.fields)
        elif self.detail:
            value["detail"] = _sanitize(self.detail)
        return value


def _sanitize(output: str, limit: int = 200) -> str:
    """One short line; never the full config dump, cert, or key material."""
    line = next((candidate.strip() for candidate in output.splitlines() if candidate.strip()), "")
    return line[:limit]


def _run(argv: list[str], timeout: int = TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=dict(MINIMAL_ENV), shell=False, check=False, timeout=timeout,
    )


def discover() -> NginxDiscoveryResult:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return NginxDiscoveryResult("nginx_discovery_unexpected_error", 1, "must run as root")

    try:
        show = _run([SYSTEMCTL_BINARY, "show", SERVICE_UNIT, "--property=ExecStart"])
    except subprocess.SubprocessError as exc:
        return NginxDiscoveryResult("nginx_discovery_unexpected_error", 1, str(exc))
    if show.returncode:
        return NginxDiscoveryResult("nginx_discovery_unexpected_error", show.returncode, "systemctl show failed")
    try:
        bind_path = application_bind_from_exec_start(show.stdout)
    except DeploymentError as exc:
        return NginxDiscoveryResult("nginx_discovery_unexpected_error", 1, str(exc))

    try:
        test = _run([NGINX_BINARY, "-t"])
    except subprocess.SubprocessError as exc:
        return NginxDiscoveryResult("nginx_discovery_unexpected_error", 1, str(exc))
    if test.returncode:
        return NginxDiscoveryResult("nginx_discovery_config_test_failed", test.returncode, test.stdout)

    try:
        dump = _run([NGINX_BINARY, "-T"])
    except subprocess.SubprocessError as exc:
        return NginxDiscoveryResult("nginx_discovery_unexpected_error", 1, str(exc))
    if dump.returncode:
        return NginxDiscoveryResult("nginx_discovery_config_test_failed", dump.returncode, dump.stdout)

    try:
        target = discover_nginx_target(dump.stdout, bind_path)
    except DeploymentError as exc:
        message = str(exc)
        if "empty or invalid" in message:
            return NginxDiscoveryResult("nginx_discovery_hostname_rejected", 1, message)
        if "candidates: none" in message:
            return NginxDiscoveryResult("nginx_discovery_no_vhost", 1, message)
        return NginxDiscoveryResult("nginx_discovery_multiple_vhosts", 1, message)

    try:
        socket_check = _run([TEST_BINARY, "-S", target.upstream])
    except subprocess.SubprocessError as exc:
        return NginxDiscoveryResult("nginx_discovery_unexpected_error", 1, str(exc))
    if socket_check.returncode:
        return NginxDiscoveryResult("nginx_discovery_socket_missing", socket_check.returncode)

    certificate_key_present = False
    if target.certificate:
        try:
            cert_check = _run([
                OPENSSL_BINARY, "x509", "-in", str(target.certificate), "-noout",
                "-checkhost", target.server_name,
            ])
        except subprocess.SubprocessError as exc:
            return NginxDiscoveryResult("nginx_discovery_unexpected_error", 1, str(exc))
        # `openssl x509 -checkhost` always exits 0, match or not -- confirmed
        # for real (two direct calls, mismatched and matched host, both
        # returned exit code 0). The result is only in stdout text, so the
        # match must be parsed from there; returncode is not evidence of
        # anything and is intentionally not used as the pass/fail signal.
        if "does match certificate" not in cert_check.stdout.lower():
            return NginxDiscoveryResult(
                "nginx_discovery_certificate_mismatch", cert_check.returncode, cert_check.stdout
            )
        certificate_key_present = target.certificate.parent.joinpath("privkey.pem").is_file()

    return NginxDiscoveryResult(
        "nginx_discovery_privileged_passed", 0,
        fields={
            "server_name": target.server_name,
            "port": target.port,
            "proxy_or_socket_target": target.upstream,
            "certificate_path": str(target.certificate) if target.certificate else None,
            "certificate_key_path_present": certificate_key_present,
            "nginx_test_passed": True,
        },
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        result = NginxDiscoveryResult("nginx_discovery_command_rejected", 2, "unexpected arguments")
    else:
        result = discover()
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
