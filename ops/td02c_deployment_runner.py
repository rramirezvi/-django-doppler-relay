"""Single versioned TD-02C deployment orchestrator.

The external wrapper contract is one package-module invocation.  This module
delegates Git, rollback, readiness, pycache, settings, test-profile, and worker
decisions to their versioned implementations instead of rebuilding them in
shell.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

try:
    import pwd
except ImportError:  # pragma: no cover - production orchestrator is Linux-only
    pwd = None  # type: ignore[assignment]

from ops.deployment_hardening import (
    DeploymentContext,
    DeploymentError,
    NginxTarget,
    Runner,
    ServiceMetadata,
    deployment_plan,
    discover_and_validate_nginx,
    discover_service,
    preflight,
    refresh_deployment_ref,
    run_manage_check,
    safe_repo_path,
    sha256_file,
    targeted_rollback,
    validate_readiness_layers,
    write_backup,
)
from ops.deployment_test_profile import (
    TestProfile,
    ValidationEvidence,
    validate_predeployment_evidence,
    validate_test_command,
)
from ops.isolated_py_compile import isolated_py_compile_ephemeral
from ops.td02c_settings_gate import evaluate_django_settings
from ops.td02c_worker_gate import (
    WorkerSnapshot,
    evaluate_worker_precondition,
)


RUNTIME_PATHS = (
    "attachments/reports/schemas/schema_bounces.json",
    "attachments/reports/schemas/schema_clicks.json",
    "attachments/reports/schemas/schema_deliveries.json",
    "attachments/reports/schemas/schema_opens.json",
    "attachments/reports/schemas/schema_sent.json",
    "attachments/reports/schemas/schema_spam.json",
    "attachments/reports/schemas/schema_unsubscribed.json",
    "attachments/reports/schemas/summary_all.txt",
    "attachments/templates/.gitkeep",
)
PYCOMPILE_SOURCES = (
    Path("ops/deployment_hardening.py"),
    Path("ops/deployment_test_profile.py"),
    Path("ops/isolated_py_compile.py"),
    Path("ops/td02c_deployment_runner.py"),
    Path("ops/td02c_settings_gate.py"),
    Path("ops/td02c_worker_gate.py"),
)
FORBIDDEN_WRAPPER_TOKENS = (
    "/tmp/td02c-pycache-root",
    "runuser -u",
    "python ops/",
    "curl ",
    "git merge",
    "git reset",
)
EXPECTED_MODULE = "ops.td02c_deployment_runner"
DEPLOY_WARNING_ALLOWLIST = frozenset({"W005", "W021"})
PHASE_EXIT_CODES = {
    "entrypoint": 10,
    "preflight": 20,
    "premerge_pycompile": 21,
    "settings": 22,
    "backup": 30,
    "merge": 40,
    "postmerge_pycompile": 41,
    "postmerge": 42,
    "deploy": 50,
    "rollback": 60,
    "cleanup": 70,
    "evidence": 71,
    "orchestrator": 90,
}


class TD02CDeploymentError(RuntimeError):
    def __init__(self, phase: str, classification: str, message: str = ""):
        super().__init__(message or classification)
        self.phase = phase
        self.classification = classification


class DeploymentInterrupted(TD02CDeploymentError):
    pass


@dataclasses.dataclass(frozen=True)
class Config:
    mode: str
    service_unit: str
    worker_unit: str
    old_sha: str
    target_sha: str
    remote: str
    branch: str
    expected_commits: tuple[str, ...]
    backup_root: Path | None
    validation_evidence: Path | None
    evidence_output: Path
    allowed_warnings: tuple[str, ...]


class Evidence:
    def __init__(self, output: Path, repository: Path | None = None):
        self.output = output
        self.repository = repository.resolve() if repository else None
        self.events: list[dict[str, Any]] = []
        if output.exists():
            if output.is_symlink() or not output.is_file():
                raise TD02CDeploymentError("evidence", "unsafe_evidence_path")
            info = output.stat(follow_symlinks=False)
            if stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise TD02CDeploymentError("evidence", "unsafe_evidence_file")
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                raise TD02CDeploymentError("evidence", "evidence_wrong_owner")
            try:
                existing = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise TD02CDeploymentError("evidence", "evidence_json_invalid") from exc
            events = existing.get("events")
            if not isinstance(events, list):
                raise TD02CDeploymentError("evidence", "evidence_schema_invalid")
            self.events.extend(events)

    def emit(self, phase: str, result: str, classification: str, **fields: Any) -> None:
        event = {
            "phase": phase,
            "result": result,
            "classification": classification,
            "timestamp": time.time(),
            "exit_code": 0 if result == "PASS" else PHASE_EXIT_CODES.get(phase, 90),
            **fields,
        }
        self.events.append(event)
        print(json.dumps(event, sort_keys=True, default=str))

    def save(self) -> None:
        path = self.output
        if not path.is_absolute() or path.is_symlink():
            raise TD02CDeploymentError("cleanup", "unsafe_evidence_path")
        parent = path.parent.resolve(strict=True)
        if self.repository and (parent == self.repository or self.repository in parent.parents):
            raise TD02CDeploymentError("evidence", "evidence_inside_checkout")
        parent_info = parent.stat()
        if os.name == "posix" and stat.S_IMODE(parent_info.st_mode) != 0o700:
            raise TD02CDeploymentError("evidence", "unsafe_evidence_directory_mode")
        if hasattr(os, "geteuid") and parent_info.st_uid != os.geteuid():
            raise TD02CDeploymentError("evidence", "evidence_directory_wrong_owner")
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=parent
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"events": self.events}, handle, indent=2, default=str)
                handle.write("\n")
            temporary.chmod(0o600)
            os.replace(temporary, path)
            path.chmod(0o600)
            verified = json.loads(path.read_text(encoding="utf-8"))
            if verified != {"events": self.events}:
                raise TD02CDeploymentError("evidence", "evidence_integrity_failed")
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


def _namespace(config: Config) -> argparse.Namespace:
    return argparse.Namespace(
        service_unit=config.service_unit,
        old_sha=config.old_sha,
        target_sha=config.target_sha,
        remote=config.remote,
        branch=config.branch,
        backup_root=str(config.backup_root) if config.backup_root else None,
        allowed_warning=list(config.allowed_warnings),
        expected_commit=list(config.expected_commits),
        restart_web=False,
        restart_unit=[],
        readiness_timeout=60.0,
        readiness_poll_interval=0.25,
        fetch_target=False,
        refresh_deployment_ref=True,
        execute=config.mode == "deploy-only",
    )


def _load_validation_evidence(path: Path) -> ValidationEvidence:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise TD02CDeploymentError("preflight", "validation_evidence_unsafe")
    data = json.loads(path.read_text(encoding="utf-8"))
    allowed = {field.name for field in dataclasses.fields(ValidationEvidence)}
    if set(data) != allowed:
        raise TD02CDeploymentError("preflight", "validation_evidence_schema_invalid")
    data["commit_sequence"] = tuple(data["commit_sequence"])
    return ValidationEvidence(**data)


def _django_state() -> tuple[Any, int, int, int]:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from django.conf import settings
    from relay.models import BackgroundJob, BulkSend, BulkSendRecipient

    gate = evaluate_django_settings(settings, expect_active=False)
    if not gate.allowed:
        raise TD02CDeploymentError(
            "settings", gate.code, ",".join(gate.reasons)
        )
    jobs = BackgroundJob.objects.filter(state__in=("queued", "running")).count()
    v2 = BulkSend.objects.filter(engine_version="v2").count()
    ledger = BulkSendRecipient.objects.count()
    return settings, jobs, v2, ledger


def _systemd_values(runner: Runner, unit: str) -> dict[str, str]:
    properties = (
        "ActiveState", "SubState", "MainPID", "NRestarts",
        "ActiveEnterTimestamp", "ExecMainStartTimestamp", "ExecStart", "Requires",
    )
    result = runner.run(
        ["systemctl", "show", unit, *[f"--property={name}" for name in properties]]
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def _worker_snapshot(runner: Runner, unit: str, jobs: int) -> WorkerSnapshot:
    values = _systemd_values(runner, unit)
    return WorkerSnapshot(
        main_pid=int(values.get("MainPID", "0") or 0),
        active_state=values.get("ActiveState", ""),
        sub_state=values.get("SubState", ""),
        nrestarts=int(values.get("NRestarts", "0") or 0),
        active_enter_timestamp=values.get("ActiveEnterTimestamp", ""),
        exec_main_start_timestamp=values.get("ExecMainStartTimestamp", ""),
        command=values.get("ExecStart", ""),
        requires=tuple(values.get("Requires", "").split()),
        queued_jobs=jobs,
        running_jobs=0,
        jobs_total=jobs,
        v2_jobs_total=0,
    )


def validate_single_invocation(command: Sequence[str]) -> None:
    rendered = " ".join(command)
    expected = "-m ops.td02c_deployment_runner"
    if expected not in rendered:
        raise TD02CDeploymentError("entrypoint", "module_entrypoint_required")
    if any(token in rendered for token in FORBIDDEN_WRAPPER_TOKENS):
        raise TD02CDeploymentError("entrypoint", "duplicated_wrapper_logic")


def validate_deploy_check_output(output: str) -> set[str]:
    """Accept only the two explicitly approved Django deploy warnings."""
    if not output.strip():
        return set()
    if "SystemCheckError" in output or re.search(r"\bERRORS?\b", output):
        raise TD02CDeploymentError("postmerge", "deploy_check_error")
    codes = set(re.findall(r"\b(?:security\.)?(W\d{3})\b", output))
    warning_lines = [line for line in output.splitlines() if "WARNING" in line or re.search(r"\bW\d{3}\b", line)]
    if warning_lines and not codes:
        raise TD02CDeploymentError("postmerge", "deploy_check_malformed")
    unexpected = codes - DEPLOY_WARNING_ALLOWLIST
    if unexpected:
        raise TD02CDeploymentError("postmerge", "unexpected_deploy_warning", ",".join(sorted(unexpected)))
    return codes


@contextlib.contextmanager
def repository_lock(repository: Path, operation: str):
    """Serialize every TD-02C operation using a kernel lock outside checkout."""
    if fcntl is None or os.name != "posix":
        raise TD02CDeploymentError("preflight", "flock_required")
    digest = hashlib.sha256(str(repository.resolve()).encode()).hexdigest()[:20]
    path = Path(tempfile.gettempdir()) / f"td02c-deployment-{digest}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise TD02CDeploymentError("preflight", "unsafe_operation_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TD02CDeploymentError("preflight", "operation_already_running") from exc
        payload = json.dumps({"pid": os.getpid(), "operation": operation}) + "\n"
        os.ftruncate(fd, 0)
        os.write(fd, payload.encode())
        os.fsync(fd)
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextlib.contextmanager
def deployment_signals():
    previous: dict[int, Any] = {}
    def interrupted(signum, _frame):
        raise DeploymentInterrupted("deploy", "signal_received", str(signum))
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, interrupted)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


class ProductionBackend:
    def __init__(self, config: Config, evidence: Evidence, runner: Runner | None = None):
        self.config = config
        self.evidence = evidence
        self.runner = runner or Runner()
        self.context: DeploymentContext | None = None

    def _require_service_identity(self, service: ServiceMetadata) -> None:
        if pwd is None or not hasattr(os, "geteuid"):
            raise TD02CDeploymentError("preflight", "posix_required")
        effective = pwd.getpwuid(os.geteuid()).pw_name
        if effective != service.user:
            raise TD02CDeploymentError("preflight", "service_user_mismatch")

    def _validate_python(self, phase: str) -> None:
        assert self.context is not None
        result = isolated_py_compile_ephemeral(
            repository=self.context.repository,
            python=self.context.service.python,
            service_user=self.context.service.user,
            sources=PYCOMPILE_SOURCES,
        )
        if result.returncode:
            raise TD02CDeploymentError(phase, "isolated_py_compile_failed")
        self.evidence.emit(phase, "PASS", "isolated_py_compile_passed")

    def _validate_operational_gates(self, phase: str) -> None:
        assert self.context is not None
        _, jobs, v2, ledger = _django_state()
        if (jobs, v2, ledger) != (0, 0, 0):
            raise TD02CDeploymentError(phase, "unexpected_active_work")
        worker = evaluate_worker_precondition(
            _worker_snapshot(self.runner, self.config.worker_unit, jobs)
        )
        if not worker.allowed:
            raise TD02CDeploymentError(
                phase, worker.classification, ",".join(worker.reasons)
            )
        validate_test_command(
            TestProfile.PRODUCTION,
            [str(self.context.service.python), "-m", "unittest", "discover", "-s", "ops/tests"],
            effective_user=self.context.service.user,
        )
        readiness = validate_readiness_layers(
            self.runner, self.context.service, self.context.nginx
        )
        self.evidence.emit(
            phase, "PASS", "operational_gates_passed",
            jobs=jobs, bulk_send_v2=v2, ledger=ledger, readiness=readiness,
        )

    def _assert_runtime_unchanged(self, phase: str) -> None:
        assert self.context is not None
        for name, expected_hash in self.context.runtime_hashes.items():
            path = safe_repo_path(self.context.repository, name, must_exist=True)
            info = path.stat(follow_symlinks=False)
            actual = {
                "sha256": sha256_file(path),
                "mode": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
            }
            expected = self.context.runtime_metadata[name]
            if actual["sha256"] != expected_hash or actual != expected:
                raise TD02CDeploymentError(phase, "runtime_baseline_changed")

    def _validate_materialized_range(self) -> None:
        assert self.context is not None
        expected_uid = pwd.getpwnam(self.context.service.user).pw_uid if pwd else None
        for name in self.context.changed_files:
            entry = self.runner.run(
                ["git", "ls-tree", self.context.target_sha, "--", name],
                cwd=self.context.repository, user=self.context.service.user,
            ).stdout.strip()
            path = safe_repo_path(self.context.repository, name)
            if not entry:
                if path.exists() or path.is_symlink():
                    raise TD02CDeploymentError("merge", "deleted_path_still_present")
                continue
            if not path.exists() or path.is_symlink():
                raise TD02CDeploymentError("merge", "materialized_path_missing_or_symlink")
            info = path.stat(follow_symlinks=False)
            if expected_uid is not None and info.st_uid != expected_uid:
                raise TD02CDeploymentError("merge", "materialized_path_wrong_owner")
            if not stat.S_ISREG(info.st_mode):
                raise TD02CDeploymentError("merge", "materialized_path_not_regular")
            if self.runner.run(
                ["git", "diff", "--quiet", self.context.target_sha, "--", name],
                cwd=self.context.repository, user=self.context.service.user,
                check=False,
            ).returncode:
                raise TD02CDeploymentError("merge", "materialized_path_differs")

    def _preflight_unlocked(self) -> DeploymentContext:
        service = discover_service(self.runner, self.config.service_unit)
        self._require_service_identity(service)
        self.context = preflight(_namespace(self.config), self.runner)
        if tuple(sorted(self.context.runtime_files)) != tuple(sorted(RUNTIME_PATHS)):
            raise TD02CDeploymentError("preflight", "runtime_baseline_mismatch")
        if any(not name.startswith("ops/") for name in self.context.changed_files):
            raise TD02CDeploymentError("preflight", "non_ops_change_detected")
        if self.config.validation_evidence is None:
            raise TD02CDeploymentError("preflight", "validation_evidence_required")
        validate_predeployment_evidence(
            _load_validation_evidence(self.config.validation_evidence),
            target_sha=self.config.target_sha,
            expected_commits=self.config.expected_commits,
        )
        self._validate_python("premerge_pycompile")
        self._assert_runtime_unchanged("preflight")
        self._validate_operational_gates("preflight")
        self.evidence.emit(
            "preflight", "PASS", "preflight_complete",
            plan=deployment_plan(_namespace(self.config), self.context),
            context=context_record(self.context),
        )
        return self.context

    def preflight(self) -> DeploymentContext:
        service = discover_service(self.runner, self.config.service_unit)
        with repository_lock(service.working_directory, "preflight-only"):
            return self._preflight_unlocked()

    def deploy(self) -> None:
        service = discover_service(self.runner, self.config.service_unit)
        self._require_service_identity(service)
        with repository_lock(service.working_directory, "deploy-only"):
            with deployment_signals():
                self._deploy_unlocked(service)

    def _deploy_unlocked(self, service: ServiceMetadata) -> None:
        refresh_deployment_ref(
            self.runner, service.working_directory,
            remote=self.config.remote, branch=self.config.branch,
            target_sha=self.config.target_sha, user=service.user,
        )
        context = self._preflight_unlocked()
        if self.config.backup_root is None:
            raise TD02CDeploymentError("backup", "backup_root_required")
        backup = write_backup(_namespace(self.config), self.runner, context)
        self.evidence.emit("backup", "PASS", "backup_created", path=str(backup))
        mutation_started = False
        try:
            mutation_started = True
            self.runner.run(
                ["git", "merge", "--ff-only", context.target_sha],
                cwd=context.repository, user=context.service.user,
            )
            head = self.runner.run(
                ["git", "rev-parse", "HEAD"], cwd=context.repository,
                user=context.service.user,
            ).stdout.strip()
            if head != context.target_sha:
                raise TD02CDeploymentError("merge", "target_head_mismatch")
            branch = self.runner.run(
                ["git", "symbolic-ref", "--short", "HEAD"], cwd=context.repository,
                user=context.service.user,
            ).stdout.strip()
            if branch != context.branch:
                raise TD02CDeploymentError("merge", "branch_changed")
            if self.runner.run(
                ["git", "ls-files", "-u"], cwd=context.repository,
                user=context.service.user,
            ).stdout.strip():
                raise TD02CDeploymentError("merge", "unmerged_paths_present")
            if self.runner.run(
                ["git", "diff", "--cached", "--name-only"], cwd=context.repository,
                user=context.service.user,
            ).stdout.strip():
                raise TD02CDeploymentError("merge", "unexpected_staged_changes")
            current_runtime = self.runner.run(
                ["git", "diff", "--name-only"], cwd=context.repository,
                user=context.service.user,
            ).stdout.splitlines()
            if current_runtime != context.runtime_files:
                raise TD02CDeploymentError("merge", "runtime_set_changed")
            self._assert_runtime_unchanged("merge")
            self._validate_materialized_range()
            self.evidence.emit("merge", "PASS", "fast_forward_complete", head=head)
            self._validate_python("postmerge_pycompile")
            self._validate_operational_gates("postmerge")
            run_manage_check(self.runner, context.service)
            deploy_output, _ = run_manage_check(self.runner, context.service, deploy=True)
            validate_deploy_check_output(deploy_output)
            self.runner.run(
                [str(context.service.python), "-m", "unittest", "discover", "-s", "ops/tests", "-q"],
                cwd=context.repository, user=context.service.user,
            )
            self._assert_runtime_unchanged("postmerge")
            self.evidence.emit("deploy", "PASS", "deployment_complete", head=head)
            # Evidence integrity is itself a post-merge gate.  Failure here must
            # enter the same targeted rollback path as every other gate.
            self.evidence.save()
        except BaseException as exc:
            if mutation_started:
                targeted_rollback(_namespace(self.config), self.runner, context, [])
                self.evidence.emit("rollback", "PASS", "rollback_complete")
            if isinstance(exc, TD02CDeploymentError):
                raise
            raise TD02CDeploymentError("deploy", "deployment_failed", str(exc)) from exc

    def rollback(self) -> None:
        service = discover_service(self.runner, self.config.service_unit)
        with repository_lock(service.working_directory, "rollback"):
            context = context_from_evidence(self.config, self.evidence.output, self.runner)
            targeted_rollback(_namespace(self.config), self.runner, context, [])
            self.evidence.emit("rollback", "PASS", "rollback_complete")


def context_record(context: DeploymentContext) -> dict[str, Any]:
    return {
        "old_sha": context.old_sha,
        "target_sha": context.target_sha,
        "branch": context.branch,
        "remote": context.remote,
        "changed_files": context.changed_files,
        "runtime_files": context.runtime_files,
        "runtime_hashes": context.runtime_hashes,
        "runtime_metadata": context.runtime_metadata,
        "approved_commits": context.approved_commits,
    }


def context_from_evidence(config: Config, path: Path, runner: Runner) -> DeploymentContext:
    data = json.loads(path.read_text(encoding="utf-8"))
    records = [event.get("context") for event in data.get("events", []) if event.get("context")]
    if len(records) != 1:
        raise TD02CDeploymentError("rollback", "rollback_context_missing")
    record = records[0]
    if record["old_sha"] != config.old_sha or record["target_sha"] != config.target_sha:
        raise TD02CDeploymentError("rollback", "rollback_context_mismatch")
    service = discover_service(runner, config.service_unit)
    nginx = discover_and_validate_nginx(runner, service)
    return DeploymentContext(
        service=service, nginx=nginx, old_sha=config.old_sha,
        target_sha=config.target_sha, repository=service.working_directory,
        branch=config.branch, remote=config.remote,
        changed_files=list(record["changed_files"]),
        runtime_files=list(record["runtime_files"]), intersections=[],
        runtime_hashes=dict(record["runtime_hashes"]), baseline_smoke={},
        baseline_warning_codes=set(), approved_commits=list(record["approved_commits"]),
        runtime_metadata=dict(record["runtime_metadata"]),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preflight-only", "deploy-only", "rollback"), required=True)
    parser.add_argument("--service-unit", default="django.service")
    parser.add_argument("--worker-unit", default="doppler-background-jobs.service")
    parser.add_argument("--old-sha", required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--expected-commit", action="append", default=[])
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--validation-evidence", type=Path)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--allowed-warning", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if __package__ != "ops" or __spec__ is None or __spec__.name != EXPECTED_MODULE:
        print(json.dumps({
            "phase": "entrypoint", "result": "FAIL", "exit_code": 10,
            "classification": "module_entrypoint_required",
        }, sort_keys=True))
        return 10
    args = build_parser().parse_args(argv)
    config = Config(
        mode=args.mode, service_unit=args.service_unit, worker_unit=args.worker_unit,
        old_sha=args.old_sha, target_sha=args.target_sha, remote=args.remote,
        branch=args.branch, expected_commits=tuple(args.expected_commit),
        backup_root=args.backup_root, validation_evidence=args.validation_evidence,
        evidence_output=args.evidence_output,
        allowed_warnings=tuple(args.allowed_warning),
    )
    evidence = Evidence(config.evidence_output)
    backend = ProductionBackend(config, evidence)
    try:
        if config.mode == "preflight-only":
            backend.preflight()
        elif config.mode == "deploy-only":
            backend.deploy()
        else:
            backend.rollback()
        return 0
    except (DeploymentError, TD02CDeploymentError, OSError, subprocess.SubprocessError) as exc:
        phase = exc.phase if isinstance(exc, TD02CDeploymentError) else "orchestrator"
        classification = (
            exc.classification if isinstance(exc, TD02CDeploymentError)
            else "orchestrator_failed"
        )
        evidence.emit(phase, "FAIL", classification)
        return PHASE_EXIT_CODES.get(phase, 90)
    finally:
        evidence.save()


if __name__ == "__main__":
    raise SystemExit(main())
