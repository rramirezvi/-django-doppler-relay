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
from datetime import datetime, timezone
from pathlib import Path


class DeploymentError(RuntimeError):
    pass


class DeploymentInterrupted(DeploymentError):
    pass


def redact_output(value: str) -> str:
    patterns = (
        r"(?im)^(\s*(?:SECRET_KEY|DOPPLER_RELAY_API_KEY|DATABASE_URL)\s*=).*$",
        r"(?im)^(\s*(?:Authorization|Cookie|Set-Cookie)\s*:).*$",
    )
    redacted = value
    for pattern in patterns:
        redacted = re.sub(pattern, r"\1[REDACTED]", redacted)
    return redacted[-8000:]


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
    ) -> subprocess.CompletedProcess[str]:
        command = list(args)
        if user:
            command = ["runuser", "-u", user, "--", *command]
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
        )
        if check and result.returncode:
            rendered = shlex.join(command)
            raise DeploymentError(
                f"Command failed ({result.returncode}): {rendered}\n"
                f"{redact_output(result.stdout)}"
            )
        return result


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
    return next(iter(unique.values()))


def discover_and_validate_nginx(runner: Runner, service: ServiceMetadata) -> NginxTarget:
    runner.run(["nginx", "-t"])
    nginx_config = runner.run(["nginx", "-T"]).stdout
    bind_path = application_bind_from_exec_start(service.exec_start_raw)
    target = discover_nginx_target(nginx_config, bind_path)
    if target.certificate:
        runner.run(
            [
                "openssl", "x509", "-in", str(target.certificate), "-noout",
                "-checkhost", target.server_name,
            ]
        )
    return target


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


def changed_runtime_intersections(
    changed_files: list[str], runtime_files: list[str]
) -> list[str]:
    return sorted(set(changed_files) & set(runtime_files))


def smoke_request(runner: Runner, target: NginxTarget, path: str, method: str = "GET") -> dict[str, str]:
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
    output = runner.run(args).stdout
    payload = output.split(marker, 1)[-1].strip()
    if not re.fullmatch(r"\d{3}", payload):
        raise DeploymentError("curl did not return a valid HTTP status")
    return {"method": method, "path": path, "status": payload}


def run_manage_check(
    runner: Runner, service: ServiceMetadata, *, deploy: bool = False
) -> tuple[str, set[str]]:
    args = [str(service.python), str(service.working_directory / "manage.py"), "check"]
    if deploy:
        args.append("--deploy")
    result = runner.run(args, cwd=service.working_directory, user=service.user)
    return result.stdout, warning_codes(result.stdout)


def preflight(args: argparse.Namespace, runner: Runner) -> DeploymentContext:
    require_commands(
        "systemctl", "getent", "git", "nginx", "openssl", "curl", "runuser", "sha256sum"
    )
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
    old_sha = resolve_commit(runner, cwd, args.old_sha, service.user)
    target_sha = resolve_commit(runner, cwd, args.target_sha, service.user)
    if branch != args.branch or head != args.old_sha:
        raise DeploymentError(
            f"Git gate failed: branch={branch!r}, head={head!r}"
        )
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
    remote_line = runner.run(
        ["git", "ls-remote", "--exit-code", args.remote, f"refs/heads/{branch}"],
        cwd=cwd, user=service.user,
    ).stdout.strip().split()
    if len(remote_line) != 2 or remote_line[0].lower() != target_sha:
        raise DeploymentError("Remote target does not match approved target SHA")
    require_fast_forward(runner, cwd, old_sha, target_sha, service.user)

    changed_files = git_lines(
        runner, cwd, "diff", "--name-only", f"{old_sha}..{target_sha}", user=service.user
    )
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

    _, baseline_warning_codes = run_manage_check(runner, service)
    nginx_target = discover_and_validate_nginx(runner, service)
    baseline_smoke = {
        path: smoke_request(runner, nginx_target, path)
        for path in ("/", "/app/", "/admin/login/")
    }
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
    head = runner.run(["git", "rev-parse", "HEAD"], cwd=cwd, user=user).stdout.strip()
    if head not in {context.old_sha, context.target_sha}:
        raise DeploymentError("Rollback refused: HEAD is neither old nor target commit")

    def paths_match(commit: str) -> bool:
        for cached in (False, True):
            command = ["git", "diff", "--quiet"]
            if cached:
                command.append("--cached")
            command.extend([commit, "--", *context.changed_files])
            if runner.run(command, cwd=cwd, user=user, check=False).returncode:
                return False
        return True

    if head == context.old_sha and paths_match(context.old_sha):
        return
    target_tree_present = paths_match(context.target_sha)
    old_tree_present = paths_match(context.old_sha)
    if head == context.target_sha and old_tree_present:
        runner.run(
            ["git", "update-ref", f"refs/heads/{context.branch}",
             context.old_sha, context.target_sha],
            cwd=cwd, user=user,
        )
    elif not target_tree_present:
        raise DeploymentError("Rollback refused: deployment paths are in an ambiguous state")
    else:
        runner.run(
            [
                "git", "restore", "--source", context.old_sha, "--staged", "--worktree",
                "--", *context.changed_files,
            ],
            cwd=cwd, user=user,
        )
        if head == context.target_sha:
            runner.run(
                ["git", "update-ref", f"refs/heads/{context.branch}",
                 context.old_sha, context.target_sha],
                cwd=cwd, user=user,
            )
    if not paths_match(context.old_sha):
        raise DeploymentError("Rollback did not restore the exact old Git tree")
    for name, expected in context.runtime_hashes.items():
        if sha256_file(safe_repo_path(cwd, name, must_exist=True)) != expected:
            raise DeploymentError(f"Runtime changed during rollback: {name}")
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
        runner.run(
            ["git", "merge", "--ff-only", context.target_sha],
            cwd=cwd, user=context.service.user,
        )
        head = runner.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, user=context.service.user
        ).stdout.strip()
        if head != context.target_sha:
            raise DeploymentError("HEAD does not match target after fast-forward")

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
            "restarted_units": restarted_units,
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
    restart_units = [context.service.unit, *args.restart_unit] if args.restart_web else []
    return {
        "mode": "execute" if args.execute else "read-only",
        "repository": str(context.repository),
        "branch": context.branch,
        "remote": context.remote,
        "OLD_COMMIT": context.old_sha,
        "TARGET_COMMIT": context.target_sha,
        "changed_files": context.changed_files,
        "runtime_files": context.runtime_files,
        "intersections": context.intersections,
        "python": str(context.service.python),
        "service_user": context.service.user,
        "web_unit": context.service.unit,
        "vhost": context.nginx.server_name,
        "restart_units": restart_units,
        "steps": [
            "Preflight: discovery and environment validation",
            "Preflight: current-code manage.py check",
            "Preflight: Nginx validation and baseline smoke tests",
            "Backup approved state",
            "Fast-forward with git merge --ff-only",
            "Post-update: manage.py check and check --deploy",
            "Post-update: restart only approved units",
            "Post-update: smoke tests and runtime SHA256 verification",
            "Targeted rollback on any post-mutation failure",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-unit", required=True)
    parser.add_argument("--old-sha", required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--backup-root")
    parser.add_argument("--allowed-warning", action="append", default=[])
    parser.add_argument("--restart-web", action="store_true")
    parser.add_argument("--restart-unit", action="append", default=[])
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply fast-forward after Preflight. Without this flag, run Preflight only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = Runner()
    try:
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


if __name__ == "__main__":
    raise SystemExit(main())
