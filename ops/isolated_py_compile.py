#!/usr/bin/env python3
"""Run production py_compile without writing bytecode inside the checkout."""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import stat
import subprocess
from pathlib import Path
from typing import Sequence

try:
    import pwd
except ImportError:  # pragma: no cover - operational helper is Linux-only
    pwd = None  # type: ignore[assignment]


class PyCompileValidationError(RuntimeError):
    pass


@contextlib.contextmanager
def _cleanup_on_termination():
    """Turn normal termination signals into exceptions so cleanup runs."""
    previous = {}

    def interrupt(signum, _frame):
        raise InterruptedError(f"interrupted by signal {signum}")

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupt)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_cache_root(repository: Path, cache_root: Path) -> tuple[Path, Path]:
    """Validate an absolute, real, non-symlink cache root outside the checkout."""
    if not repository.is_absolute() or not cache_root.is_absolute():
        raise PyCompileValidationError("repository and cache root must be absolute")
    if repository.is_symlink() or cache_root.is_symlink():
        raise PyCompileValidationError("repository and cache root must not be symlinks")
    resolved_repository = repository.resolve(strict=True)
    resolved_root = cache_root.resolve(strict=True)
    if not resolved_repository.is_dir() or not resolved_root.is_dir():
        raise PyCompileValidationError("repository and cache root must be directories")
    if _inside(resolved_root, resolved_repository):
        raise PyCompileValidationError("cache root must be outside the repository")
    return resolved_repository, resolved_root


def _safe_sources(repository: Path, sources: Sequence[Path]) -> list[Path]:
    if not sources:
        raise PyCompileValidationError("at least one Python source is required")
    validated: list[Path] = []
    for source in sources:
        candidate = source if source.is_absolute() else repository / source
        if candidate.is_symlink():
            raise PyCompileValidationError("Python source must not be a symlink")
        resolved = candidate.resolve(strict=True)
        if not _inside(resolved, repository) or not resolved.is_file():
            raise PyCompileValidationError("Python source must be a file in the repository")
        validated.append(resolved)
    return validated


def _run_as_user(
    argv: Sequence[str], *, cwd: Path, user: str
) -> subprocess.CompletedProcess[str]:
    if pwd is None:
        raise PyCompileValidationError("operational py_compile requires POSIX")
    current_user = pwd.getpwuid(os.geteuid()).pw_name
    command = list(argv)
    if current_user != user:
        if os.geteuid() != 0:
            raise PyCompileValidationError("cannot switch to the operational user")
        command = ["runuser", "-u", user, "--", *command]
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        check=False,
        timeout=120,
    )


def _git_status(repository: Path, user: str) -> str:
    result = _run_as_user(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository,
        user=user,
    )
    if result.returncode:
        raise PyCompileValidationError("could not capture git status")
    return result.stdout


def _checkout_bytecode(repository: Path) -> set[Path]:
    return {path.relative_to(repository) for path in repository.rglob("*.pyc")}


def _account(service_user: str):
    if pwd is None:
        raise PyCompileValidationError("operational py_compile requires POSIX")
    try:
        return pwd.getpwnam(service_user)
    except KeyError as exc:
        raise PyCompileValidationError("operational user does not exist") from exc


def _validate_workspace(
    *,
    repository: Path,
    workspace: Path,
    service_user: str,
    cache_root: Path | None = None,
) -> Path:
    """Validate the exact workspace created for this invocation."""
    if not workspace.is_absolute() or workspace.is_symlink():
        raise PyCompileValidationError("temporary cache workspace is ambiguous")
    resolved = workspace.resolve(strict=True)
    if not resolved.is_dir() or _inside(resolved, repository):
        raise PyCompileValidationError("temporary cache workspace is unsafe")
    if cache_root is not None and resolved.parent != cache_root:
        raise PyCompileValidationError("temporary cache workspace escaped its parent")
    metadata = resolved.stat(follow_symlinks=False)
    account = _account(service_user)
    if metadata.st_uid != account.pw_uid or metadata.st_gid != account.pw_gid:
        raise PyCompileValidationError("temporary cache owner or group is unexpected")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PyCompileValidationError("temporary cache mode is not 0700")
    return resolved


def _write_probe(workspace: Path, service_user: str) -> None:
    probe = workspace / ".write-probe"
    result = _run_as_user(
        [
            "sh",
            "-c",
            'set -eu; umask 077; printf probe > "$1"; test "$(cat "$1")" = probe; rm -- "$1"',
            "td02c-write-probe",
            str(probe),
        ],
        cwd=workspace,
        user=service_user,
    )
    if result.returncode or probe.exists():
        raise PyCompileValidationError("effective cache write/read/delete probe failed")


def _remove_workspace(
    *,
    repository: Path,
    workspace: Path,
    service_user: str,
    cache_root: Path,
    identity: tuple[int, int],
) -> None:
    workspace = _validate_workspace(
        repository=repository,
        workspace=workspace,
        service_user=service_user,
        cache_root=cache_root,
    )
    metadata = workspace.stat(follow_symlinks=False)
    if (metadata.st_dev, metadata.st_ino) != identity:
        raise PyCompileValidationError("temporary cache workspace identity changed")
    if not workspace.name.startswith("td02c-pycache-"):
        raise PyCompileValidationError("refusing to remove unexpected cache workspace")
    result = _run_as_user(
        ["rm", "-rf", "--", str(workspace)], cwd=workspace.parent, user=service_user
    )
    if result.returncode or workspace.exists():
        raise PyCompileValidationError("temporary cache cleanup failed")


@contextlib.contextmanager
def unique_cache_workspace(
    *, repository: Path, cache_root: Path, service_user: str
):
    """Create a unique 0700 cache workspace directly as the operational user."""
    repository, cache_root = validate_cache_root(repository, cache_root)
    account = _account(service_user)
    result = _run_as_user(
        ["mktemp", "-d", "-p", str(cache_root), "td02c-pycache-XXXXXXXXXX"],
        cwd=cache_root,
        user=service_user,
    )
    if result.returncode:
        raise PyCompileValidationError("could not create unique cache workspace")
    lines = result.stdout.splitlines()
    if len(lines) != 1:
        raise PyCompileValidationError("mktemp returned an ambiguous cache path")
    workspace = Path(lines[0])
    identity = None
    try:
        workspace = _validate_workspace(
            repository=repository,
            workspace=workspace,
            service_user=service_user,
            cache_root=cache_root,
        )
        # Explicitly document that group ownership is expected to match the
        # operational account's primary group.
        if workspace.stat(follow_symlinks=False).st_gid != account.pw_gid:
            raise PyCompileValidationError("temporary cache group is unexpected")
        metadata = workspace.stat(follow_symlinks=False)
        identity = (metadata.st_dev, metadata.st_ino)
        print(
            "pycache_workspace_created "
            f"owner_uid={metadata.st_uid} group_gid={metadata.st_gid} mode=0700"
        )
        _write_probe(workspace, service_user)
        print("pycache_write_probe=PASS")
        yield workspace
    finally:
        if workspace.exists() or workspace.is_symlink():
            if identity is None:
                raise PyCompileValidationError(
                    "refusing cleanup without validated workspace identity"
                )
            _remove_workspace(
                repository=repository,
                workspace=workspace,
                service_user=service_user,
                cache_root=cache_root,
                identity=identity,
            )
            print("pycache_cleanup=PASS")


def isolated_py_compile(
    *,
    repository: Path,
    python: Path,
    service_user: str,
    sources: Sequence[Path],
    cache_root: Path,
) -> subprocess.CompletedProcess[str]:
    """Compile sources as service_user and always remove the external cache."""
    repository, cache_root = validate_cache_root(repository, cache_root)
    if pwd is None:
        raise PyCompileValidationError("operational py_compile requires POSIX")
    python = python.resolve(strict=True)
    if not python.is_file() or not os.access(python, os.X_OK):
        raise PyCompileValidationError("Python interpreter is not executable")
    _account(service_user)
    validated_sources = _safe_sources(repository, sources)
    status_before = _git_status(repository, service_user)
    bytecode_before = _checkout_bytecode(repository)

    with unique_cache_workspace(
        repository=repository, cache_root=cache_root, service_user=service_user
    ) as cache:
        result = _run_as_user(
            [
                "env",
                f"PYTHONPYCACHEPREFIX={cache}",
                str(python),
                "-m",
                "py_compile",
                *(str(path) for path in validated_sources),
            ],
            cwd=repository,
            user=service_user,
        )
        if _git_status(repository, service_user) != status_before:
            raise PyCompileValidationError("py_compile changed git status")
        if _checkout_bytecode(repository) != bytecode_before:
            raise PyCompileValidationError("py_compile wrote bytecode in the checkout")
        return result


def isolated_py_compile_ephemeral(
    *,
    repository: Path,
    python: Path,
    service_user: str,
    sources: Sequence[Path],
    temporary_parent: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Compile using a unique parent and workspace, both removed afterwards."""
    if pwd is None:
        raise PyCompileValidationError("operational py_compile requires POSIX")
    repository = repository.resolve(strict=True)
    parent = (temporary_parent or Path(tempfile.gettempdir())).resolve(strict=True)
    if parent.is_symlink() or not parent.is_dir() or _inside(parent, repository):
        raise PyCompileValidationError("temporary parent is unsafe")
    account = _account(service_user)
    result = _run_as_user(
        ["mktemp", "-d", "-p", str(parent), "td02c-pycache-parent-XXXXXXXXXX"],
        cwd=parent,
        user=service_user,
    )
    if result.returncode or len(result.stdout.splitlines()) != 1:
        raise PyCompileValidationError("could not create unique cache parent")
    cache_root = Path(result.stdout.strip())
    identity: tuple[int, int] | None = None
    try:
        metadata = cache_root.stat(follow_symlinks=False)
        if (
            cache_root.is_symlink()
            or not cache_root.is_dir()
            or cache_root.parent.resolve(strict=True) != parent
            or metadata.st_uid != account.pw_uid
            or metadata.st_gid != account.pw_gid
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise PyCompileValidationError("unique cache parent metadata is unsafe")
        identity = (metadata.st_dev, metadata.st_ino)
        return isolated_py_compile(
            repository=repository,
            python=python,
            service_user=service_user,
            sources=sources,
            cache_root=cache_root,
        )
    finally:
        if cache_root.exists() or cache_root.is_symlink():
            if identity is None:
                raise PyCompileValidationError(
                    "refusing cleanup without validated cache parent identity"
                )
            metadata = cache_root.stat(follow_symlinks=False)
            if (
                cache_root.is_symlink()
                or (metadata.st_dev, metadata.st_ino) != identity
                or any(cache_root.iterdir())
            ):
                raise PyCompileValidationError("unique cache parent cleanup unsafe")
            cleanup = _run_as_user(
                ["rmdir", "--", str(cache_root)], cwd=parent, user=service_user
            )
            if cleanup.returncode or cache_root.exists():
                raise PyCompileValidationError("unique cache parent cleanup failed")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--service-user", required=True)
    parser.add_argument("--cache-root", required=True, type=Path)
    parser.add_argument("sources", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        with _cleanup_on_termination():
            result = isolated_py_compile(
                repository=args.repository,
                python=args.python,
                service_user=args.service_user,
                sources=args.sources,
                cache_root=args.cache_root,
            )
    except (OSError, KeyError, InterruptedError, PyCompileValidationError) as exc:
        print(f"py_compile validation failed: {exc}")
        return 1
    if result.stdout:
        print(result.stdout, end="")
    print(f"py_compile_result={'PASS' if result.returncode == 0 else 'FAIL'}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
