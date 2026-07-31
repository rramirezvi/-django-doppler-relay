from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class CanaryDecision:
    allowed: bool
    code: str
    message: str


def _normalize_allowlist(
    value: str | Iterable[Any], *, integer: bool = False
) -> tuple[tuple[str | int, ...], str | None]:
    raw = value.split(",") if isinstance(value, str) else list(value or ())
    if isinstance(value, str) and not value.strip():
        raw = []
    normalized: list[str | int] = []
    for item in raw:
        text = str(item).strip()
        if not text or any(char in text for char in ("*", "?", "[", "]")):
            return (), "canary_config_invalid"
        if integer:
            try:
                parsed: str | int = int(text)
            except ValueError:
                return (), "canary_config_invalid"
            if parsed <= 0:
                return (), "canary_config_invalid"
        else:
            parsed = text
        normalized.append(parsed)
    if len(set(normalized)) != len(normalized):
        return (), "canary_config_invalid"
    return tuple(normalized), None


def evaluate_canary(
    *,
    engine_enabled: bool,
    canary_enabled: bool,
    request_allowlist: str | Iterable[Any],
    user_allowlist: str | Iterable[Any],
    max_rows: Any,
    allow_external_template_lookup: bool,
    client_request_id: str,
    user_id: int | None,
    total_rows: int,
    send_now: bool,
    scheduled_at: str,
    import_only: bool,
) -> CanaryDecision:
    if not engine_enabled:
        return CanaryDecision(False, "engine_disabled", "El motor v2 no esta habilitado.")
    if not canary_enabled:
        return CanaryDecision(
            False, "canary_disabled", "La activacion canary no esta habilitada."
        )
    request_ids, request_error = _normalize_allowlist(request_allowlist)
    user_ids, user_error = _normalize_allowlist(user_allowlist, integer=True)
    try:
        row_limit = int(max_rows)
    except (TypeError, ValueError):
        row_limit = 0
    if request_error or user_error or row_limit <= 0:
        return CanaryDecision(
            False, "canary_config_invalid", "La configuracion canary no es valida."
        )
    if not request_ids:
        return CanaryDecision(
            False, "request_allowlist_empty", "No hay solicitudes canary autorizadas."
        )
    if not user_ids:
        return CanaryDecision(
            False, "user_allowlist_empty", "No hay usuarios canary autorizados."
        )
    request_id = str(client_request_id or "").strip()
    if not request_id:
        return CanaryDecision(
            False, "request_id_required", "client_request_id es obligatorio."
        )
    if request_id not in request_ids:
        return CanaryDecision(False, "request_not_allowed", "La solicitud no esta autorizada.")
    if not user_id or int(user_id) not in user_ids:
        return CanaryDecision(False, "user_not_allowed", "El usuario no esta autorizado.")
    if total_rows <= 0:
        return CanaryDecision(False, "row_count_empty", "El CSV no contiene filas.")
    if total_rows > row_limit:
        return CanaryDecision(
            False, "row_limit_exceeded", "El CSV excede el limite canary."
        )
    if send_now:
        return CanaryDecision(False, "send_not_allowed", "El canary solo permite importacion.")
    if str(scheduled_at or "").strip():
        return CanaryDecision(
            False, "schedule_not_allowed", "El canary no permite programacion."
        )
    if not import_only:
        return CanaryDecision(
            False, "import_only_required", "El canary requiere modo import-only."
        )
    if allow_external_template_lookup:
        return CanaryDecision(
            False,
            "external_lookup_not_allowed",
            "El canary no permite consultas externas de plantilla.",
        )
    return CanaryDecision(True, "canary_allowed", "Canary autorizado.")


def count_uploaded_csv_rows(uploaded_file: Any, *, stop_after: int) -> int:
    if stop_after < 1:
        raise ValueError("stop_after debe ser positivo")
    uploaded_file.seek(0)
    wrapper = io.TextIOWrapper(
        uploaded_file, encoding="utf-8-sig", errors="strict", newline=""
    )
    try:
        sample = wrapper.read(4096)
        if not sample:
            return 0
        wrapper.seek(0)
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;").delimiter
        except csv.Error:
            delimiter = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.reader(wrapper, delimiter=delimiter)
        try:
            next(reader)
        except StopIteration:
            return 0
        count = 0
        for row in reader:
            if not any(str(cell or "").strip() for cell in row):
                continue
            count += 1
            if count > stop_after:
                break
        return count
    finally:
        wrapper.detach()
        uploaded_file.seek(0)
