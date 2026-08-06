#!/usr/bin/env python3
"""Gate-guarded, committed, credential-free canary POST client.

Composition layer only -- not a new HTTP stack. Session machinery
(``CurlOperations``, ``DjangoState``, ``validate_credential_file``,
``delete_exact_file``, ``validate_module_entrypoint``) already exists in
``ops.td02c_authenticated_get_runner``; POST artifacts
(``write_post_curl_config``, ``safe_curl_argv``, ``classify_canary_response``,
``secure_cookie_workspace``) already exist in ``ops.td02c_http_client``. This
module adds exactly three things the repo lacks: (1) a settings-gate
precondition, (2) the POST execution step the GET-only runner deliberately
refuses, (3) an expected-delta assertion. ``relay/``, ``config/settings.py``
and ``evaluate_canary`` stay untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence, TextIO

from ops.deployment_hardening import DeploymentError, Runner, discover_service
from ops.td02c_authenticated_get_runner import (
    Baseline,
    CurlOperations,
    DjangoState,
    RunnerFailure,
    delete_exact_file,
    emit_counts,
    validate_credential_file,
    validate_module_entrypoint,
)
from ops.td02c_deployment_runner import TD02CDeploymentError, repository_lock
from ops.td02c_http_client import (
    AuthenticatedGetFailure,
    ERROR_CODES as _HTTP_CLIENT_ERROR_CODES,
    ResponseDecision,
    ResponseMetadata,
    SafeDiagnosticLog,
    StageDiagnostic,
    classify_canary_response,
    run_authenticated_get_gate,
    safe_curl_argv,
    secure_cookie_workspace,
    write_post_curl_config,
)
from ops.td02c_settings_gate import evaluate_django_settings

EXPECTED_MODULE = "ops.bulk_v2_canary_client"

# New codes this client introduces, plus the existing, unmodified
# ops.td02c_http_client taxonomy (raised by the reused login/GET machinery).
ERROR_CODES = {
    "settings_gate_failed",
    "profile_mismatch",
    "import_only_violation",
    "unexpected_row_delta",
    "firewall_breach",
    "disposition_required",
    "client_module_path_mismatch",
    "client_module_not_importable_from_checkout",
    "client_entrypoint_identity_mismatch",
    "unexpected_error",
    "concurrent_execution_blocked",
} | _HTTP_CLIENT_ERROR_CODES

# Matches the --write-out format ops.td02c_authenticated_get_runner.
# CurlOperations._curl uses for the GET gate; write_post_curl_config's
# config file never includes it (it stays a plain body-secret carrier), so
# this client appends the same, non-secret, machine-readable flag on top of
# the safe base argv returned by safe_curl_argv().
WRITE_OUT_FORMAT = "%{http_code}\t%{content_type}\t%{redirect_url}\t%{num_redirects}\t%{ssl_verify_result}\t%{time_total}"

_FORBIDDEN_PROFILE_CHARS = ("\n", "\r", '"', "\\")


@dataclass(frozen=True)
class CanaryProfile:
    """One profile feeds both the settings gate and the POST (design D3).

    Deliberately has no ``send_now``, ``scheduled_at`` or ``sender_id``
    field: their absence *is* the client-side import-only enforcement
    (design D4), not a runtime check catching an override attempt.
    """

    request_id: str
    user_id: int
    template_id: str
    template_name: str
    subject: str
    max_rows: int = 20


def _load_django_settings():
    """Return the effective Django settings object.

    Isolated into its own function so tests can patch it with a plain
    ``SimpleNamespace`` and never require a configured Django project.
    """
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from django.conf import settings

    return settings


def _assert_profile_matches_settings(profile: CanaryProfile, settings) -> None:
    """Design D3 + requirement: max_rows is read from the same source the
    gate validates (``BULK_PROCESSING_V2_CANARY_MAX_ROWS``), never a
    hardcoded number. Runs after the gate call but before any workspace,
    credential or network activity."""
    if profile.request_id != settings.BULK_PROCESSING_V2_CANARY_REQUEST_IDS:
        raise RunnerFailure("profile_mismatch")
    if str(profile.user_id) != settings.BULK_PROCESSING_V2_CANARY_USER_IDS:
        raise RunnerFailure("profile_mismatch")
    if profile.max_rows > settings.BULK_PROCESSING_V2_CANARY_MAX_ROWS:
        raise RunnerFailure("profile_mismatch")


def _assert_import_only_profile(profile: CanaryProfile) -> None:
    """Defense-in-depth (design D4). CanaryProfile has no field that could
    ever set send_now/scheduled_at/sender_id, so the only remaining
    injection path into write_post_curl_config's flat ``form = "..."``
    lines is a crafted string value breaking out of its own line. Reject
    that outright, before any workspace or credential file is touched."""
    for value in (
        profile.request_id,
        profile.template_id,
        profile.template_name,
        profile.subject,
    ):
        if not value or any(char in value for char in _FORBIDDEN_PROFILE_CHARS):
            raise RunnerFailure("import_only_violation")


def _assert_safe_csv_path(csv_path: Path) -> None:
    """csv_path is not a CanaryProfile field -- it is supplied separately to
    run_canary/_execute_canary_post -- but ops.td02c_http_client's
    write_post_curl_config interpolates it into the exact same flat
    ``form = "recipients_file=@{csv_path};type=text/csv"`` line that
    _assert_import_only_profile's forbidden-character check protects the
    other four fields against. Apply the identical check here, before any
    workspace, credential, or network activity."""
    value = str(csv_path)
    if not value or any(char in value for char in _FORBIDDEN_PROFILE_CHARS):
        raise RunnerFailure("import_only_violation")


def _assert_expected_delta(before: Baseline, after: Baseline, *, expect_success: bool) -> None:
    """Design D6. jobs/messages MUST stay at zero delta regardless of the
    POST outcome -- any nonzero delta there is a V1/V2 firewall breach,
    escalated loudly, never merely logged. The row-creation delta
    (bulk_sends +1, bulk_sends_v2 +1, recipients +N) is only expected when
    the POST itself was classified as allowed."""
    if (after.jobs - before.jobs, after.messages - before.messages) != (0, 0):
        raise RunnerFailure("firewall_breach")
    if not expect_success:
        return
    if (
        after.bulk_sends - before.bulk_sends != 1
        or after.bulk_sends_v2 - before.bulk_sends_v2 != 1
        or after.recipients - before.recipients < 1
    ):
        raise RunnerFailure("unexpected_row_delta")


@contextmanager
def _retained_workspace(workspace: Path) -> Iterator[Path]:
    """Wrap an already-open workspace so run_authenticated_get_gate's own
    cleanup call becomes a no-op: the canary POST still needs this
    workspace and its cookie jar afterward, so the outer
    secure_cookie_workspace() context that created it -- not this inner
    gate -- owns the real teardown."""
    yield workspace


def _execute_canary_post(
    operations: CurlOperations,
    workspace: Path,
    profile: CanaryProfile,
    csv_path: Path,
    log: SafeDiagnosticLog,
) -> ResponseDecision:
    """The POST execution step the GET-only runner deliberately refuses."""
    started = time.monotonic()
    # CurlOperations._cookie is the only existing cookie-jar-value reader;
    # reusing it (instead of reimplementing Netscape cookie-jar parsing)
    # mirrors how CurlOperations.authenticate() already reads its own CSRF
    # cookie the same way.
    csrf_token = CurlOperations._cookie(workspace, "csrftoken")
    if not csrf_token or operations.target is None:
        log.emit(StageDiagnostic("canary_post_completed", "FAIL", 1, time.monotonic() - started, "csrf_cookie_missing"))
        raise AuthenticatedGetFailure("csrf_cookie_missing")

    config = write_post_curl_config(
        workspace,
        target=operations.target,
        csrf_token=csrf_token,
        csv_path=csv_path,
        client_request_id=profile.request_id,
        template_id=profile.template_id,
        template_name=profile.template_name,
        subject=profile.subject,
    )
    argv = safe_curl_argv(config) + ["--write-out", WRITE_OUT_FORMAT]
    fingerprint = hashlib.sha256(profile.request_id.encode("utf-8")).hexdigest()[:12]
    try:
        result = subprocess.run(
            argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, check=False, timeout=35
        )
    except subprocess.SubprocessError:
        log.emit(StageDiagnostic("canary_post_completed", "FAIL", 1, time.monotonic() - started, "connection_failed", detail=f"request={fingerprint}"))
        raise AuthenticatedGetFailure("connection_failed") from None
    if result.returncode:
        classification = "tls_failed" if result.returncode == 60 else "connection_failed"
        log.emit(StageDiagnostic("canary_post_completed", "FAIL", result.returncode, time.monotonic() - started, classification, detail=f"request={fingerprint}"))
        raise AuthenticatedGetFailure(classification, exit_code=result.returncode)

    fields = (result.stdout or "").strip().split("\t")
    if len(fields) != 6:
        log.emit(StageDiagnostic("canary_post_completed", "FAIL", 1, time.monotonic() - started, "unexpected_status", detail=f"request={fingerprint}"))
        raise RunnerFailure("unexpected_status")
    metadata = ResponseMetadata(
        "POST", "/api/bulk-sends/", int(fields[0]), fields[1], fields[2], int(fields[3]),
        float(fields[5]) or (time.monotonic() - started), int(fields[4]),
    )
    decision = classify_canary_response(metadata)
    log.emit(
        StageDiagnostic(
            "canary_post_completed",
            "PASS" if decision.allowed else "FAIL",
            0 if decision.allowed else 1,
            time.monotonic() - started,
            "" if decision.allowed else decision.code,
            http=metadata,
            detail=f"request={fingerprint}",
        )
    )
    if not decision.allowed:
        raise RunnerFailure(decision.code)
    return decision


def run_canary(
    profile: CanaryProfile,
    stream: TextIO = sys.stdout,
    *,
    csv_path: Path,
    credential_file: Path,
    service_unit: str,
    username: str,
) -> int:
    """Run exactly one authorized, gate-guarded, import-only canary POST.

    Signature note: the design's Interfaces block lists
    ``run_canary(profile, *, csv_path, credential_file, service_unit)``.
    ``username`` is added here because ``DjangoState`` (reused unmodified)
    hard-requires both ``user_id`` *and* ``username`` to resolve exactly one
    authorized operator -- ``CanaryProfile`` intentionally carries only the
    identity that also appears in the settings gate and the POST body.
    ``stream`` is added for the same injectable-diagnostics convention
    already used by ``ops.td02c_authenticated_get_runner.run()``.
    """
    log = SafeDiagnosticLog(stream)
    started = time.monotonic()

    settings = _load_django_settings()
    gate = evaluate_django_settings(
        settings,
        expect_active=True,
        expected_request_id=profile.request_id,
        expected_user_id=profile.user_id,
    )
    if not gate.allowed:
        log.emit(
            StageDiagnostic(
                "settings_gate_evaluated", "FAIL", 1, time.monotonic() - started,
                "settings_gate_failed", detail=",".join(gate.reasons)[:200],
            )
        )
        return 1
    log.emit(StageDiagnostic("settings_gate_evaluated", "PASS", 0, time.monotonic() - started))

    # Fingerprint-Only Logging: matches relay/api.py:447-449's
    # sha256(client_request_id)[:12] pattern. Computed once and reused for
    # every subsequent diagnostic line -- the raw request_id/user_id must
    # never appear in any emitted line, only this fingerprint.
    fingerprint = hashlib.sha256(profile.request_id.encode("utf-8")).hexdigest()[:12]

    started = time.monotonic()
    try:
        _assert_profile_matches_settings(profile, settings)
        _assert_import_only_profile(profile)
        _assert_safe_csv_path(csv_path)
    except RunnerFailure as exc:
        log.emit(StageDiagnostic("profile_validated", "FAIL", 1, time.monotonic() - started, str(exc), detail=f"request={fingerprint}"))
        return 1
    log.emit(StageDiagnostic("profile_validated", "PASS", 0, time.monotonic() - started, detail=f"request={fingerprint}"))

    credential: Path | None = None
    identity: tuple[int, int, int] | None = None
    state: DjangoState | None = None
    baseline: Baseline | None = None
    operations: CurlOperations | None = None
    decision: ResponseDecision | None = None
    failure = False
    try:
        service = discover_service(Runner(), service_unit)
        started = time.monotonic()
        try:
            credential, identity = validate_credential_file(credential_file, service.working_directory, service.user)
        except Exception:
            log.emit(StageDiagnostic("credential_file_validated", "FAIL", 1, time.monotonic() - started, "credential_file_unsafe"))
            raise
        log.emit(StageDiagnostic("credential_file_validated", "PASS", 0, time.monotonic() - started))

        state = DjangoState(user_id=profile.user_id, username=username)

        # Serialize the disposition-check-through-POST section with the
        # project's existing, proven flock-based lock (reused unmodified --
        # not a new locking mechanism). Without it, two concurrent
        # invocations could both observe a clean baseline and both proceed,
        # each creating a BulkSend/BulkSendRecipient row.
        try:
            with repository_lock(repository=service.working_directory, operation="bulk_v2_canary"):
                baseline = state.baseline()
                emit_counts(stream, "baseline_before", baseline)

                # A prior, undisposed canary run permanently blocks every
                # future TD-02C deployment preflight (ops/README.md,
                # Disposition section) -- refuse to compound that rather
                # than silently stacking rows.
                if baseline.bulk_sends_v2 or baseline.recipients:
                    raise RunnerFailure("disposition_required")

                operations = CurlOperations(
                    service_unit=service_unit, credential_file=credential, state=state, baseline=baseline, log=log
                )

                with secure_cookie_workspace() as workspace:
                    run_authenticated_get_gate(
                        operations, log, workspace_factory=lambda: _retained_workspace(workspace)
                    )
                    decision = _execute_canary_post(operations, workspace, profile, csv_path, log)
        except TD02CDeploymentError as exc:
            # Translate rather than leak the deployment-runner's own error
            # type -- classified consistently with this module's other
            # refusals.
            raise RunnerFailure("concurrent_execution_blocked") from exc
    except (AuthenticatedGetFailure, DeploymentError, RunnerFailure, OSError, subprocess.SubprocessError) as exc:
        failure = True
        if not isinstance(exc, AuthenticatedGetFailure):
            classification = str(exc) if str(exc) in ERROR_CODES else "runner_failed"
            log.emit(StageDiagnostic("canary_client", "FAIL", 1, 0.0, classification))
    except Exception as exc:
        # Fail closed on any exception type this module did not anticipate
        # (e.g. django.db.utils.*/django.core.exceptions.* from
        # state.baseline(), or a bare KeyError from pwd.getpwnam inside the
        # reused validate_credential_file) -- never let it propagate out of
        # run_canary, and never echo its raw message unless it is already a
        # known, safe classification string.
        failure = True
        classification = str(exc) if str(exc) in ERROR_CODES else "unexpected_error"
        log.emit(StageDiagnostic("canary_client", "FAIL", 1, 0.0, classification))
    finally:
        if state is not None and baseline is not None:
            started = time.monotonic()
            try:
                if operations is not None and operations.session_identified:
                    state.delete_session(operations.session_key)
                after = state.baseline()
                _assert_expected_delta(baseline, after, expect_success=bool(decision and decision.allowed))
                emit_counts(stream, "baseline_after", after)
                log.emit(StageDiagnostic("session_cleanup_completed", "PASS", 0, time.monotonic() - started))
            except Exception as exc:
                failure = True
                classification = str(exc) if str(exc) in ERROR_CODES else "session_cleanup_failed"
                log.emit(StageDiagnostic("session_cleanup_completed", "FAIL", 1, time.monotonic() - started, classification))
        if credential is not None and identity is not None:
            started = time.monotonic()
            try:
                delete_exact_file(credential, identity)
            except Exception:
                failure = True
                log.emit(StageDiagnostic("credential_cleanup_completed", "FAIL", 1, time.monotonic() - started, "credential_cleanup_failed"))
            else:
                log.emit(StageDiagnostic("credential_cleanup_completed", "PASS", 0, time.monotonic() - started))
    return 1 if failure else 0


def validate_client_module_identity(service_unit: str) -> None:
    """Prove ``ops.bulk_v2_canary_client`` -- this module, not the reused
    ``ops.td02c_authenticated_get_runner`` -- is imported from the expected
    checkout, and that its CLI entrypoint (``main``) resolves to that exact
    same location.

    ``validate_module_entrypoint`` (reused above, unmodified) is a closure
    over ``ops.td02c_authenticated_get_runner``'s own
    ``__package__``/``__spec__``/``__file__`` globals: calling it from here
    only proves that *other* module, and the ``ops`` package it lives in,
    are correctly deployed and importable from the discovered checkout. It
    proves nothing about this module's own identity. This function performs
    the equivalent proof against this module's own globals, following the
    same idiom (same exception type, same ``discover_service``/``Runner``
    lookup for the expected checkout directory).
    """
    if __package__ != "ops" or __spec__ is None or __spec__.name != EXPECTED_MODULE:
        raise RunnerFailure("client_module_not_importable_from_checkout")

    service = discover_service(Runner(), service_unit)
    working_directory = service.working_directory.resolve(strict=True)
    resolved_file = Path(__file__).resolve()

    # Sub-requirement 1+4: the resolved __file__ must fall under the
    # discovered checkout's working_directory -- not merely "is a module
    # with this name importable somewhere on sys.path." A same-named module
    # importable from a PYTHONPATH-injected duplicate elsewhere resolves
    # outside working_directory and is rejected here.
    try:
        resolved_file.relative_to(working_directory)
    except ValueError:
        raise RunnerFailure("client_module_not_importable_from_checkout") from None

    # Sub-requirement 2: not just "somewhere under the checkout" -- the
    # loaded file must be exactly the expected one.
    expected_file = (working_directory / "ops" / "bulk_v2_canary_client.py").resolve()
    if resolved_file != expected_file:
        raise RunnerFailure("client_module_path_mismatch")

    # Sub-requirement 3: main is reachable and not shadowed by some other
    # module's main -- its defining file must match the same identity just
    # proven above.
    try:
        entrypoint_file = Path(inspect.getfile(main)).resolve()
    except TypeError:
        raise RunnerFailure("client_entrypoint_identity_mismatch") from None
    if entrypoint_file != expected_file:
        raise RunnerFailure("client_entrypoint_identity_mismatch")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-unit", default="django.service")
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--username", required=True, type=str)
    parser.add_argument("--user-id", required=True, type=int)
    parser.add_argument("--request-id", required=True, type=str)
    parser.add_argument("--template-id", required=True, type=str)
    parser.add_argument("--template-name", required=True, type=str)
    parser.add_argument("--subject", required=True, type=str)
    parser.add_argument("--csv-path", required=True, type=Path)
    parser.add_argument("--max-rows", type=int, default=20)
    args = parser.parse_args(argv)

    log = SafeDiagnosticLog(sys.stdout)
    started = time.monotonic()
    try:
        # Reuse note: validate_module_entrypoint is defined in and closes
        # over ops.td02c_authenticated_get_runner's own __package__/__spec__
        # globals, so it validates that module's identity, not this one's --
        # it cannot be generically parameterized without modifying that
        # module, which is out of scope here. Kept as the mandated reuse
        # (it still proves the ops package itself is correctly deployed and
        # importable from the discovered service checkout).
        validate_module_entrypoint(args.service_unit)
    except (DeploymentError, OSError, RunnerFailure, subprocess.SubprocessError) as exc:
        classification = str(exc).split(":", 1)[0]
        log.emit(StageDiagnostic("entrypoint_validated", "FAIL", 1, time.monotonic() - started, classification))
        return 1
    log.emit(StageDiagnostic("entrypoint_validated", "PASS", 0, time.monotonic() - started))

    started = time.monotonic()
    try:
        # Runs alongside, not in place of, validate_module_entrypoint above:
        # that call proves the ops package/td02c_authenticated_get_runner
        # are correctly deployed; this proves this module (and its own
        # main entrypoint) resolve to that same expected checkout.
        validate_client_module_identity(args.service_unit)
    except (DeploymentError, OSError, RunnerFailure, subprocess.SubprocessError) as exc:
        classification = str(exc).split(":", 1)[0]
        log.emit(StageDiagnostic("client_module_identity_validated", "FAIL", 1, time.monotonic() - started, classification))
        return 1
    log.emit(StageDiagnostic("client_module_identity_validated", "PASS", 0, time.monotonic() - started))

    profile = CanaryProfile(
        request_id=args.request_id,
        user_id=args.user_id,
        template_id=args.template_id,
        template_name=args.template_name,
        subject=args.subject,
        max_rows=args.max_rows,
    )
    started = time.monotonic()
    try:
        # Defense in depth: run_canary already classifies and swallows any
        # unexpected exception itself and always returns an int, but the CLI
        # entrypoint must never crash with a raw traceback even if that
        # invariant were somehow violated -- same bare except Exception
        # idiom used for the two validate_*_identity calls above.
        return run_canary(
            profile,
            sys.stdout,
            csv_path=args.csv_path,
            credential_file=args.credential_file,
            service_unit=args.service_unit,
            username=args.username,
        )
    except Exception as exc:
        classification = str(exc) if str(exc) in ERROR_CODES else "unexpected_error"
        log.emit(StageDiagnostic("canary_client", "FAIL", 1, time.monotonic() - started, classification))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
