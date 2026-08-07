#!/usr/bin/env python3
"""Fail-closed deployment procedure driven by systemd and Nginx discovery."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import grp
    import pwd
except ImportError:  # pragma: no cover - operational tooling is POSIX-only
    grp = None  # type: ignore[assignment]
    pwd = None  # type: ignore[assignment]


class DeploymentError(RuntimeError):
    pass


class DeploymentInterrupted(DeploymentError):
    pass


@dataclasses.dataclass(frozen=True)
class ServiceUserCommand:
    argv: list[str]
    classification: str


def run_as_service_user_command(
    args: list[str],
    target_user: str,
    *,
    effective_uid: int | None = None,
    effective_user: str | None = None,
) -> ServiceUserCommand:
    """Choose a fail-closed command for the discovered service user."""
    if not target_user or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", target_user):
        raise DeploymentError("service_user_mismatch: invalid service user")
    if not args:
        raise DeploymentError("command_failed: empty command")
    if pwd is None or not hasattr(os, "geteuid"):
        raise DeploymentError("cannot_switch_user: POSIX user discovery unavailable")

    uid = os.geteuid() if effective_uid is None else effective_uid
    if effective_user is None:
        try:
            effective_user = pwd.getpwuid(uid).pw_name
        except KeyError as exc:
            raise DeploymentError("service_user_mismatch: unknown effective user") from exc

    if effective_user == target_user:
        return ServiceUserCommand(list(args), "already_running_as_service_user")
    if uid == 0:
        return ServiceUserCommand(
            ["runuser", "-u", target_user, "--", *args],
            "switched_from_root_to_service_user",
        )
    raise DeploymentError(
        "cannot_switch_user: effective user does not match service user"
    )


def redact_output(value: str) -> str:
    patterns = (
        r"(?im)^(\s*(?:SECRET_KEY|DOPPLER_RELAY_API_KEY|DATABASE_URL)\s*=).*$",
        r"(?im)^(\s*(?:Authorization|Cookie|Set-Cookie)\s*:).*$",
    )
    redacted = value
    for pattern in patterns:
        redacted = re.sub(pattern, r"\1[REDACTED]", redacted)
    return redacted[-8000:]


def _apply_deterministic_umask() -> None:
    """``preexec_fn`` for a forked Git child, never the parent process.

    Runs strictly after ``fork()`` and before ``execve()``, in the child
    only: it changes that one process's umask to 0022, deterministically,
    regardless of whatever umask the parent inherited from its shell, SSH
    session, PAM, or systemd. The parent process (and everything else
    running as the app account) is never touched. Safe here specifically
    because this CLI is single-threaded and never starts threads before
    forking a subprocess -- ``fork()`` after threads exist is unsafe, but
    that never happens in this module.
    """
    os.umask(0o022)


def _resolve_git_binary() -> str:
    """Resolve and validate Git as an absolute, executable path -- never a
    bare command name resolved through an inherited PATH -- for use only
    by the Git subcommands that materialize worktree content."""
    resolved = shutil.which("git")
    if not resolved or not Path(resolved).is_absolute():
        raise DeploymentError("Could not resolve an absolute Git binary path")
    if not os.access(resolved, os.X_OK):
        raise DeploymentError(f"Resolved Git binary is not executable: {resolved}")
    return resolved


_MATERIALIZING_GIT_SUBCOMMANDS = frozenset({"merge", "restore"})


class Runner:
    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        user: str | None = None,
        check: bool = True,
        input_text: str | None = None,
        timeout: int = 120,
        deterministic_umask: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = list(args)
        if user:
            decision = run_as_service_user_command(command, user)
            command = decision.argv
            if deterministic_umask and decision.classification != "already_running_as_service_user":
                raise DeploymentError(
                    "deterministic_umask requires executing directly as the "
                    "service user: a runuser wrapper's own PAM session can "
                    "reset the child's umask after this process's preexec_fn "
                    "runs, so the guarantee cannot be kept through a root "
                    "fallback"
                )
        if deterministic_umask and os.name != "posix":
            raise DeploymentError("deterministic_umask requires POSIX")
        result = subprocess.run(
            command,
            cwd=cwd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            shell=False,
            timeout=timeout,
            preexec_fn=_apply_deterministic_umask if deterministic_umask else None,
        )
        if check and result.returncode:
            rendered = shlex.join(command)
            classification = "command_failed"
            raise DeploymentError(
                f"{classification}: Command failed ({result.returncode}): {rendered}\n"
                f"{redact_output(result.stdout)}"
            )
        return result


def run_git_materializing(
    runner: Runner,
    argv: list[str],
    *,
    cwd: Path,
    user: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the one class of Git subcommand that creates or replaces
    worktree content (merge --ff-only, restore) with an explicit,
    deterministic umask of 0022 -- never inherited from the shell, SSH,
    PAM, or systemd. Resolves Git to an absolute, validated path rather
    than a bare command name, keeps argv a list (never shell=True), and
    keeps Git running as the service user with no root fallback.

    Read-only Git subcommands (rev-parse, status, diff, cat-file,
    ls-tree, ls-remote) must never be routed through here -- call
    Runner.run directly for those, matching the existing convention
    everywhere else in this module.
    """
    if len(argv) < 2 or argv[0] != "git" or argv[1] not in _MATERIALIZING_GIT_SUBCOMMANDS:
        raise DeploymentError(f"Unsafe materializing Git invocation: {argv!r}")
    command = [_resolve_git_binary(), *argv[1:]]
    return runner.run(
        command, cwd=cwd, user=user, check=check, deterministic_umask=True,
    )


@dataclasses.dataclass(frozen=True)
class EnvironmentFile:
    path: Path
    optional: bool


@dataclasses.dataclass(frozen=True)
class ServiceMetadata:
    unit: str
    working_directory: Path
    exec_start_path: Path
    exec_start_raw: str
    python: Path
    user: str
    group: str
    main_pid: int
    fragment_path: Path
    environment_files: tuple[EnvironmentFile, ...]


@dataclasses.dataclass(frozen=True)
class NginxTarget:
    server_name: str
    port: int
    upstream: str
    certificate: Path | None


@dataclasses.dataclass
class DeploymentContext:
    service: ServiceMetadata
    nginx: NginxTarget
    old_sha: str
    target_sha: str
    repository: Path
    branch: str
    remote: str
    changed_files: list[str]
    runtime_files: list[str]
    intersections: list[str]
    runtime_hashes: dict[str, str]
    baseline_smoke: dict[str, dict[str, str]]
    baseline_warning_codes: set[str]
    preflight_readiness: dict[str, object] | None = None
    approved_commits: list[str] = dataclasses.field(default_factory=list)
    runtime_metadata: dict[str, dict[str, object]] = dataclasses.field(
        default_factory=dict
    )
    materialized_path_metadata_before: dict[str, dict[str, object]] = (
        dataclasses.field(default_factory=dict)
    )
    preexisting_modified_paths: list[str] = dataclasses.field(default_factory=list)
    new_paths: list[str] = dataclasses.field(default_factory=list)
    deleted_paths: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class GitStateSnapshot:
    head: str
    branch: str
    status: str
    unstaged_diff: str
    staged_diff: str
    remote_refs: str


def temporary_target_ref(target_sha: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", target_sha):
        raise DeploymentError("Target SHA is not a full object id")
    return f"refs/deployment-preflight/{target_sha.lower()}"


def require_commands(*commands: str) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise DeploymentError("Missing required commands: " + ", ".join(missing))


def parse_systemd_show(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def parse_exec_start_path(raw: str) -> Path:
    match = re.search(r"(?:^|\{\s*)path=(?P<value>(?:\\.|[^ ;}])+)", raw)
    if not match:
        raise DeploymentError("Could not derive ExecStart.path from systemd")
    value = re.sub(
        r"\\x([0-9A-Fa-f]{2})",
        lambda item: chr(int(item.group(1), 16)),
        match.group("value"),
    ).replace(r"\ ", " ")
    path = Path(value)
    if not path.as_posix().startswith("/"):
        raise DeploymentError("ExecStart.path is not absolute")
    return path


def interpreter_from_exec_start(exec_path: Path) -> Path:
    if not exec_path.is_file() or not os.access(exec_path, os.X_OK):
        raise DeploymentError(f"ExecStart executable is missing or not executable: {exec_path}")

    with exec_path.open("rb") as handle:
        first_line = handle.readline(4096)

    if first_line.startswith(b"#!"):
        shebang = first_line[2:].decode("utf-8", errors="strict").strip()
        parts = shlex.split(shebang)
        if not parts or not parts[0].startswith("/"):
            raise DeploymentError("Script shebang must contain an absolute interpreter")
        if Path(parts[0]).name == "env":
            raise DeploymentError("/usr/bin/env shebangs are ambiguous and are not accepted")
        interpreter = Path(parts[0])
        if "python" not in interpreter.name.lower():
            raise DeploymentError(
                f"ExecStart script does not declare a Python interpreter: {interpreter}"
            )
        return interpreter

    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", exec_path.name.lower()):
        return exec_path

    raise DeploymentError(
        "ExecStart is neither a Python executable nor a script with a Python shebang"
    )


def parse_environment_files(raw: str) -> tuple[EnvironmentFile, ...]:
    files: list[EnvironmentFile] = []
    for token in shlex.split(raw.replace(";", " ")):
        token = token.split("(", 1)[0]
        optional = token.startswith("-")
        value = token[1:] if optional else token
        if value.startswith("/"):
            item = EnvironmentFile(Path(value), optional)
            if item not in files:
                files.append(item)
    return tuple(files)


def validate_token(value: str, label: str) -> str:
    if not value or value.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@+-]+", value):
        raise DeploymentError(f"Unsafe {label}: {value!r}")
    return value


def discover_service(runner: Runner, unit: str) -> ServiceMetadata:
    validate_token(unit, "systemd unit")
    properties = [
        "LoadState",
        "ActiveState",
        "SubState",
        "WorkingDirectory",
        "ExecStart",
        "User",
        "Group",
        "MainPID",
        "FragmentPath",
        "EnvironmentFiles",
    ]
    result = runner.run(
        ["systemctl", "show", unit, *[f"--property={item}" for item in properties]]
    )
    values = parse_systemd_show(result.stdout)
    if values.get("LoadState") != "loaded" or values.get("ActiveState") != "active":
        raise DeploymentError(f"{unit} is not loaded and active")

    working_directory = Path(values.get("WorkingDirectory", ""))
    if not working_directory.is_absolute() or not working_directory.is_dir():
        raise DeploymentError("WorkingDirectory is missing, relative, or not a directory")
    manage_py = (working_directory / "manage.py").resolve()
    if not manage_py.is_file() or working_directory.resolve() not in manage_py.parents:
        raise DeploymentError("manage.py is missing or escapes WorkingDirectory")
    if not (working_directory / ".git").exists():
        raise DeploymentError("WorkingDirectory is not a Git checkout")

    user = values.get("User", "")
    group = values.get("Group", "")
    if not user or not group:
        raise DeploymentError("Service User or Group is empty")
    runner.run(["getent", "passwd", user])
    runner.run(["getent", "group", group])

    exec_raw = values.get("ExecStart", "")
    exec_path = parse_exec_start_path(exec_raw)
    python = interpreter_from_exec_start(exec_path)
    if not python.is_file() or not os.access(python, os.X_OK):
        raise DeploymentError(f"Discovered Python is not executable: {python}")

    runner.run(
        [
            str(python),
            "-c",
            "import django,sys; print(sys.executable); print(sys.prefix); print(django.get_version())",
        ],
        cwd=working_directory,
        user=user,
    )
    runner.run(["test", "-r", str(manage_py)], user=user)

    environment_files = parse_environment_files(values.get("EnvironmentFiles", ""))
    for item in environment_files:
        if not item.path.is_file():
            if item.optional:
                continue
            raise DeploymentError(f"EnvironmentFile does not exist: {item.path}")
        runner.run(["test", "-r", str(item.path)], user=user)

    return ServiceMetadata(
        unit=unit,
        working_directory=working_directory,
        exec_start_path=exec_path,
        exec_start_raw=exec_raw,
        python=python,
        user=user,
        group=group,
        main_pid=int(values.get("MainPID", "0") or "0"),
        fragment_path=Path(values.get("FragmentPath", "")),
        environment_files=environment_files,
    )


def current_main_pid(runner: Runner, unit: str) -> int:
    """Re-fetch a unit's current MainPID using the exact ``systemctl show``
    property pattern ``discover_service`` uses, so callers can prove a unit
    was never restarted by comparing this value against a previously
    captured ``ServiceMetadata.main_pid``."""
    result = runner.run(["systemctl", "show", unit, "--property=MainPID"])
    values = parse_systemd_show(result.stdout)
    return int(values.get("MainPID", "0") or "0")


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


_NGINX_CHECK_ALLOWED_CLASSIFICATIONS = frozenset({
    "nginx_check_direct_passed",
    "nginx_check_privileged_passed",
    "nginx_check_permission_denied",
    "nginx_check_sudoers_missing",
    "nginx_check_sudoers_invalid",
    "nginx_check_command_rejected",
    "nginx_check_config_invalid",
    "nginx_check_unexpected_error",
})


def run_nginx_config_test(runner: Runner, service: ServiceMetadata) -> str:
    """Run `nginx -t` through the least-privilege helper, never raw.

    The helper is resolved next to *this* module (``Path(__file__).parent``),
    not under ``service.working_directory``: during
    bootstrap_existing_component_deployment this code itself runs from an
    ephemeral copy of the target version, and the real checkout being
    validated has not been fast-forwarded yet, so it may not contain the
    helper at all. Using the running module's own directory means the
    helper that actually executes is always the one shipped with whichever
    ops.deployment_hardening is currently loaded.

    The helper always executes as the service user; it escalates
    internally through the single narrow sudoers rule only for a
    certificate-read permission failure, never for a syntax/config error.
    This function never falls back to a generic root re-exec of anything
    else.
    """
    helper = Path(__file__).resolve().parent / "td02c_nginx_config_check.py"
    result = runner.run(
        [str(service.python), str(helper)],
        cwd=service.working_directory, user=service.user, check=False,
    )
    classification = "nginx_check_unexpected_error"
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        candidate = payload.get("classification", "")
        if candidate in _NGINX_CHECK_ALLOWED_CLASSIFICATIONS:
            classification = candidate
    except (ValueError, IndexError):
        pass
    if result.returncode or not classification.endswith("_passed"):
        raise DeploymentError(f"{classification}: nginx configuration check failed")
    return classification


NGINX_DISCOVERY_SCHEMA = "td02c.nginx-discovery/v1"
NGINX_DISCOVERY_INSTALLED_PATH = "/usr/local/libexec/td02c-nginx-discovery"
NGINX_DISCOVERY_SUDO_BINARY = "/usr/bin/sudo"

_NGINX_DISCOVERY_BASE_FIELDS = frozenset({"schema", "phase", "classification", "result", "exit_code"})
_NGINX_DISCOVERY_SUCCESS_FIELDS = _NGINX_DISCOVERY_BASE_FIELDS | frozenset({
    "server_name", "port", "proxy_or_socket_target",
    "certificate_path", "certificate_key_path_present", "nginx_test_passed",
})
_NGINX_DISCOVERY_FAILURE_FIELDS = _NGINX_DISCOVERY_BASE_FIELDS | frozenset({"detail"})

# Defense in depth: even though the helper is designed to never emit
# these, the caller independently scans its raw output before trusting
# anything parsed from it.
_NGINX_DISCOVERY_SENSITIVE_OUTPUT_PATTERN = re.compile(
    r"BEGIN (RSA |EC )?PRIVATE KEY|BEGIN CERTIFICATE|ssl_certificate_key|"
    r"\bAuthorization\s*:|\bCookie\s*:|\bSet-Cookie\s*:",
    re.IGNORECASE,
)
_NGINX_DISCOVERY_NOT_ALLOWED_PATTERN = re.compile(
    r"is not allowed to (run|execute)|sorry,? user", re.IGNORECASE
)
_NGINX_DISCOVERY_NO_PASSWORD_PATTERN = re.compile(
    r"a password is required|no tty present|sudoers? entry", re.IGNORECASE
)
_NGINX_DISCOVERY_HELPER_MISSING_PATTERN = re.compile(
    r"no such file|command not found", re.IGNORECASE
)


def run_nginx_discovery(runner: Runner, service: ServiceMetadata) -> NginxTarget:
    """Run the privileged, output-sanitizing Nginx discovery helper --
    never raw `nginx -T` as the unprivileged service user, and no
    fallback to a reduced-privilege attempt. `-T` needs the same
    certificate-read access as `-t`, so there is no unprivileged path
    worth trying first; this goes straight through the closed sudoers
    rule that authorizes exactly the helper's fixed installed path with
    zero arguments. The helper's raw output is scanned for sensitive
    content before anything parsed from it is trusted, its schema is
    validated against an exact allowed field set (never merely a
    superset check), and its own classification must be exactly
    "nginx_discovery_privileged_passed" -- any other value, or any
    inability to parse or validate the output, fails closed.
    """
    result = runner.run(
        [NGINX_DISCOVERY_SUDO_BINARY, "-n", NGINX_DISCOVERY_INSTALLED_PATH],
        cwd=service.working_directory, user=service.user, check=False,
    )
    output = result.stdout.strip()

    if _NGINX_DISCOVERY_SENSITIVE_OUTPUT_PATTERN.search(output):
        raise DeploymentError(
            "nginx_discovery_sensitive_output_detected: "
            "discovery helper output contained unexpected sensitive content"
        )

    payload: object = None
    if output:
        try:
            payload = json.loads(output.splitlines()[-1])
        except ValueError:
            payload = None

    if not isinstance(payload, dict):
        if _NGINX_DISCOVERY_NOT_ALLOWED_PATTERN.search(output):
            raise DeploymentError("nginx_discovery_command_rejected: sudo refused the discovery helper")
        if _NGINX_DISCOVERY_HELPER_MISSING_PATTERN.search(output):
            raise DeploymentError("nginx_discovery_helper_missing: discovery helper is not installed")
        if _NGINX_DISCOVERY_NO_PASSWORD_PATTERN.search(output):
            raise DeploymentError("nginx_discovery_sudoers_missing: discovery sudoers rule is not installed")
        raise DeploymentError("nginx_discovery_invalid_json: discovery helper did not return valid JSON")

    if payload.get("schema") != NGINX_DISCOVERY_SCHEMA:
        raise DeploymentError("nginx_discovery_schema_mismatch: unexpected schema version")

    classification = payload.get("classification")
    success = classification == "nginx_discovery_privileged_passed"
    allowed_keys = _NGINX_DISCOVERY_SUCCESS_FIELDS if success else _NGINX_DISCOVERY_FAILURE_FIELDS
    if set(payload) - allowed_keys:
        raise DeploymentError("nginx_discovery_schema_mismatch: unexpected fields in discovery output")

    if not success or result.returncode:
        raise DeploymentError(f"{classification or 'nginx_discovery_unexpected_error'}: nginx discovery failed")

    required = _NGINX_DISCOVERY_SUCCESS_FIELDS - {"detail"}
    if not required.issubset(payload):
        raise DeploymentError("nginx_discovery_schema_mismatch: missing required fields")

    certificate_path = payload.get("certificate_path")
    return NginxTarget(
        server_name=payload["server_name"],
        port=int(payload["port"]),
        upstream=payload["proxy_or_socket_target"],
        certificate=Path(certificate_path) if certificate_path else None,
    )


def discover_and_validate_nginx(runner: Runner, service: ServiceMetadata) -> NginxTarget:
    run_nginx_config_test(runner, service)
    return run_nginx_discovery(runner, service)


def warning_codes(output: str) -> set[str]:
    return set(re.findall(r"\b(?:security\.)?(W\d{3})\b", output))


def unexpected_warning_codes(output: str, allowed: set[str]) -> set[str]:
    return warning_codes(output) - allowed


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_lines(runner: Runner, cwd: Path, *args: str, user: str | None = None) -> list[str]:
    output = runner.run(["git", *args], cwd=cwd, user=user).stdout
    return [line for line in output.splitlines() if line]


def safe_repo_path(repository: Path, name: str, *, must_exist: bool = False) -> Path:
    if "\x00" in name or Path(name).is_absolute():
        raise DeploymentError(f"Unsafe repository path: {name!r}")
    root = repository.resolve()
    candidate = repository.joinpath(name)
    resolved = candidate.resolve(strict=must_exist)
    if resolved != root and root not in resolved.parents:
        raise DeploymentError(f"Repository path escapes checkout: {name!r}")
    if candidate.is_symlink():
        raise DeploymentError(f"Symlink is not accepted for protected path: {name!r}")
    return candidate


def filesystem_metadata(path: Path) -> dict[str, object]:
    """Return sanitized lstat metadata without reading file content."""
    info = path.stat(follow_symlinks=False)
    owner = str(info.st_uid)
    group = str(info.st_gid)
    if pwd is not None and hasattr(pwd, "getpwuid"):
        try:
            owner = pwd.getpwuid(info.st_uid).pw_name
        except KeyError:
            pass
    if grp is not None and hasattr(grp, "getgrgid"):
        try:
            group = grp.getgrgid(info.st_gid).gr_name
        except KeyError:
            pass
    if stat.S_ISREG(info.st_mode):
        kind = "file"
    elif stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISLNK(info.st_mode):
        kind = "symlink"
    else:
        kind = "other"
    return {
        "exists": True,
        "type": kind,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "owner": owner,
        "group": group,
        "mode": stat.S_IMODE(info.st_mode),
        "size": info.st_size,
        "device": info.st_dev,
        "inode": info.st_ino,
        "symlink": stat.S_ISLNK(info.st_mode),
    }


def git_path_exists(
    runner: Runner, cwd: Path, commit: str, name: str, user: str
) -> bool:
    return runner.run(
        ["git", "cat-file", "-e", f"{commit}:{name}"],
        cwd=cwd, user=user, check=False,
    ).returncode == 0


def git_path_mode(
    runner: Runner, cwd: Path, commit: str, name: str, user: str
) -> int:
    fields = runner.run(
        ["git", "ls-tree", commit, "--", name], cwd=cwd, user=user
    ).stdout.split(None, 1)
    if len(fields) != 2 or not fields[0].isdigit():
        raise DeploymentError(f"Git metadata missing for approved path: {name}")
    return int(fields[0], 8)


def resolve_commit(runner: Runner, cwd: Path, oid: str, user: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", oid):
        raise DeploymentError(f"Commit must be a full hexadecimal object id: {oid!r}")
    result = runner.run(
        ["git", "rev-parse", "--verify", f"{oid}^{{commit}}"], cwd=cwd, user=user
    ).stdout.strip()
    if result.lower() != oid.lower():
        raise DeploymentError(f"Object is not the exact approved commit: {oid}")
    return result.lower()


def require_fast_forward(
    runner: Runner, cwd: Path, old_sha: str, target_sha: str, user: str
) -> None:
    result = runner.run(
        ["git", "merge-base", "--is-ancestor", old_sha, target_sha],
        cwd=cwd, user=user, check=False,
    )
    if result.returncode:
        raise DeploymentError("Target is not a fast-forward from old SHA")


def require_exact_commit_sequence(
    approved_commits: list[str], expected_commits: list[str]
) -> None:
    if expected_commits and approved_commits != expected_commits:
        raise DeploymentError(
            "Approved commit range differs from --expected-commit sequence"
        )


def changed_runtime_intersections(
    changed_files: list[str], runtime_files: list[str]
) -> list[str]:
    return sorted(set(changed_files) & set(runtime_files))


NO_RESTART_ALLOWLIST_PREFIXES = ("ops/", "openspec/changes/")
NO_RESTART_ALLOWLIST_EXACT_PATHS = frozenset({"openspec/config.yaml"})


def classify_restart_requirement(changed_files: list[str]) -> str:
    """Classify whether a deployment's changed files can possibly affect the
    running web process, returning exactly one of two literal strings.

    Returns ``"ops_only_no_restart"`` only when ``changed_files`` is
    non-empty and every entry either starts with an allowlisted prefix
    (``NO_RESTART_ALLOWLIST_PREFIXES``) or is exactly equal to one of the
    allowlisted exact paths (``NO_RESTART_ALLOWLIST_EXACT_PATHS`` -- string
    equality only, never a prefix match, so e.g. ``openspec/config.yaml.bak``
    still fails closed). Otherwise returns ``"web_runtime_required"`` --
    fail-closed, including for an empty list: an empty diff is not evidence
    of anything and must not skip the restart. There is no third
    "ambiguous" outcome and no parameter of any kind that can flip either
    result; this function's return value is the single source of truth and
    nothing downstream may override it.
    """
    if not changed_files:
        return "web_runtime_required"
    if all(
        name.startswith(NO_RESTART_ALLOWLIST_PREFIXES)
        or name in NO_RESTART_ALLOWLIST_EXACT_PATHS
        for name in changed_files
    ):
        return "ops_only_no_restart"
    return "web_runtime_required"


def snapshot_git_state(
    runner: Runner, cwd: Path, remote: str, user: str
) -> GitStateSnapshot:
    return GitStateSnapshot(
        head=runner.run(["git", "rev-parse", "HEAD"], cwd=cwd, user=user).stdout.strip(),
        branch=runner.run(
            ["git", "branch", "--show-current"], cwd=cwd, user=user
        ).stdout.strip(),
        status=runner.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=cwd, user=user,
        ).stdout,
        unstaged_diff=runner.run(
            ["git", "diff", "--binary", "--no-ext-diff"], cwd=cwd, user=user
        ).stdout,
        staged_diff=runner.run(
            ["git", "diff", "--cached", "--binary", "--no-ext-diff"],
            cwd=cwd, user=user,
        ).stdout,
        remote_refs=runner.run(
            [
                "git", "for-each-ref", "--format=%(refname) %(objectname)",
                f"refs/remotes/{remote}/",
            ],
            cwd=cwd, user=user,
        ).stdout,
    )


def assert_git_state_unchanged(
    runner: Runner, cwd: Path, remote: str, user: str, expected: GitStateSnapshot
) -> None:
    actual = snapshot_git_state(runner, cwd, remote, user)
    if actual != expected:
        changed = [
            field.name
            for field in dataclasses.fields(GitStateSnapshot)
            if getattr(actual, field.name) != getattr(expected, field.name)
        ]
        raise DeploymentError(
            "Controlled target acquisition changed HEAD, branch, index, working tree, "
            "or remote-tracking refs: " + ", ".join(changed)
        )


def refresh_deployment_ref(
    runner: Runner,
    cwd: Path,
    *,
    remote: str,
    branch: str,
    target_sha: str,
    user: str,
) -> str:
    """Update exactly the remote-tracking ref consumed by deployment."""
    validate_token(remote, "Git remote")
    validate_token(branch, "Git branch")
    before = snapshot_git_state(runner, cwd, remote, user)
    remote_line = runner.run(
        ["git", "ls-remote", "--exit-code", remote, f"refs/heads/{branch}"],
        cwd=cwd, user=user,
    ).stdout.strip().split()
    if len(remote_line) != 2 or remote_line[0].lower() != target_sha.lower():
        raise DeploymentError("Remote target does not match approved target SHA")
    ref = f"refs/remotes/{remote}/{branch}"
    runner.run(
        [
            "git", "fetch", "--no-tags", "--no-prune", "--no-write-fetch-head",
            "--refmap=", remote, f"refs/heads/{branch}:{ref}",
        ],
        cwd=cwd, user=user,
    )
    resolved = runner.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=cwd, user=user,
    ).stdout.strip().lower()
    if resolved != target_sha.lower():
        raise DeploymentError("Deployment remote-tracking ref is not the approved target")
    resolve_commit(runner, cwd, target_sha, user)
    after = snapshot_git_state(runner, cwd, remote, user)
    for field in ("head", "branch", "status", "unstaged_diff", "staged_diff"):
        if getattr(after, field) != getattr(before, field):
            raise DeploymentError(
                "Controlled deployment fetch changed active Git state: " + field
            )
    return ref


def validate_target_ref(value: str) -> str:
    """Require a fully qualified, non-symbolic, non-remote-tracking,
    non-wildcard Git ref -- the explicit authority bootstrap-existing-
    component resolves --target-sha against, deliberately never the tip of
    the productive branch."""
    if not value or value in ("HEAD", "FETCH_HEAD") or value.startswith("refs/remotes/"):
        raise DeploymentError(f"Unsafe target ref: {value!r}")
    if not re.fullmatch(r"refs/[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?", value):
        raise DeploymentError(f"Unsafe target ref: {value!r}")
    if ".." in value or value.endswith(".lock") or "/.git/" in value:
        raise DeploymentError(f"Unsafe target ref: {value!r}")
    return value


def verify_remote_target_ref(
    runner: Runner,
    cwd: Path,
    *,
    remote: str,
    target_ref: str,
    target_sha: str,
    user: str,
) -> None:
    """Require target_ref to resolve to exactly one remote object, matching
    target_sha exactly. Never accepts ancestry, a wildcard match, or more
    than one line back from ls-remote."""
    validate_target_ref(target_ref)
    result = runner.run(
        ["git", "ls-remote", "--exit-code", remote, target_ref], cwd=cwd, user=user,
    )
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    if len(lines) != 1:
        raise DeploymentError(f"Target ref is missing or ambiguous on remote: {target_ref}")
    fields = lines[0].split()
    if len(fields) != 2 or fields[1] != target_ref:
        raise DeploymentError(f"Target ref did not resolve to itself exactly: {target_ref}")
    if fields[0].lower() != target_sha.lower():
        raise DeploymentError(
            f"Target ref does not resolve to the approved target SHA: {target_ref}"
        )


def fetch_bootstrap_target_ref(
    runner: Runner,
    cwd: Path,
    *,
    remote: str,
    target_ref: str,
    target_sha: str,
    user: str,
) -> str:
    """Fetch an explicit, hash-pinned deployment target ref into an
    isolated local ref -- never the productive branch tip -- reverifying
    the remote object identity both before and after the fetch, so a
    target-ref move mid-flow is never silently accepted. The productive
    branch's own remote-tracking ref is never read, written, or used as an
    implicit fallback here."""
    validate_token(remote, "Git remote")
    before = snapshot_git_state(runner, cwd, remote, user)
    verify_remote_target_ref(
        runner, cwd, remote=remote, target_ref=target_ref, target_sha=target_sha, user=user,
    )
    local_ref = temporary_target_ref(target_sha)
    runner.run(
        [
            "git", "fetch", "--no-tags", "--no-prune", "--no-write-fetch-head",
            "--refmap=", remote, f"{target_ref}:{local_ref}",
        ],
        cwd=cwd, user=user,
    )
    resolved = runner.run(
        ["git", "rev-parse", "--verify", f"{local_ref}^{{commit}}"], cwd=cwd, user=user,
    ).stdout.strip().lower()
    if resolved != target_sha.lower():
        raise DeploymentError("Fetched bootstrap target ref is not the approved target")
    verify_remote_target_ref(
        runner, cwd, remote=remote, target_ref=target_ref, target_sha=target_sha, user=user,
    )
    after = snapshot_git_state(runner, cwd, remote, user)
    for field in ("head", "branch", "status", "unstaged_diff", "staged_diff"):
        if getattr(after, field) != getattr(before, field):
            raise DeploymentError(
                "Controlled bootstrap target-ref fetch changed active Git state: " + field
            )
    return local_ref


def require_deployment_ref(
    runner: Runner,
    cwd: Path,
    *,
    remote: str,
    branch: str,
    target_sha: str,
    user: str,
) -> str:
    ref = f"refs/remotes/{remote}/{branch}"
    resolved = runner.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=cwd, user=user,
    ).stdout.strip().lower()
    if resolved != target_sha.lower():
        raise DeploymentError("Deployment ref does not match the exact approved target")
    return ref


def acquire_target_object(
    runner: Runner,
    cwd: Path,
    *,
    remote: str,
    branch: str,
    target_sha: str,
    user: str,
) -> tuple[str, GitStateSnapshot]:
    """Fetch an approved branch tip into an isolated, hash-qualified ref."""
    ref = temporary_target_ref(target_sha)
    before = snapshot_git_state(runner, cwd, remote, user)
    existing = runner.run(
        ["git", "rev-parse", "--verify", ref], cwd=cwd, user=user, check=False
    )
    if existing.returncode == 0 and existing.stdout.strip().lower() != target_sha.lower():
        raise DeploymentError(
            f"Stale preflight ref points to an unexpected object: {ref}"
        )

    remote_line = runner.run(
        ["git", "ls-remote", "--exit-code", remote, f"refs/heads/{branch}"],
        cwd=cwd, user=user,
    ).stdout.strip().split()
    if len(remote_line) != 2 or remote_line[0].lower() != target_sha.lower():
        raise DeploymentError("Remote target does not match approved target SHA")

    runner.run(
        [
            "git", "fetch", "--no-tags", "--no-prune", "--no-write-fetch-head",
            "--refmap=", remote, f"refs/heads/{branch}:{ref}",
        ],
        cwd=cwd, user=user,
    )
    resolved = resolve_commit(runner, cwd, target_sha, user)
    ref_value = runner.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=cwd, user=user
    ).stdout.strip().lower()
    if ref_value != resolved or resolved != target_sha.lower():
        raise DeploymentError("Temporary preflight ref did not resolve to approved target")
    assert_git_state_unchanged(runner, cwd, remote, user, before)
    return ref, before


def delete_temporary_target_ref(
    runner: Runner, cwd: Path, ref: str, target_sha: str, user: str
) -> None:
    current = runner.run(
        ["git", "rev-parse", "--verify", ref], cwd=cwd, user=user, check=False
    )
    if current.returncode:
        return
    if current.stdout.strip().lower() != target_sha.lower():
        raise DeploymentError(f"Ref cleanup refused because {ref} changed unexpectedly")
    runner.run(["git", "update-ref", "-d", ref, target_sha], cwd=cwd, user=user)


def smoke_request(
    runner: Runner,
    target: NginxTarget,
    path: str,
    method: str = "GET",
    *,
    monotonic=time.monotonic,
) -> dict[str, object]:
    if method not in {"GET", "POST"} or not path.startswith("/") or path.startswith("//"):
        raise DeploymentError("Unsafe smoke-test method or path")
    marker = "__DEPLOY_SMOKE__"
    args = [
        "curl",
        "--silent",
        "--show-error",
        "--output",
        "/dev/null",
        "--connect-timeout",
        "10",
        "--max-time",
        "30",
        "--resolve",
        f"{target.server_name}:{target.port}:127.0.0.1",
        "--request",
        method,
    ]
    if method == "POST":
        args.extend(["--data", ""])
    args.extend(
        [
            "--write-out",
            marker + "%{http_code}",
            f"https://{target.server_name}:{target.port}{path}",
        ]
    )
    started = monotonic()
    output = runner.run(args).stdout
    elapsed = round(monotonic() - started, 3)
    payload = output.split(marker, 1)[-1].strip()
    if not re.fullmatch(r"\d{3}", payload):
        raise DeploymentError("curl did not return a valid HTTP status")
    return {
        "method": method,
        "path": path,
        "status": payload,
        "host_header": target.server_name,
        "url": f"https://{target.server_name}:{target.port}{path}",
        "elapsed_seconds": elapsed,
    }


def validate_readiness_layers(
    runner: Runner,
    service: ServiceMetadata,
    target: NginxTarget,
    *,
    path: str = "/admin/login/",
) -> dict[str, object]:
    """Validate each layer before deployment so HTTP 000 is never misattributed."""
    service_state = runner.run(
        ["systemctl", "is-active", service.unit], check=False
    )
    if service_state.returncode or service_state.stdout.strip() != "active":
        raise DeploymentError("Readiness layer failed: systemd service is not active")

    socket_path = Path(target.upstream)
    if not socket_path.is_absolute():
        raise DeploymentError("Readiness layer failed: socket path is not absolute")
    socket_state = runner.run(
        ["test", "-S", str(socket_path)], user=service.user, check=False
    )
    if socket_state.returncode:
        raise DeploymentError("Readiness layer failed: application socket is unavailable")

    run_nginx_config_test(runner, service)
    response = smoke_request(runner, target, path)
    if response["status"] != "200":
        raise DeploymentError(
            "Readiness layer failed: application endpoint returned "
            f"HTTP {response['status']}"
        )
    return {
        "service": {"unit": service.unit, "status": "active"},
        "socket": {"path": str(socket_path), "available": True},
        "nginx": {"configuration": "valid", "connection": "local-via-127.0.0.1"},
        "application": response,
        "hostname_source": "active-nginx-config",
        "attempts": 1,
    }


def run_manage_check(
    runner: Runner, service: ServiceMetadata, *, deploy: bool = False
) -> tuple[str, set[str]]:
    args = [str(service.python), str(service.working_directory / "manage.py"), "check"]
    if deploy:
        args.append("--deploy")
    result = runner.run(args, cwd=service.working_directory, user=service.user)
    return result.stdout, warning_codes(result.stdout)


def wait_for_application_ready(
    runner: Runner,
    context: DeploymentContext,
    *,
    timeout_seconds: float = 60.0,
    poll_interval: float = 0.25,
    monotonic=time.monotonic,
    sleeper=time.sleep,
) -> dict[str, object]:
    """Poll objective readiness signals; never rely on a fixed startup delay."""
    if timeout_seconds <= 0 or poll_interval <= 0:
        raise DeploymentError("Readiness timeout and poll interval must be positive")
    socket_path = Path(context.nginx.upstream)
    if not socket_path.is_absolute():
        raise DeploymentError("Readiness socket path is not absolute")
    expected = context.baseline_smoke.get("/admin/login/", {}).get("status")
    if not expected:
        raise DeploymentError("Missing login baseline for readiness validation")

    started = monotonic()
    attempts = 0
    last_state = "not-started"
    while monotonic() - started < timeout_seconds:
        attempts += 1
        active = runner.run(
            ["systemctl", "is-active", context.service.unit], check=False
        )
        if active.returncode or active.stdout.strip() != "active":
            last_state = "service-not-active"
            sleeper(poll_interval)
            continue
        socket_ready = runner.run(
            ["test", "-S", str(socket_path)], user=context.service.user, check=False
        )
        if socket_ready.returncode:
            last_state = "socket-not-ready"
            sleeper(poll_interval)
            continue
        try:
            response = smoke_request(
                runner, context.nginx, "/admin/login/", method="GET"
            )
        except DeploymentError:
            last_state = "http-unreachable"
            sleeper(poll_interval)
            continue
        if response["status"] == expected:
            return {
                "ready": True,
                "attempts": attempts,
                "elapsed_seconds": round(monotonic() - started, 3),
                "service": context.service.unit,
                "socket": str(socket_path),
                "probe": response,
            }
        last_state = f"http-{response['status']}-expected-{expected}"
        sleeper(poll_interval)

    raise DeploymentError(
        "Application readiness timeout "
        f"after {timeout_seconds:g}s ({attempts} attempts); last={last_state}"
    )


def _ops_module_name(name: str) -> str:
    return name[: -len(".py")].replace("/", ".")


def verify_ops_only_changed_modules(
    runner: Runner, service: ServiceMetadata, changed_files: list[str]
) -> None:
    """For an ``ops_only_no_restart`` deployment, prove each changed
    ``ops/`` Python module still imports cleanly in a clean process as the
    service user, and that any changed module's CLI entrypoint is still
    wired, without restarting or executing any real behaviour. Modules
    under ``ops/tests/`` are excluded -- they are never a CLI entrypoint
    and never imported by the running web process."""
    cwd = service.working_directory
    for name in changed_files:
        if not name.startswith("ops/") or not name.endswith(".py"):
            continue
        if name.startswith("ops/tests/"):
            continue
        module = _ops_module_name(name)
        runner.run(
            [str(service.python), "-c", f"import {module}"],
            cwd=cwd, user=service.user,
        )
        path = safe_repo_path(cwd, name, must_exist=True)
        content = path.read_text(encoding="utf-8")
        if "def main(" in content and '__name__ == "__main__"' in content:
            runner.run(
                [str(service.python), "-m", module, "--help"],
                cwd=cwd, user=service.user,
            )


def preflight(
    args: argparse.Namespace,
    runner: Runner,
    *,
    operational_checks: bool = True,
    target_ref: str | None = None,
) -> DeploymentContext:
    """``target_ref`` is bootstrap-existing-component's explicit, hash-pinned
    deployment authority (see ``fetch_bootstrap_target_ref``): when given,
    --target-sha is resolved and fetched against that ref instead of the tip
    of --branch, and the branch tip is never consulted or used as a
    fallback. Every other caller passes ``target_ref=None`` and keeps the
    exact behaviour this function always had."""
    commands = ["systemctl", "getent", "git", "runuser", "sha256sum"]
    if operational_checks:
        commands.extend(["nginx", "openssl", "curl"])
    require_commands(*commands)
    service = discover_service(runner, args.service_unit)
    cwd = service.working_directory
    validate_token(args.remote, "Git remote")
    validate_token(args.branch, "Git branch")
    repository = Path(
        runner.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, user=service.user)
        .stdout.strip()
    ).resolve()
    if repository != cwd.resolve():
        raise DeploymentError("systemd WorkingDirectory is not the repository root")
    branch = runner.run(
        ["git", "branch", "--show-current"], cwd=cwd, user=service.user
    ).stdout.strip()
    head = runner.run(["git", "rev-parse", "HEAD"], cwd=cwd, user=service.user).stdout.strip()
    if branch != args.branch or head != args.old_sha:
        raise DeploymentError(
            f"Git gate failed: branch={branch!r}, head={head!r}"
        )
    if target_ref is not None:
        fetch_bootstrap_target_ref(
            runner, cwd, remote=args.remote, target_ref=target_ref,
            target_sha=args.target_sha, user=service.user,
        )
    old_sha = resolve_commit(runner, cwd, args.old_sha, service.user)
    target_sha = resolve_commit(runner, cwd, args.target_sha, service.user)
    remotes = git_lines(runner, cwd, "remote", user=service.user)
    if args.remote not in remotes:
        raise DeploymentError("Configured remote does not exist in production repository")
    upstream_remote = runner.run(
        ["git", "config", "--get", f"branch.{branch}.remote"],
        cwd=cwd, user=service.user
    ).stdout.strip()
    upstream_merge = runner.run(
        ["git", "config", "--get", f"branch.{branch}.merge"],
        cwd=cwd, user=service.user
    ).stdout.strip()
    if upstream_remote != args.remote or upstream_merge != f"refs/heads/{branch}":
        raise DeploymentError("Approved remote/branch does not match the checked-out upstream")
    if target_ref is None:
        remote_line = runner.run(
            ["git", "ls-remote", "--exit-code", args.remote, f"refs/heads/{branch}"],
            cwd=cwd, user=service.user,
        ).stdout.strip().split()
        if len(remote_line) != 2 or remote_line[0].lower() != target_sha:
            raise DeploymentError("Remote target does not match approved target SHA")
    require_fast_forward(runner, cwd, old_sha, target_sha, service.user)
    approved_commits = git_lines(
        runner, cwd, "rev-list", "--reverse", f"{old_sha}..{target_sha}",
        user=service.user,
    )
    expected_commits = [
        resolve_commit(runner, cwd, oid, service.user)
        for oid in getattr(args, "expected_commit", [])
    ]
    require_exact_commit_sequence(approved_commits, expected_commits)

    changed_files = git_lines(
        runner, cwd, "diff", "--name-only", f"{old_sha}..{target_sha}", user=service.user
    )
    preexisting_modified_paths: list[str] = []
    new_paths: list[str] = []
    deleted_paths: list[str] = []
    materialized_path_metadata_before: dict[str, dict[str, object]] = {}
    for name in changed_files:
        in_old = git_path_exists(runner, cwd, old_sha, name, service.user)
        in_target = git_path_exists(runner, cwd, target_sha, name, service.user)
        if in_old and in_target:
            preexisting_modified_paths.append(name)
        elif not in_old and in_target:
            new_paths.append(name)
        elif in_old and not in_target:
            deleted_paths.append(name)
        else:
            raise DeploymentError(f"Ambiguous approved-range path classification: {name}")
        if in_old:
            path = safe_repo_path(cwd, name, must_exist=True)
            metadata = filesystem_metadata(path)
            if metadata["symlink"]:
                raise DeploymentError(f"Pre-merge approved path is a symlink: {name}")
            materialized_path_metadata_before[name] = metadata
    runtime_files = git_lines(runner, cwd, "diff", "--name-only", user=service.user)
    intersections = changed_runtime_intersections(changed_files, runtime_files)
    if intersections:
        raise DeploymentError(
            "Target/runtime intersection: " + ", ".join(intersections)
        )
    runtime_hashes = {
        name: sha256_file(safe_repo_path(cwd, name, must_exist=True))
        for name in runtime_files
        if safe_repo_path(cwd, name).is_file()
    }
    runtime_metadata = {}
    for name in runtime_hashes:
        path = safe_repo_path(cwd, name, must_exist=True)
        info = path.stat()
        runtime_metadata[name] = {
            "sha256": runtime_hashes[name],
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
        }

    if operational_checks:
        _, baseline_warning_codes = run_manage_check(runner, service)
        nginx_target = discover_and_validate_nginx(runner, service)
        preflight_readiness = validate_readiness_layers(runner, service, nginx_target)
        baseline_smoke = {
            "/": smoke_request(runner, nginx_target, "/"),
            "/app/": smoke_request(runner, nginx_target, "/app/"),
            "/admin/login/": preflight_readiness["application"],
        }
    else:
        baseline_warning_codes = set()
        nginx_target = NginxTarget("bootstrap.invalid", 443, "", None)
        preflight_readiness = {}
        baseline_smoke = {}
    return DeploymentContext(
        service=service,
        nginx=nginx_target,
        old_sha=old_sha,
        target_sha=target_sha,
        repository=repository,
        branch=branch,
        remote=args.remote,
        changed_files=changed_files,
        runtime_files=runtime_files,
        intersections=intersections,
        runtime_hashes=runtime_hashes,
        baseline_smoke=baseline_smoke,
        baseline_warning_codes=baseline_warning_codes,
        preflight_readiness=preflight_readiness,
        approved_commits=approved_commits,
        runtime_metadata=runtime_metadata,
        materialized_path_metadata_before=materialized_path_metadata_before,
        preexisting_modified_paths=preexisting_modified_paths,
        new_paths=new_paths,
        deleted_paths=deleted_paths,
    )


def write_backup(
    args: argparse.Namespace, runner: Runner, context: DeploymentContext
) -> Path:
    if not args.backup_root:
        raise DeploymentError("--backup-root is required with --execute")
    backup_root = Path(args.backup_root)
    if not backup_root.is_absolute() or not backup_root.is_dir() or backup_root.is_symlink():
        raise DeploymentError("Backup root must be an existing absolute non-symlink directory")
    backup_root = backup_root.resolve()
    backup = Path(tempfile.mkdtemp(prefix="deployment-", dir=backup_root))
    backup.chmod(0o700)
    cwd = context.service.working_directory

    (backup / "manifest.json").write_text(
        json.dumps(
            {
                "old_sha": context.old_sha,
                "target_sha": context.target_sha,
                "service": dataclasses.asdict(context.service),
                "nginx": dataclasses.asdict(context.nginx),
                "changed_files": context.changed_files,
                "runtime_files": context.runtime_files,
                "runtime_hashes": context.runtime_hashes,
                "baseline_smoke": context.baseline_smoke,
            },
            default=str,
            indent=2,
        ),
        encoding="utf-8",
    )
    (backup / "git-status.txt").write_text(
        runner.run(["git", "status", "--short"], cwd=cwd).stdout, encoding="utf-8"
    )
    (backup / "git-diff.patch").write_text(
        runner.run(["git", "diff", "--binary"], cwd=cwd).stdout, encoding="utf-8"
    )
    runtime_root = backup / "runtime"
    for name in context.runtime_files:
        source = safe_repo_path(cwd, name)
        if source.is_file():
            destination = runtime_root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    for index, environment_file in enumerate(context.service.environment_files):
        if not environment_file.path.is_file():
            continue
        destination = backup / "environment" / f"{index:02d}.env"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(environment_file.path, destination)
        destination.chmod(0o600)
    shutil.copy2(context.service.fragment_path, backup / "service-unit")

    checksums = []
    for path in sorted(item for item in backup.rglob("*") if item.is_file()):
        checksums.append(f"{sha256_file(path)}  {path.relative_to(backup)}")
    (backup / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    for path in backup.rglob("*"):
        if path.is_file() and "environment" not in path.parts:
            path.chmod(0o600)
    return backup


def targeted_rollback(
    args: argparse,
    runner: Runner,
    context: DeploymentContext,
    restarted_units: list[str],
) -> None:
    cwd = context.service.working_directory
    user = context.service.user
    if not user:
        raise DeploymentError("Rollback refused: service user is empty")
    head = runner.run(["git", "rev-parse", "HEAD"], cwd=cwd, user=user).stdout.strip()
    approved_commits = context.approved_commits or git_lines(
        runner, cwd, "rev-list", "--reverse",
        f"{context.old_sha}..{context.target_sha}", user=user,
    )
    allowed_heads = {context.old_sha, *approved_commits}
    if head not in allowed_heads:
        raise DeploymentError("Rollback refused: HEAD is outside the approved range")

    def paths_match(commit: str) -> bool:
        for cached in (False, True):
            command = ["git", "diff", "--quiet"]
            if cached:
                command.append("--cached")
            command.extend([commit, "--", *context.changed_files])
            if runner.run(command, cwd=cwd, user=user, check=False).returncode:
                return False
        return True

    if not paths_match(context.old_sha):
        restorable_paths = []
        for name in context.changed_files:
            in_old = runner.run(
                ["git", "cat-file", "-e", f"{context.old_sha}:{name}"],
                cwd=cwd, user=user, check=False,
            ).returncode == 0
            in_index = runner.run(
                ["git", "ls-files", "--error-unmatch", "--", name],
                cwd=cwd, user=user, check=False,
            ).returncode == 0
            if in_old or in_index:
                restorable_paths.append(name)
        if restorable_paths:
            run_git_materializing(
                runner,
                [
                    "git", "restore", "--source", context.old_sha,
                    "--staged", "--worktree", "--", *restorable_paths,
                ],
                cwd=cwd, user=user,
            )
    if head != context.old_sha:
        runner.run(
            ["git", "update-ref", f"refs/heads/{context.branch}",
             context.old_sha, head],
            cwd=cwd, user=user,
        )
    if not paths_match(context.old_sha):
        raise DeploymentError("Rollback did not restore the exact old Git tree")
    for name, expected in context.materialized_path_metadata_before.items():
        path = safe_repo_path(cwd, name, must_exist=True)
        actual = filesystem_metadata(path)
        if actual["symlink"] or actual["type"] != expected.get("type"):
            raise DeploymentError(f"Rollback metadata type mismatch: {name}")
        if actual["mode"] != expected.get("mode"):
            runner.run(
                ["chmod", f"{int(expected['mode']):04o}", str(path)], user=user
            )
        if actual["gid"] != expected.get("gid"):
            group_name = str(expected.get("group", ""))
            if not group_name:
                raise DeploymentError(f"Rollback metadata group missing: {name}")
            runner.run(["chgrp", group_name, str(path)], user=user)
        restored = filesystem_metadata(path)
        for field in ("type", "uid", "gid", "owner", "group", "mode", "symlink"):
            if restored.get(field) != expected.get(field):
                raise DeploymentError(f"Rollback metadata restoration incomplete: {name}")
    for name, expected in context.runtime_hashes.items():
        path = safe_repo_path(cwd, name, must_exist=True)
        if sha256_file(path) != expected:
            raise DeploymentError(f"Runtime changed during rollback: {name}")
        expected_metadata = context.runtime_metadata.get(name)
        if expected_metadata:
            info = path.stat()
            actual_metadata = {
                "sha256": expected,
                "mode": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
            }
            if actual_metadata != expected_metadata:
                raise DeploymentError(f"Runtime metadata changed during rollback: {name}")
    for unit in restarted_units:
        runner.run(["systemctl", "restart", unit])


def execute_deployment(
    args: argparse, runner: Runner, context: DeploymentContext
) -> dict[str, object]:
    backup = write_backup(args, runner, context)
    cwd = context.service.working_directory
    restarted_units: list[str] = []
    mutation_started = False
    previous_handlers: dict[int, object] = {}

    def interrupt(signum: int, _frame: object) -> None:
        raise DeploymentInterrupted(f"Received signal {signum}")

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, interrupt)
    try:
        mutation_started = True
        require_deployment_ref(
            runner, cwd, remote=context.remote, branch=context.branch,
            target_sha=context.target_sha, user=context.service.user,
        )
        run_git_materializing(
            runner, ["git", "merge", "--ff-only", context.target_sha],
            cwd=cwd, user=context.service.user,
        )
        head = runner.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, user=context.service.user
        ).stdout.strip()
        if head != context.target_sha:
            raise DeploymentError("HEAD does not match target after fast-forward")
        for cached in (False, True):
            command = ["git", "diff", "--quiet"]
            if cached:
                command.append("--cached")
            command.extend([context.target_sha, "--", *context.changed_files])
            if runner.run(
                command, cwd=cwd, user=context.service.user, check=False
            ).returncode:
                raise DeploymentError("Working tree is partially materialized")
        current_runtime = git_lines(
            runner, cwd, "diff", "--name-only", user=context.service.user
        )
        if current_runtime != context.runtime_files:
            raise DeploymentError("Post-merge runtime file set changed")
        for name, expected in context.runtime_hashes.items():
            if sha256_file(safe_repo_path(cwd, name, must_exist=True)) != expected:
                raise DeploymentError(f"Runtime SHA256 changed after merge: {name}")
        for name in context.changed_files:
            path = safe_repo_path(cwd, name)
            if path.exists() and runner.run(
                ["test", "-O", str(path)], user=context.service.user, check=False
            ).returncode:
                raise DeploymentError(f"Changed path has unexpected ownership: {name}")

        migrations = [name for name in context.changed_files if "/migrations/" in name]
        static_changes = [
            name
            for name in context.changed_files
            if name.startswith(("static/", "config/static/"))
        ]
        if migrations or static_changes:
            raise DeploymentError(
                "Diff requires an explicit migration/static deployment plan"
            )

        run_manage_check(runner, context.service)
        deploy_output, _ = run_manage_check(runner, context.service, deploy=True)
        allowed = set(args.allowed_warning)
        new_codes = unexpected_warning_codes(deploy_output, allowed)
        if new_codes:
            raise DeploymentError(
                "Unexpected deploy warning codes: " + ", ".join(sorted(new_codes))
            )

        restart_classification = classify_restart_requirement(context.changed_files)
        if restart_classification == "web_runtime_required":
            if not args.restart_web:
                raise DeploymentError("--restart-web is required for an executable deployment")
            restarted_units.append(context.service.unit)
            runner.run(["systemctl", "restart", context.service.unit])
            for unit in args.restart_unit:
                restarted_units.append(unit)
                runner.run(["systemctl", "restart", unit])

            for unit in restarted_units:
                state = runner.run(["systemctl", "is-active", unit]).stdout.strip()
                if state != "active":
                    raise DeploymentError(f"Restarted unit is not active: {unit}")
        else:
            # "ops_only_no_restart": the classification -- not --restart-web
            # -- is authoritative. No systemctl restart is issued here under
            # any condition, even if the operator passed --restart-web.
            post_merge_pid = current_main_pid(runner, context.service.unit)
            if post_merge_pid != context.service.main_pid:
                raise DeploymentError(
                    "unexpected_restart_detected: "
                    f"{context.service.unit} MainPID changed from "
                    f"{context.service.main_pid} to {post_merge_pid} during an "
                    "ops-only deployment that must not restart the web service"
                )
            state = runner.run(
                ["systemctl", "is-active", context.service.unit]
            ).stdout.strip()
            if state != "active":
                raise DeploymentError(
                    f"Service unit is not active: {context.service.unit}"
                )
            verify_ops_only_changed_modules(
                runner, context.service, context.changed_files
            )

        readiness = wait_for_application_ready(
            runner,
            context,
            timeout_seconds=args.readiness_timeout,
            poll_interval=args.readiness_poll_interval,
        )
        smoke = {
            "/": smoke_request(runner, context.nginx, "/"),
            "/app/": smoke_request(runner, context.nginx, "/app/"),
            "GET /relay/send/": smoke_request(
                runner, context.nginx, "/relay/send/"
            ),
            "POST /relay/send/": smoke_request(
                runner, context.nginx, "/relay/send/", method="POST"
            ),
        }
        if smoke["GET /relay/send/"]["status"] != "405":
            raise DeploymentError("GET /relay/send/ did not return 405")
        if smoke["POST /relay/send/"]["status"] != "403":
            raise DeploymentError("Anonymous POST /relay/send/ did not return 403")
        for path in ("/", "/app/"):
            if smoke[path]["status"] != context.baseline_smoke[path]["status"]:
                raise DeploymentError(f"Smoke baseline changed for {path}")
        for name, expected in context.runtime_hashes.items():
            if sha256_file(cwd / name) != expected:
                raise DeploymentError(f"Runtime SHA256 changed: {name}")

        return {
            "backup": str(backup),
            "head": head,
            "restart_classification": restart_classification,
            "restarted_units": restarted_units,
            "readiness": readiness,
            "smoke": smoke,
            "migrations_executed": False,
            "collectstatic_executed": False,
        }
    except BaseException as original:
        if mutation_started:
            for signum in previous_handlers:
                signal.signal(signum, signal.SIG_IGN)
            try:
                targeted_rollback(args, runner, context, restarted_units)
            except BaseException as rollback_error:
                raise DeploymentError(
                    "DEPLOYMENT_FAILED; ROLLBACK_INCOMPLETE: "
                    f"{redact_output(str(rollback_error))}"
                ) from original
            raise DeploymentError(
                "DEPLOYMENT_FAILED; ROLLBACK_COMPLETED: "
                f"{redact_output(str(original))}"
            ) from original
        raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def deployment_plan(args: argparse.Namespace, context: DeploymentContext) -> dict[str, object]:
    restart_classification = classify_restart_requirement(context.changed_files)
    restart_units = (
        [context.service.unit, *args.restart_unit]
        if restart_classification == "web_runtime_required"
        else []
    )
    return {
        "mode": "execute" if args.execute else "read-only",
        "repository": str(context.repository),
        "branch": context.branch,
        "remote": context.remote,
        "OLD_COMMIT": context.old_sha,
        "TARGET_COMMIT": context.target_sha,
        "approved_commits": context.approved_commits,
        "changed_files": context.changed_files,
        "runtime_files": context.runtime_files,
        "intersections": context.intersections,
        "python": str(context.service.python),
        "service_user": context.service.user,
        "web_unit": context.service.unit,
        "vhost": context.nginx.server_name,
        "preflight_readiness": context.preflight_readiness,
        "restart_units": restart_units,
        "restart_classification": restart_classification,
        "steps": [
            "Preflight: discovery and environment validation",
            "Preflight: current-code manage.py check",
            "Preflight: Nginx validation and baseline smoke tests",
            "Backup approved state",
            "Fast-forward with git merge --ff-only",
            "Post-update: manage.py check and check --deploy",
            "Post-update: restart only approved units",
            "Post-update: wait for service, socket, and baseline HTTP readiness",
            "Post-update: smoke tests and runtime SHA256 verification",
            "Targeted rollback on any post-mutation failure",
        ],
    }


def bootstrap_module_deployment(
    args: argparse.Namespace, runner: Runner, module_name: str
) -> dict[str, object]:
    """One-time, data-free installation of a versioned operations module.

    This deliberately stops after the exact fast-forward and import check.  It is
    not a general deployment path and performs no backup, restart, migration,
    collectstatic, readiness, smoke test, or application write.
    """
    if not re.fullmatch(r"ops(?:\.[A-Za-z_][A-Za-z0-9_]*)+", module_name):
        raise DeploymentError("Unsafe bootstrap module name")
    service = discover_service(runner, args.service_unit)
    cwd, user = service.working_directory, service.user
    before = snapshot_git_state(runner, cwd, args.remote, user)
    if before.head != args.old_sha or before.branch != args.branch:
        raise DeploymentError("Bootstrap initial HEAD or branch mismatch")
    remote = runner.run(
        ["git", "ls-remote", "--exit-code", args.remote, f"refs/heads/{args.branch}"],
        cwd=cwd, user=user,
    ).stdout.split()
    if not remote or remote[0] != args.target_sha:
        raise DeploymentError("Bootstrap remote target mismatch")
    assert_git_state_unchanged(runner, cwd, args.remote, user, before)
    module_path = module_name.replace(".", "/") + ".py"
    old_has_module = runner.run(
        ["git", "cat-file", "-e", f"{args.old_sha}:{module_path}"],
        cwd=cwd, user=user, check=False,
    ).returncode == 0
    if old_has_module:
        raise DeploymentError("Bootstrap is only allowed for first module installation")
    refresh_deployment_ref(
        runner, cwd, remote=args.remote, branch=args.branch,
        target_sha=args.target_sha, user=user,
    )
    context = preflight(args, runner, operational_checks=False)
    if module_path not in context.changed_files:
        raise DeploymentError("Bootstrap target does not introduce requested module")
    mutation_started = False
    try:
        mutation_started = True
        run_git_materializing(
            runner, ["git", "merge", "--ff-only", args.target_sha], cwd=cwd, user=user,
        )
        head = validate_bootstrap_post_merge(
            runner, context, module_name=module_name
        )
        return {
            "phase": "bootstrap-complete",
            "head": head,
            "module": module_name,
            "path_classification": {
                "preexisting_modified": context.preexisting_modified_paths,
                "new": context.new_paths,
                "deleted": context.deleted_paths,
            },
            "materialized_path_metadata_before": (
                context.materialized_path_metadata_before
            ),
        }
    except BaseException:
        if mutation_started:
            targeted_rollback(args, runner, context, [])
        raise


def validate_bootstrap_post_merge(
    runner: Runner,
    context: DeploymentContext,
    *,
    module_name: str,
) -> str:
    """Validate the materialized target without re-running old-HEAD preflight."""
    cwd, user = context.service.working_directory, context.service.user
    if not user:
        raise DeploymentError("Post-merge service user is empty")
    if user != "app":
        raise DeploymentError("Bootstrap post-merge service user must be app")
    head = runner.run(["git", "rev-parse", "HEAD"], cwd=cwd, user=user).stdout.strip()
    branch = runner.run(
        ["git", "branch", "--show-current"], cwd=cwd, user=user
    ).stdout.strip()
    if head != context.target_sha:
        raise DeploymentError("Bootstrap post-merge HEAD is not target")
    if branch != context.branch:
        raise DeploymentError("Bootstrap post-merge branch mismatch")
    if runner.run(["git", "ls-files", "-u"], cwd=cwd, user=user).stdout.strip():
        raise DeploymentError("Bootstrap post-merge has unmerged paths")
    if runner.run(
        ["git", "diff", "--cached", "--quiet"], cwd=cwd, user=user, check=False
    ).returncode:
        raise DeploymentError("Bootstrap post-merge has staged changes")
    for cached in (False, True):
        command = ["git", "diff", "--quiet"]
        if cached:
            command.append("--cached")
        command.extend([context.target_sha, "--", *context.changed_files])
        if runner.run(command, cwd=cwd, user=user, check=False).returncode:
            raise DeploymentError("Bootstrap working tree is partially materialized")
    current_runtime = git_lines(runner, cwd, "diff", "--name-only", user=user)
    if current_runtime != context.runtime_files:
        raise DeploymentError("Bootstrap post-merge runtime file set changed")
    if git_lines(runner, cwd, "ls-files", "--others", "--exclude-standard", user=user):
        raise DeploymentError("Bootstrap post-merge has untracked paths")
    for name, expected in context.runtime_hashes.items():
        path = safe_repo_path(cwd, name, must_exist=True)
        if sha256_file(path) != expected:
            raise DeploymentError(f"Runtime changed during bootstrap: {name}")
        info = path.stat()
        actual = {
            "sha256": expected,
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
        }
        if actual != context.runtime_metadata.get(name):
            raise DeploymentError(f"Runtime metadata changed during bootstrap: {name}")
    classified = set(
        context.preexisting_modified_paths + context.new_paths + context.deleted_paths
    )
    classification_count = sum(map(len, (
        context.preexisting_modified_paths, context.new_paths, context.deleted_paths
    )))
    if classified != set(context.changed_files) or classification_count != len(classified):
        raise DeploymentError("Bootstrap path classification is missing or incomplete")
    for name in context.deleted_paths:
        path = safe_repo_path(cwd, name)
        in_target = git_path_exists(runner, cwd, context.target_sha, name, user)
        if in_target or path.exists() or path.is_symlink():
            raise DeploymentError(f"Bootstrap deleted path remains materialized: {name}")
    for name in context.preexisting_modified_paths:
        path = safe_repo_path(cwd, name, must_exist=True)
        if not git_path_exists(runner, cwd, context.target_sha, name, user):
            raise DeploymentError(f"Bootstrap path materialization mismatch: {name}")
        actual = filesystem_metadata(path)
        baseline = context.materialized_path_metadata_before.get(name)
        required = {"exists", "type", "uid", "gid", "owner", "group", "mode", "symlink"}
        if not baseline or not required.issubset(baseline):
            raise DeploymentError(f"Bootstrap pre-merge metadata missing: {name}")
        old_git_mode = git_path_mode(runner, cwd, context.old_sha, name, user)
        target_git_mode = git_path_mode(runner, cwd, context.target_sha, name, user)
        expected_mode = int(baseline["mode"])
        if bool(old_git_mode & 0o111) != bool(target_git_mode & 0o111):
            expected_mode = (
                expected_mode | 0o111
                if target_git_mode & 0o111
                else expected_mode & ~0o111
            )
        for field in ("type", "uid", "gid", "symlink"):
            if actual.get(field) != baseline.get(field):
                raise DeploymentError(f"Bootstrap preexisting metadata changed: {name}")
        if actual["mode"] != expected_mode:
            raise DeploymentError(f"Bootstrap preexisting metadata changed: {name}")
    try:
        expected_uid = pwd.getpwnam("app").pw_uid if pwd is not None else None
        allowed_gids = (
            {grp.getgrnam(name).gr_gid for name in ("app", "www-data")}
            if grp is not None else None
        )
    except KeyError as exc:
        raise DeploymentError("Bootstrap approved service ownership identity is missing") from exc
    for name in context.new_paths:
        path = safe_repo_path(cwd, name, must_exist=True)
        if not git_path_exists(runner, cwd, context.target_sha, name, user):
            raise DeploymentError(f"Bootstrap path materialization mismatch: {name}")
        actual = filesystem_metadata(path)
        info = path.stat(follow_symlinks=False)
        if (
            actual["symlink"]
            or (expected_uid is not None and info.st_uid != expected_uid)
            or (allowed_gids is not None and info.st_gid not in allowed_gids)
        ):
            raise DeploymentError(f"Bootstrap materialized unsafe ownership: {name}")
        git_executable = bool(
            git_path_mode(runner, cwd, context.target_sha, name, user) & 0o111
        )
        actual_mode = stat.S_IMODE(info.st_mode)
        expected_mode = 0o755 if git_executable else 0o644
        if pwd is not None and actual_mode != expected_mode:
            raise DeploymentError(f"Bootstrap materialized unsafe permissions: {name}")
    module_path = module_name.replace(".", "/") + ".py"
    safe_repo_path(cwd, module_path, must_exist=True)
    runner.run(
        [str(context.service.python), "-c", f"import {module_name}; "
         "from ops.td02c_deployment_runner import _django_state; _django_state()"],
        cwd=cwd, user=user,
    )
    return head


_BOOTSTRAP_EXISTING_PATH = re.compile(r"ops/[A-Za-z0-9_][A-Za-z0-9_./-]*")


def _validate_bootstrap_existing_paths(authorized_paths: tuple[str, ...]) -> None:
    if not authorized_paths:
        raise DeploymentError(
            "Bootstrap-existing-component requires at least one authorized path"
        )
    seen: set[str] = set()
    for path in authorized_paths:
        if (
            not _BOOTSTRAP_EXISTING_PATH.fullmatch(path)
            or ".." in path.split("/")
            or path in seen
        ):
            raise DeploymentError(f"Unsafe or duplicated bootstrap-existing path: {path!r}")
        seen.add(path)


def _load_bootstrap_evidence(path: Path | None) -> object:
    from ops.deployment_test_profile import BootstrapEvidence

    if path is None or not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise DeploymentError("Bootstrap evidence path is unsafe or missing")
    info = path.stat(follow_symlinks=False)
    if os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600:
        raise DeploymentError("Bootstrap evidence file has unsafe permissions")
    if info.st_nlink != 1:
        raise DeploymentError("Bootstrap evidence file has unexpected hard links")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise DeploymentError("Bootstrap evidence file has an unexpected owner")
    data = json.loads(path.read_text(encoding="utf-8"))
    allowed = {field.name for field in dataclasses.fields(BootstrapEvidence)}
    if set(data) != allowed:
        raise DeploymentError("Bootstrap evidence schema is invalid")
    values = dict(data)
    values["commit_sequence"] = tuple(values["commit_sequence"])
    values["authorized_paths"] = tuple(values["authorized_paths"])
    return BootstrapEvidence(**values)


def _validate_bootstrap_operational_gates(
    runner: Runner, service: ServiceMetadata, worker_unit: str
) -> None:
    """Read-only jobs/V2/ledger/settings/worker check for bootstrap-existing.

    Runs out-of-process (a fresh ``python -c`` invocation, the same pattern
    already used by validate_bootstrap_post_merge) so this module never
    imports ops.td02c_deployment_runner at module load time -- that module
    already imports this one, and a top-level cross-import would create a
    circular dependency.
    """
    validate_token(worker_unit, "systemd unit")
    script = (
        "from ops.td02c_deployment_runner import _django_state, _worker_snapshot; "
        "from ops.td02c_worker_gate import evaluate_worker_precondition; "
        "from ops.deployment_hardening import Runner; "
        "_, jobs, v2, ledger = _django_state(); "
        "assert (jobs, v2, ledger) == (0, 0, 0), (jobs, v2, ledger); "
        f"snapshot = _worker_snapshot(Runner(), {worker_unit!r}, jobs); "
        "result = evaluate_worker_precondition(snapshot); "
        "assert result.allowed, (result.classification, result.reasons)"
    )
    runner.run([str(service.python), "-c", script], cwd=service.working_directory, user=service.user)


def _validate_new_evidence_gate_operational(runner: Runner, service: ServiceMetadata) -> None:
    """Prove the just-installed evidence gate is live, not merely importable.

    Builds evidence exactly at the module's own MINIMUM_* floors and asserts
    it is accepted.  Every floor here is above the old stale exact-match
    snapshot (http_client_passed == 8), so this can only pass if the fixed,
    floor-based gate is the one actually executing after the merge.
    """
    dummy_sha = "0" * 40
    script = (
        "from ops.deployment_test_profile import ("
        "ValidationEvidence, validate_predeployment_evidence, "
        "MINIMUM_API_V2_PASSED, MINIMUM_HTTP_CLIENT_PASSED, MINIMUM_OPS_PASSED); "
        f"sha = {dummy_sha!r}; "
        "evidence = ValidationEvidence(target_sha=sha, commit_sequence=(sha,), "
        "api_v2_passed=MINIMUM_API_V2_PASSED, http_client_passed=MINIMUM_HTTP_CLIENT_PASSED, "
        "ops_passed=MINIMUM_OPS_PASSED, linux_repetitions_passed=True, postgresql_major=17); "
        "validate_predeployment_evidence(evidence, target_sha=sha, expected_commits=(sha,))"
    )
    runner.run([str(service.python), "-c", script], cwd=service.working_directory, user=service.user)


def bootstrap_existing_component_deployment(
    args: argparse.Namespace, runner: Runner, authorized_paths: tuple[str, ...],
) -> dict[str, object]:
    """One-time, narrowly-scoped update for existing operational components
    whose currently-installed code blocks validating its own fix (for
    example a predeployment evidence gate with a stale hardcoded threshold).

    Unlike bootstrap_module_deployment (first-time module installation
    only), this updates files that already exist in old_sha, restricted to
    an explicit closed allowlist. It never runs the normal predeployment
    evidence gate from ops.td02c_deployment_runner, which stays the only
    path for ordinary deployments. This is not a general deployment path:
    no restart, migration, collectstatic, or application write occurs here.

    ``--bootstrap-skip-operational-checks`` exists only for installing a
    component that the operational checks themselves depend on (for
    example this very nginx-check mechanism, before its sudoers rule can
    exist): it skips nginx discovery, readiness, and baseline smoke in
    preflight and post-merge, but never the Git/evidence/allowlist gates,
    never the jobs/V2/ledger/settings/worker check, and never `manage.py
    check` or the permitted test suites. The default keeps full operational
    checks, matching every other bootstrap-existing-component target.

    ``--target-ref`` is the explicit, hash-pinned deployment authority: an
    immutable remote ref (never the tip of --branch, never refs/remotes/*,
    never HEAD/FETCH_HEAD, never a wildcard) that must resolve to exactly
    --target-sha. This lets --branch keep advancing past the evidenced
    target (for example to carry ahead operational tooling this bootstrap
    itself depends on) without the branch tip ever being read, trusted, or
    used as an implicit fallback for what --target-sha is allowed to be.
    """
    _validate_bootstrap_existing_paths(authorized_paths)
    target_ref = getattr(args, "target_ref", None)
    if not target_ref:
        raise DeploymentError("Bootstrap-existing target-ref is required")
    service = discover_service(runner, args.service_unit)
    cwd, user = service.working_directory, service.user
    before = snapshot_git_state(runner, cwd, args.remote, user)
    if before.head != args.old_sha or before.branch != args.branch:
        raise DeploymentError("Bootstrap-existing initial HEAD or branch mismatch")
    assert_git_state_unchanged(runner, cwd, args.remote, user, before)

    from ops.deployment_test_profile import TestProfileError, validate_bootstrap_evidence

    evidence = _load_bootstrap_evidence(getattr(args, "bootstrap_evidence", None))
    try:
        validate_bootstrap_evidence(
            evidence,
            target_sha=args.target_sha,
            expected_commits=list(getattr(args, "expected_commit", [])),
            authorized_paths=authorized_paths,
        )
    except TestProfileError as exc:
        raise DeploymentError(str(exc)) from exc

    skip_operational_checks = bool(getattr(args, "bootstrap_skip_operational_checks", False))
    context = preflight(
        args, runner, operational_checks=not skip_operational_checks, target_ref=target_ref,
    )
    changed = set(context.changed_files)
    authorized = set(authorized_paths)
    unauthorized = sorted(changed - authorized)
    if unauthorized:
        raise DeploymentError(
            "Bootstrap-existing target modifies unauthorized paths: " + ", ".join(unauthorized)
        )
    missing = sorted(authorized - changed)
    if missing:
        raise DeploymentError(
            "Bootstrap-existing allowlist expects paths absent from the target range: "
            + ", ".join(missing)
        )
    if not context.changed_files:
        raise DeploymentError("Bootstrap-existing target introduces no changes")
    _validate_bootstrap_operational_gates(runner, service, args.worker_unit)

    mutation_started = False
    try:
        mutation_started = True
        run_git_materializing(
            runner, ["git", "merge", "--ff-only", args.target_sha], cwd=cwd, user=user,
        )
        head = validate_bootstrap_existing_post_merge(
            runner, context, authorized_paths=authorized_paths,
            allowed_warnings=set(getattr(args, "allowed_warning", [])),
            skip_operational_checks=skip_operational_checks,
        )
        return {
            "phase": "bootstrap-existing-complete",
            "head": head,
            "authorized_paths": list(authorized_paths),
            "path_classification": {
                "preexisting_modified": context.preexisting_modified_paths,
                "new": context.new_paths,
                "deleted": context.deleted_paths,
            },
            "materialized_path_metadata_before": (
                context.materialized_path_metadata_before
            ),
        }
    except BaseException:
        if mutation_started:
            targeted_rollback(args, runner, context, [])
        raise


def validate_bootstrap_existing_post_merge(
    runner: Runner,
    context: DeploymentContext,
    *,
    authorized_paths: tuple[str, ...],
    allowed_warnings: set[str] = frozenset(),
    skip_operational_checks: bool = False,
) -> str:
    """Validate the materialized bootstrap-existing target and prove the
    fixed gate is live, without re-running old-HEAD preflight or the normal
    predeployment evidence gate."""
    cwd, user = context.service.working_directory, context.service.user
    if not user:
        raise DeploymentError("Bootstrap-existing post-merge service user is empty")
    if user != "app":
        raise DeploymentError("Bootstrap-existing post-merge service user must be app")
    head = runner.run(["git", "rev-parse", "HEAD"], cwd=cwd, user=user).stdout.strip()
    branch = runner.run(
        ["git", "branch", "--show-current"], cwd=cwd, user=user
    ).stdout.strip()
    if head != context.target_sha:
        raise DeploymentError("Bootstrap-existing post-merge HEAD is not target")
    if branch != context.branch:
        raise DeploymentError("Bootstrap-existing post-merge branch mismatch")
    if runner.run(["git", "ls-files", "-u"], cwd=cwd, user=user).stdout.strip():
        raise DeploymentError("Bootstrap-existing post-merge has unmerged paths")
    if runner.run(
        ["git", "diff", "--cached", "--quiet"], cwd=cwd, user=user, check=False
    ).returncode:
        raise DeploymentError("Bootstrap-existing post-merge has staged changes")
    for cached in (False, True):
        command = ["git", "diff", "--quiet"]
        if cached:
            command.append("--cached")
        command.extend([context.target_sha, "--", *context.changed_files])
        if runner.run(command, cwd=cwd, user=user, check=False).returncode:
            raise DeploymentError("Bootstrap-existing working tree is partially materialized")
    current_runtime = git_lines(runner, cwd, "diff", "--name-only", user=user)
    if current_runtime != context.runtime_files:
        raise DeploymentError("Bootstrap-existing post-merge runtime file set changed")
    if git_lines(runner, cwd, "ls-files", "--others", "--exclude-standard", user=user):
        raise DeploymentError("Bootstrap-existing post-merge has untracked paths")
    for name, expected in context.runtime_hashes.items():
        path = safe_repo_path(cwd, name, must_exist=True)
        if sha256_file(path) != expected:
            raise DeploymentError(f"Runtime changed during bootstrap-existing: {name}")
        info = path.stat()
        actual = {
            "sha256": expected,
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
        }
        if actual != context.runtime_metadata.get(name):
            raise DeploymentError(f"Runtime metadata changed during bootstrap-existing: {name}")
    classified = set(
        context.preexisting_modified_paths + context.new_paths + context.deleted_paths
    )
    classification_count = sum(map(len, (
        context.preexisting_modified_paths, context.new_paths, context.deleted_paths
    )))
    if classified != set(context.changed_files) or classification_count != len(classified):
        raise DeploymentError("Bootstrap-existing path classification is missing or incomplete")
    for name in context.deleted_paths:
        path = safe_repo_path(cwd, name)
        in_target = git_path_exists(runner, cwd, context.target_sha, name, user)
        if in_target or path.exists() or path.is_symlink():
            raise DeploymentError(f"Bootstrap-existing deleted path remains materialized: {name}")
    for name in context.preexisting_modified_paths:
        path = safe_repo_path(cwd, name, must_exist=True)
        if not git_path_exists(runner, cwd, context.target_sha, name, user):
            raise DeploymentError(f"Bootstrap-existing path materialization mismatch: {name}")
        actual = filesystem_metadata(path)
        baseline = context.materialized_path_metadata_before.get(name)
        required = {"exists", "type", "uid", "gid", "owner", "group", "mode", "symlink"}
        if not baseline or not required.issubset(baseline):
            raise DeploymentError(f"Bootstrap-existing pre-merge metadata missing: {name}")
        old_git_mode = git_path_mode(runner, cwd, context.old_sha, name, user)
        target_git_mode = git_path_mode(runner, cwd, context.target_sha, name, user)
        expected_mode = int(baseline["mode"])
        if bool(old_git_mode & 0o111) != bool(target_git_mode & 0o111):
            expected_mode = (
                expected_mode | 0o111
                if target_git_mode & 0o111
                else expected_mode & ~0o111
            )
        for field in ("type", "uid", "gid", "symlink"):
            if actual.get(field) != baseline.get(field):
                raise DeploymentError(f"Bootstrap-existing preexisting metadata changed: {name}")
        if actual["mode"] != expected_mode:
            raise DeploymentError(f"Bootstrap-existing preexisting metadata changed: {name}")
    try:
        expected_uid = pwd.getpwnam("app").pw_uid if pwd is not None else None
        allowed_gids = (
            {grp.getgrnam(name).gr_gid for name in ("app", "www-data")}
            if grp is not None else None
        )
    except KeyError as exc:
        raise DeploymentError(
            "Bootstrap-existing approved service ownership identity is missing"
        ) from exc
    for name in context.new_paths:
        path = safe_repo_path(cwd, name, must_exist=True)
        if not git_path_exists(runner, cwd, context.target_sha, name, user):
            raise DeploymentError(f"Bootstrap-existing path materialization mismatch: {name}")
        actual = filesystem_metadata(path)
        info = path.stat(follow_symlinks=False)
        if (
            actual["symlink"]
            or (expected_uid is not None and info.st_uid != expected_uid)
            or (allowed_gids is not None and info.st_gid not in allowed_gids)
        ):
            raise DeploymentError(f"Bootstrap-existing materialized unsafe ownership: {name}")
        git_executable = bool(
            git_path_mode(runner, cwd, context.target_sha, name, user) & 0o111
        )
        actual_mode = stat.S_IMODE(info.st_mode)
        expected_mode = 0o755 if git_executable else 0o644
        if pwd is not None and actual_mode != expected_mode:
            raise DeploymentError(f"Bootstrap-existing materialized unsafe permissions: {name}")
    run_manage_check(runner, context.service)
    deploy_output, _ = run_manage_check(runner, context.service, deploy=True)
    new_codes = unexpected_warning_codes(deploy_output, allowed_warnings)
    if new_codes:
        raise DeploymentError(
            "Unexpected deploy warning codes: " + ", ".join(sorted(new_codes))
        )
    if not skip_operational_checks:
        validate_readiness_layers(runner, context.service, context.nginx)
    runner.run(
        [str(context.service.python), "-m", "unittest", "discover", "-s", "ops/tests", "-q"],
        cwd=cwd, user=user,
    )
    _validate_new_evidence_gate_operational(runner, context.service)
    return head


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-unit", required=True)
    parser.add_argument("--old-sha", required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--backup-root")
    parser.add_argument("--allowed-warning", action="append", default=[])
    parser.add_argument(
        "--expected-commit", action="append", default=[],
        help="Full commit ID expected in OLD..TARGET order; repeat for each commit.",
    )
    parser.add_argument("--restart-web", action="store_true")
    parser.add_argument("--restart-unit", action="append", default=[])
    parser.add_argument("--readiness-timeout", type=float, default=60.0)
    parser.add_argument("--readiness-poll-interval", type=float, default=0.25)
    parser.add_argument(
        "--fetch-target",
        action="store_true",
        help=(
            "Acquire the approved remote target into an isolated "
            "refs/deployment-preflight/<SHA> ref before read-only analysis."
        ),
    )
    parser.add_argument(
        "--refresh-deployment-ref",
        action="store_true",
        help=(
            "Explicitly update refs/remotes/<remote>/<branch> to the approved "
            "target and verify active HEAD/index/worktree remain unchanged."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply fast-forward after Preflight. Without this flag, run Preflight only.",
    )
    parser.add_argument(
        "--bootstrap-module",
        help="One-time limited installation of a new versioned ops module.",
    )
    parser.add_argument(
        "--worker-unit",
        default="doppler-background-jobs.service",
        help="Background worker systemd unit checked by bootstrap-existing-component.",
    )
    parser.add_argument(
        "--bootstrap-existing-component",
        action="append",
        default=[],
        help=(
            "Repeatable closed allowlist of already-existing ops/ paths this "
            "one-time bootstrap may update, for use only when the currently "
            "installed evidence gate blocks deploying its own fix."
        ),
    )
    parser.add_argument(
        "--bootstrap-evidence",
        type=Path,
        help="Path to a BootstrapEvidence JSON file bound to --target-sha.",
    )
    parser.add_argument(
        "--bootstrap-skip-operational-checks",
        action="store_true",
        help=(
            "Skip nginx discovery, readiness, and baseline smoke in "
            "bootstrap-existing-component's pre- and post-merge validation. "
            "Only for installing a component the operational checks "
            "themselves depend on (for example the nginx-check mechanism "
            "before its sudoers rule exists). Never skips Git/evidence/"
            "allowlist gates, the jobs/V2/ledger/settings/worker check, "
            "manage.py check, or the permitted test suites."
        ),
    )
    parser.add_argument(
        "--target-ref",
        help=(
            "Fully qualified remote ref (for example "
            "refs/heads/td02c-bootstrap-<target-sha>) that "
            "bootstrap-existing-component must resolve exactly to "
            "--target-sha, instead of the tip of --branch. Required for "
            "--bootstrap-existing-component: --branch identifies the local "
            "productive branch being updated, while --target-ref is the "
            "immutable authority for what --target-sha is allowed to be. "
            "Never a branch tip, never refs/remotes/*, never HEAD or "
            "FETCH_HEAD, never a wildcard."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = Runner()
    temporary_ref: str | None = None
    acquisition_service: ServiceMetadata | None = None
    try:
        if args.bootstrap_module:
            if args.execute or args.fetch_target or args.refresh_deployment_ref:
                raise DeploymentError("Bootstrap cannot be combined with other mutation modes")
            report = bootstrap_module_deployment(args, runner, args.bootstrap_module)
            print(json.dumps(report, default=str, indent=2))
            return 0
        if args.bootstrap_existing_component:
            if args.execute or args.fetch_target or args.refresh_deployment_ref or args.bootstrap_module:
                raise DeploymentError(
                    "Bootstrap-existing-component cannot be combined with other mutation modes"
                )
            report = bootstrap_existing_component_deployment(
                args, runner, tuple(args.bootstrap_existing_component)
            )
            print(json.dumps(report, default=str, indent=2))
            return 0
        if args.fetch_target and args.refresh_deployment_ref:
            raise DeploymentError(
                "Choose either isolated preflight fetch or deployment-ref refresh"
            )
        if args.refresh_deployment_ref:
            acquisition_service = discover_service(runner, args.service_unit)
            refresh_deployment_ref(
                runner,
                acquisition_service.working_directory,
                remote=args.remote,
                branch=args.branch,
                target_sha=args.target_sha,
                user=acquisition_service.user,
            )
        if args.fetch_target:
            validate_token(args.remote, "Git remote")
            validate_token(args.branch, "Git branch")
            acquisition_service = discover_service(runner, args.service_unit)
            temporary_ref, _ = acquire_target_object(
                runner,
                acquisition_service.working_directory,
                remote=args.remote,
                branch=args.branch,
                target_sha=args.target_sha,
                user=acquisition_service.user,
            )
        context = preflight(args, runner)
        report: dict[str, object] = {
            "phase": "preflight",
            "deployment_plan": deployment_plan(args, context),
            "baseline_smoke": context.baseline_smoke,
        }
        if args.execute:
            report["post_update"] = execute_deployment(args, runner, context)
            report["phase"] = "post-update-complete"
        print(json.dumps(report, default=str, indent=2))
        return 0
    except (DeploymentError, KeyboardInterrupt) as exc:
        print(f"DEPLOYMENT_ABORTED: {exc}", file=sys.stderr)
        return 1
    finally:
        if temporary_ref and acquisition_service:
            try:
                delete_temporary_target_ref(
                    runner,
                    acquisition_service.working_directory,
                    temporary_ref,
                    args.target_sha,
                    acquisition_service.user,
                )
            except DeploymentError as cleanup_error:
                raise DeploymentError(
                    f"PREFLIGHT_REF_CLEANUP_REQUIRED: {cleanup_error}"
                ) from cleanup_error


if __name__ == "__main__":
    raise SystemExit(main())
