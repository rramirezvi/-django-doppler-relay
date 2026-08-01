"""Fail-closed validation of effective TD-02C canary settings.

The gate intentionally delegates allowlist parsing to the production canary
policy.  It additionally requires the raw environment representation to be
canonical so operational activation cannot hide spaces, duplicates or extra
values behind normalization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from relay.services.bulk_v2_canary import normalize_allowlist


CANARY_REQUEST_ID = "td02c-canary-import-v1-20260731"
CANARY_USER_ID = 1


@dataclass(frozen=True)
class SettingsGateResult:
    allowed: bool
    code: str
    reasons: tuple[str, ...] = ()


def evaluate_effective_settings(
    *,
    engine_enabled: Any,
    canary_enabled: Any,
    request_allowlist: Any,
    user_allowlist: Any,
    max_rows: Any,
    allow_external_template_lookup: Any,
    expect_active: bool,
) -> SettingsGateResult:
    """Validate the exact active or inactive operational configuration."""
    reasons: list[str] = []

    request_ids, request_error = normalize_allowlist(request_allowlist)
    user_ids, user_error = normalize_allowlist(user_allowlist, integer=True)
    expected_requests = (CANARY_REQUEST_ID,) if expect_active else ()
    expected_users = (CANARY_USER_ID,) if expect_active else ()
    expected_request_raw = CANARY_REQUEST_ID if expect_active else ""
    expected_user_raw = str(CANARY_USER_ID) if expect_active else ""

    if request_error or request_ids != expected_requests:
        reasons.append("request_allowlist_mismatch")
    if user_error or user_ids != expected_users:
        reasons.append("user_allowlist_mismatch")
    if (
        not isinstance(request_allowlist, str)
        or request_allowlist != expected_request_raw
    ):
        reasons.append("request_allowlist_not_canonical")
    if not isinstance(user_allowlist, str) or user_allowlist != expected_user_raw:
        reasons.append("user_allowlist_not_canonical")

    expected_flag = expect_active
    if engine_enabled is not expected_flag:
        reasons.append("engine_flag_mismatch")
    if canary_enabled is not expected_flag:
        reasons.append("canary_flag_mismatch")
    if type(max_rows) is not int or max_rows != 20:
        reasons.append("max_rows_mismatch")
    if allow_external_template_lookup is not False:
        reasons.append("external_lookup_must_be_false")

    if reasons:
        return SettingsGateResult(False, "settings_gate_failed", tuple(reasons))
    return SettingsGateResult(
        True,
        "canary_settings_active" if expect_active else "canary_settings_inactive",
    )


def evaluate_django_settings(settings: Any, *, expect_active: bool) -> SettingsGateResult:
    """Read the six effective values from a Django settings object."""
    return evaluate_effective_settings(
        engine_enabled=settings.BULK_PROCESSING_ENGINE_V2,
        canary_enabled=settings.BULK_PROCESSING_V2_CANARY_ENABLED,
        request_allowlist=settings.BULK_PROCESSING_V2_CANARY_REQUEST_IDS,
        user_allowlist=settings.BULK_PROCESSING_V2_CANARY_USER_IDS,
        max_rows=settings.BULK_PROCESSING_V2_CANARY_MAX_ROWS,
        allow_external_template_lookup=(
            settings.BULK_PROCESSING_V2_ALLOW_EXTERNAL_TEMPLATE_LOOKUP
        ),
        expect_active=expect_active,
    )
