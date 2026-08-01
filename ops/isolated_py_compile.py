#!/usr/bin/env python3
"""Run production py_compile without writing bytecode inside the checkout."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

try:
    import pwd
except ImportError:  # pragma: no cover - operational helper is Linux-only
    pwd = None  # type: ignore[assignment]


class PyCompileValidationError(RuntimeError):
    pass


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
    account = pwd.getpwnam(service_user)
    validated_sources = _safe_sources(repository, sources)
    status_before = _git_status(repository, service_user)
    bytecode_before = _checkout_bytecode(repository)

    cache = Path(tempfile.mkdtemp(prefix="td02c-pycache-", dir=cache_root))
    try:
        if cache.is_symlink() or cache.resolve().parent != cache_root:
            raise PyCompileValidationError("temporary cache path is ambiguous")
        os.chmod(cache, 0o700)
        if os.geteuid() == 0:
            os.chown(cache, account.pw_uid, account.pw_gid)
        metadata = cache.stat(follow_symlinks=False)
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise PyCompileValidationError("temporary cache mode is not 0700")
        if metadata.st_uid != account.pw_uid:
            raise PyCompileValidationError("temporary cache owner is not service user")

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
    finally:
        shutil.rmtree(cache, ignore_errors=False)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--service-user", required=True)
    parser.add_argument("--cache-root", required=True, type=Path)
    parser.add_argument("sources", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        result = isolated_py_compile(
            repository=args.repository,
            python=args.python,
            service_user=args.service_user,
            sources=args.sources,
            cache_root=args.cache_root,
        )
    except (OSError, KeyError, PyCompileValidationError) as exc:
        print(f"py_compile validation failed: {exc}")
        return 1
    if result.stdout:
        print(result.stdout, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
