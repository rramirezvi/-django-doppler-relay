"""Fail-closed classification for deployment-time validation commands.

Django test suites that create a test database belong to an isolated
environment.  Production deployment checks must remain read-only.
"""

from __future__ import annotations

import dataclasses
import enum
import re
from collections.abc import Sequence


FULL_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")

# Minimum passed-test floors for predeployment evidence.  These are floors,
# not exact snapshots: the underlying suites grow over time as legitimate
# coverage is added, so evidence must meet or exceed the current baseline
# rather than match a historical count exactly. Bump a floor only when the
# corresponding suite's real count has grown and stayed there.
MINIMUM_API_V2_PASSED = 27
MINIMUM_HTTP_CLIENT_PASSED = 32
MINIMUM_OPS_PASSED = 257


class TestProfile(str, enum.Enum):
    ISOLATED = "isolated"
    PRODUCTION = "production"


class TestProfileError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class ValidationEvidence:
    target_sha: str
    commit_sequence: tuple[str, ...]
    api_v2_passed: int
    http_client_passed: int
    ops_passed: int
    linux_repetitions_passed: bool
    postgresql_major: int


@dataclasses.dataclass(frozen=True)
class BootstrapEvidence:
    """Evidence for ops.deployment_hardening's bootstrap-existing-component mode.

    Deliberately a distinct schema from ValidationEvidence (adds
    authorized_paths) so a bootstrap evidence file can never satisfy the
    normal predeployment evidence schema check, and vice versa.
    """

    target_sha: str
    commit_sequence: tuple[str, ...]
    authorized_paths: tuple[str, ...]
    api_v2_passed: int
    http_client_passed: int
    ops_passed: int
    linux_repetitions_passed: bool
    postgresql_major: int


def requires_django_test_database(argv: Sequence[str]) -> bool:
    """Return whether argv invokes Django's database-creating test runner."""
    normalized = [str(value).strip().lower() for value in argv]
    return any(
        normalized[index].endswith("manage.py")
        and index + 1 < len(normalized)
        and normalized[index + 1] == "test"
        for index in range(len(normalized))
    )


def validate_test_command(
    profile: TestProfile, argv: Sequence[str], *, effective_user: str
) -> None:
    """Reject database-creating or privilege-escalating tests in production."""
    normalized = [str(value).strip().lower() for value in argv]
    rendered = " ".join(normalized)
    if profile is TestProfile.PRODUCTION:
        if requires_django_test_database(argv):
            raise TestProfileError(
                "Django test-database suites are forbidden in production"
            )
        if effective_user.lower() in {"root", "postgres"}:
            raise TestProfileError(
                "Production validation must not use a privileged database user"
            )
        forbidden = ("createdb", "create database", "alter role", "superuser")
        if any(token in rendered for token in forbidden):
            raise TestProfileError(
                "Production validation must not grant privileges or create databases"
            )


def validate_predeployment_evidence(
    evidence: ValidationEvidence,
    *,
    target_sha: str,
    expected_commits: Sequence[str],
) -> None:
    """Bind approved isolated evidence to one exact immutable Git range."""
    values = (target_sha, evidence.target_sha, *expected_commits,
              *evidence.commit_sequence)
    if not all(FULL_COMMIT.fullmatch(value) for value in values):
        raise TestProfileError("Evidence contains a non-full commit ID")
    if evidence.target_sha != target_sha:
        raise TestProfileError("Evidence target does not match deployment target")
    if evidence.commit_sequence != tuple(expected_commits):
        raise TestProfileError("Evidence commit sequence does not match target range")
    if not expected_commits or expected_commits[-1] != target_sha:
        raise TestProfileError("Expected commit sequence does not end at target")
    if evidence.api_v2_passed < MINIMUM_API_V2_PASSED:
        raise TestProfileError("API V2 isolated evidence is incomplete")
    if evidence.http_client_passed < MINIMUM_HTTP_CLIENT_PASSED:
        raise TestProfileError("HTTP client evidence is incomplete")
    if evidence.ops_passed < MINIMUM_OPS_PASSED:
        raise TestProfileError("Ops suite evidence is incomplete")
    if not evidence.linux_repetitions_passed:
        raise TestProfileError("Linux deterministic repetitions did not pass")
    if evidence.postgresql_major != 17:
        raise TestProfileError("PostgreSQL isolated evidence is for another major")


def validate_bootstrap_evidence(
    evidence: BootstrapEvidence,
    *,
    target_sha: str,
    expected_commits: Sequence[str],
    authorized_paths: Sequence[str],
) -> None:
    """Bind bootstrap-existing-component evidence to one exact Git range and
    one exact, closed set of authorized paths.

    This exists only for ops.deployment_hardening's bootstrap-existing-
    component mode, used when the currently-installed predeployment evidence
    gate itself blocks deploying its own fix. It never runs
    validate_predeployment_evidence and is not a substitute for it.
    """
    values = (target_sha, evidence.target_sha, *expected_commits,
              *evidence.commit_sequence)
    if not all(FULL_COMMIT.fullmatch(value) for value in values):
        raise TestProfileError("Bootstrap evidence contains a non-full commit ID")
    if evidence.target_sha != target_sha:
        raise TestProfileError("Bootstrap evidence target does not match deployment target")
    if evidence.commit_sequence != tuple(expected_commits):
        raise TestProfileError("Bootstrap evidence commit sequence does not match target range")
    if not expected_commits or expected_commits[-1] != target_sha:
        raise TestProfileError("Bootstrap expected commit sequence does not end at target")
    if tuple(sorted(evidence.authorized_paths)) != tuple(sorted(authorized_paths)):
        raise TestProfileError("Bootstrap evidence authorized paths do not match the allowlist")
    if evidence.api_v2_passed < MINIMUM_API_V2_PASSED:
        raise TestProfileError("Bootstrap API V2 isolated evidence is incomplete")
    if evidence.http_client_passed < MINIMUM_HTTP_CLIENT_PASSED:
        raise TestProfileError("Bootstrap HTTP client evidence is incomplete")
    if evidence.ops_passed < MINIMUM_OPS_PASSED:
        raise TestProfileError("Bootstrap ops suite evidence is incomplete")
    if not evidence.linux_repetitions_passed:
        raise TestProfileError("Bootstrap Linux deterministic repetitions did not pass")
    if evidence.postgresql_major != 17:
        raise TestProfileError("Bootstrap PostgreSQL isolated evidence is for another major")
