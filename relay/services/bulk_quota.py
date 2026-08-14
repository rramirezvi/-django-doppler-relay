"""bulk-v2 quota guard -- infrastructure primitives (design round 5/6, PR A).

DORMANT: nothing in this module is called from the send path yet, and
nothing outside this module and its own tests may import it. PR B will
add `reserve_and_claim` (fusing these primitives with the recipient claim
in ONE transaction.atomic() block) and wire it into
relay/services/bulk_v2_send.py's claim loop, behind
`settings.DOPPLER_QUOTA_GUARD_ENABLED` (default False).

Every function below is a private, composable primitive -- prefixed with
`_` deliberately. There is NO public `reserve_quota()` / `reserve_and_claim()`
in this module, and none should ever be added here: the only sanctioned way
to combine quota reservation with a recipient claim is PR B's
`reserve_and_claim` (lives with the V2 send path, not here), and the only
sanctioned way to reserve quota WITHOUT a recipient claim (V1's temporary,
documented-as-removable bridge) is PR E's `reserve_quota_only`. Both will
compose the primitives below; neither is defined in this module.

Global lock-order rule (design round 6, point 2): RECIPIENT -> MONTH -> DAY
-> HOUR is the only compound lock order that exists or will exist in this
system, and it belongs exclusively to `reserve_and_claim` (PR B). This
module never acquires a BulkSendRecipient/BackgroundJob/BulkSend lock --
only QuotaWindow rows, always in MONTH -> DAY -> HOUR order.

All window boundaries are UTC (Doppler's contractually confirmed quota
timezone), independent of `settings.TIME_ZONE` -- deliberately never read
here.

Zero imports from relay.services.doppler_relay, relay.services.bulk_v2_send,
relay.services.bulk_v2_send_state, relay.services.jobs, any management
command, or any HTTP transport library -- this module has no knowledge
Doppler exists.
"""

from __future__ import annotations

import math
from datetime import datetime
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F

from relay.models import QuotaWindow

# The only sanctioned sub-order for QuotaWindow locks, anywhere.
_WINDOW_ORDER = (QuotaWindow.WINDOW_MONTH, QuotaWindow.WINDOW_DAY, QuotaWindow.WINDOW_HOUR)

_LIMIT_SETTING_NAMES = {
    QuotaWindow.WINDOW_MONTH: "DOPPLER_QUOTA_MONTHLY_LIMIT",
    QuotaWindow.WINDOW_DAY: "DOPPLER_QUOTA_DAILY_LIMIT",
    QuotaWindow.WINDOW_HOUR: "DOPPLER_QUOTA_HOURLY_LIMIT",
}


class QuotaConfigurationError(Exception):
    """Raised by `_resolve_effective_limit` on any invalid/missing/
    unsupported quota configuration. Fail-closed: never silently treated
    as "unlimited" or ignored."""


class QuotaExhausted(Exception):
    """Raised by `_validate_capacity` when a window has no remaining
    capacity. Not a wrapper for reserving/claiming -- a plain signal type
    the PR B/E callers catch to stop cleanly."""

    def __init__(self, window_type: str, limit_value: int, consumed: int):
        self.window_type = window_type
        self.limit_value = limit_value
        self.consumed = consumed
        super().__init__(
            f"Quota exhausted for window_type={window_type}: {consumed}/{limit_value}"
        )


def _resolve_window_starts(now: datetime) -> dict[str, datetime]:
    """UTC month/day/hour boundaries for `now`, per Doppler's contractually
    confirmed UTC-0 quota windows (design round 4). Never reads the
    project's local timezone setting -- `now` must already be tz-aware."""
    now_utc = now.astimezone(dt_timezone.utc)
    return {
        QuotaWindow.WINDOW_MONTH: now_utc.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ),
        QuotaWindow.WINDOW_DAY: now_utc.replace(hour=0, minute=0, second=0, microsecond=0),
        QuotaWindow.WINDOW_HOUR: now_utc.replace(minute=0, second=0, microsecond=0),
    }


def _resolve_effective_limit(window_type: str) -> int:
    """Resolves the limit for a NEW window row, once, at creation time
    (design round 6, point 5: the result is never rewritten afterward).

    Fail-closed on any of: missing/non-positive configured limit, overage
    requested (DOPPLER_OVERAGE_CONTRACT_AMBIGUOUS, design round 4 --
    overage is not implemented, ever raises here), an out-of-range safety
    margin, or a margin that collapses the effective limit to <= 0.
    """
    setting_name = _LIMIT_SETTING_NAMES[window_type]
    configured = getattr(settings, setting_name, 0)
    if not isinstance(configured, int) or configured <= 0:
        raise QuotaConfigurationError(
            f"{setting_name} debe ser un entero positivo (actual: {configured!r})."
        )

    if getattr(settings, "DOPPLER_QUOTA_OVERAGE_ENABLED", False):
        raise QuotaConfigurationError(
            "DOPPLER_QUOTA_OVERAGE_ENABLED=True no esta soportado todavia "
            "(DOPPLER_OVERAGE_CONTRACT_AMBIGUOUS, design round 4)."
        )

    margin = getattr(settings, "DOPPLER_QUOTA_SAFETY_MARGIN_RATIO", 0.0)
    if not isinstance(margin, (int, float)) or not (0.0 <= margin < 1.0):
        raise QuotaConfigurationError(
            f"DOPPLER_QUOTA_SAFETY_MARGIN_RATIO fuera de rango [0, 1): {margin!r}."
        )

    effective = math.floor(configured * (1.0 - margin))
    if effective <= 0:
        raise QuotaConfigurationError(
            f"{setting_name} tras aplicar el margen de seguridad resulta en "
            f"{effective} (<= 0)."
        )
    return effective


def _get_or_create_window_locked(
    window_type: str, window_start: datetime, limit_value_if_created: int
) -> QuotaWindow:
    """Must be called inside an already-open transaction.atomic() block.
    Returns the row FOR UPDATE, whether it pre-existed or was just
    created.

    Race-safety: `select_for_update()` alone cannot lock a not-yet-existing
    row, so the creation race is resolved by the (window_type, window_start)
    UniqueConstraint -- whichever concurrent INSERT loses gets an
    IntegrityError, caught here (inside its own nested atomic()/SAVEPOINT,
    required so the surrounding transaction is not poisoned by the failed
    INSERT), and the row is then re-read WITH the lock.
    """
    row = (
        QuotaWindow.objects.select_for_update()
        .filter(window_type=window_type, window_start=window_start)
        .first()
    )
    if row is not None:
        return row

    try:
        with transaction.atomic():
            QuotaWindow.objects.create(
                window_type=window_type,
                window_start=window_start,
                limit_value=limit_value_if_created,
                consumed=0,
            )
    except IntegrityError:
        pass  # lost the creation race; row already exists, fall through

    return QuotaWindow.objects.select_for_update().get(
        window_type=window_type, window_start=window_start
    )


def _lock_quota_windows(window_starts: dict[str, datetime]) -> dict[str, QuotaWindow]:
    """Must be called inside an already-open transaction.atomic() block.
    Acquires the three QuotaWindow locks strictly in _WINDOW_ORDER
    (MONTH -> DAY -> HOUR) -- the only sanctioned sub-order anywhere in
    this system."""
    return {
        window_type: _get_or_create_window_locked(
            window_type,
            window_starts[window_type],
            _resolve_effective_limit(window_type),
        )
        for window_type in _WINDOW_ORDER
    }


def _validate_capacity(windows: dict[str, QuotaWindow]) -> None:
    """Raises QuotaExhausted on the FIRST window (in _WINDOW_ORDER) that
    has no remaining capacity. Must run before `_increment_windows` --
    never partially increment."""
    for window_type in _WINDOW_ORDER:
        window = windows[window_type]
        if window.consumed >= window.limit_value:
            raise QuotaExhausted(window_type, window.limit_value, window.consumed)


def _increment_windows(windows: dict[str, QuotaWindow], *, now: datetime) -> None:
    """Increments `consumed` by 1 on all three windows. Only ever call
    after `_validate_capacity` has passed for all three -- this function
    performs no capacity check of its own."""
    for window_type in _WINDOW_ORDER:
        QuotaWindow.objects.filter(pk=windows[window_type].pk).update(
            consumed=F("consumed") + 1, updated_at=now
        )
