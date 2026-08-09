"""bulk-v2-real-send-canary authorization gate (design.md §9).

Pure function module: no ORM import, no `django.db`, no `settings` import,
no I/O, no HTTP, no filesystem access. Every input is passed in as a keyword
argument. This is deliberate — it is what makes `evaluate_real_send` fully
testable before any send-capable code exists (PR1 of
openspec/changes/bulk-v2-real-send-canary).

The only permitted coupling to `relay.services.bulk_v2_canary` is
`normalize_allowlist` (design.md §9: "the *only* permitted reuse; no other
symbol is imported from that module"). `evaluate_real_send` never calls
`evaluate_canary`, in either direction, on any path, and shares no parameter
name with `evaluate_canary`'s signature.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from relay.services.bulk_v2_canary import normalize_allowlist


@dataclass(frozen=True)
class RealSendDecision:
    allowed: bool
    code: str
    message: str


def evaluate_real_send(
    *,
    real_send_enabled: bool,
    user_allowlist: str | Iterable[Any],
    request_allowlist: str | Iterable[Any],
    template_allowlist: str | Iterable[Any],
    recipient_domain_allowlist: str | Iterable[Any],
    max_rows: Any,
    user_id: int | None,
    client_request_id: str,
    template_id: str,
    recipient_domains: Iterable[str],
    eligible_row_count: int,
) -> RealSendDecision:
    if not real_send_enabled:
        return RealSendDecision(
            False, "real_send_disabled", "El envio real V2 no esta habilitado."
        )

    user_ids, user_error = normalize_allowlist(user_allowlist, integer=True)
    request_ids, request_error = normalize_allowlist(request_allowlist)
    template_ids, template_error = normalize_allowlist(template_allowlist)
    domain_allowlist, domain_error = normalize_allowlist(
        recipient_domain_allowlist
    )
    # Mirrors ops/td02c_settings_gate.py:77's `type(max_rows) is not int or
    # max_rows != 20` precedent: MAX_ROWS must be exactly the int 1, not
    # merely equal to 1 after coercion (a string "1" is invalid).
    max_rows_invalid = type(max_rows) is not int or max_rows != 1
    row_limit = max_rows if not max_rows_invalid else None

    if (
        user_error
        or request_error
        or template_error
        or domain_error
        or max_rows_invalid
    ):
        return RealSendDecision(
            False,
            "real_send_config_invalid",
            "La configuracion de envio real V2 no es valida.",
        )

    if not user_ids:
        return RealSendDecision(
            False,
            "real_send_user_allowlist_empty",
            "No hay usuarios autorizados para el envio real V2.",
        )
    if not request_ids:
        return RealSendDecision(
            False,
            "real_send_request_allowlist_empty",
            "No hay solicitudes autorizadas para el envio real V2.",
        )
    if not template_ids:
        return RealSendDecision(
            False,
            "real_send_template_allowlist_empty",
            "No hay plantillas autorizadas para el envio real V2.",
        )
    if not domain_allowlist:
        return RealSendDecision(
            False,
            "real_send_domain_allowlist_empty",
            "No hay dominios autorizados para el envio real V2.",
        )

    request_id = str(client_request_id or "").strip()
    if not request_id:
        return RealSendDecision(
            False,
            "real_send_request_id_required",
            "client_request_id es obligatorio.",
        )
    if request_id not in request_ids:
        return RealSendDecision(
            False,
            "real_send_request_not_allowed",
            "La solicitud no esta autorizada para el envio real V2.",
        )
    if not user_id or int(user_id) not in user_ids:
        return RealSendDecision(
            False,
            "real_send_user_not_allowed",
            "El usuario no esta autorizado para el envio real V2.",
        )
    if str(template_id or "").strip() not in {str(t) for t in template_ids}:
        return RealSendDecision(
            False,
            "real_send_template_not_allowed",
            "La plantilla no esta autorizada para el envio real V2.",
        )
    domains = {str(d or "").strip().lower() for d in recipient_domains}
    allowed_domains = {str(d).strip().lower() for d in domain_allowlist}
    if not domains or not domains.issubset(allowed_domains):
        return RealSendDecision(
            False,
            "real_send_domain_not_allowed",
            "El dominio del destinatario no esta autorizado para el envio real V2.",
        )

    if eligible_row_count == 0:
        return RealSendDecision(
            False,
            "real_send_row_count_empty",
            "No hay filas elegibles para el envio real V2.",
        )
    if eligible_row_count > row_limit:
        return RealSendDecision(
            False,
            "real_send_row_limit_exceeded",
            "El envio real V2 excede el limite de filas permitido.",
        )

    return RealSendDecision(True, "real_send_allowed", "Envio real V2 autorizado.")
