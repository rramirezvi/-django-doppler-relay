from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

VALID_REPORT_TYPES = {
    "deliveries",
    "bounces",
    "opens",
    "clicks",
    "spam",
    "unsubscribed",
    "sent",
}


def _poll_cfg():
    cfg = getattr(settings, "DOPPLER_REPORTS", {}) or {}
    return {
        "DEFAULT_TIMEOUT": int(cfg.get("TIMEOUT", 30)),
        "POLL_INITIAL_DELAY": int(cfg.get("POLL_INITIAL_DELAY", 2)),
        "POLL_MAX_DELAY": int(cfg.get("POLL_MAX_DELAY", 60)),
        "POLL_TOTAL_TIMEOUT": int(cfg.get("POLL_TOTAL_TIMEOUT", 15 * 60)),
    }


def _attachments_root() -> Path:
    return Path(settings.BASE_DIR) / "attachments" / "reports"


class ReportError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, payload: Any | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload


def _require_setting(name: str) -> str:
    value = getattr(settings, name, None)
    if value:
        return str(value).strip()

    if name.startswith('DOPPLER_RELAY_'):
        cfg = getattr(settings, 'DOPPLER_RELAY', {}) or {}
        nested_key = name.replace('DOPPLER_RELAY_', '')
        nested_value = cfg.get(nested_key)
        if nested_value:
            return str(nested_value).strip()

    raise ReportError(f'La configuracion {name} no esta definida')


def _api_key() -> str:
    return _require_setting("DOPPLER_RELAY_API_KEY")


def _base_url() -> str:
    base = _require_setting("DOPPLER_RELAY_BASE_URL")
    return base.rstrip("/\r\n")


def _account_id() -> str:
    return _require_setting("DOPPLER_RELAY_ACCOUNT_ID")


def _headers(accept: str, *, content_type: str | None = None) -> Dict[str, str]:
    headers: Dict[str, str] = {
        "Authorization": f"token {_api_key()}",
        "Accept": accept,
        "User-Agent": "django-doppler-relay-report-client/2.0",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _iso_datetime(value: date, *, end: bool = False) -> str:
    base_time = dt_time.max.replace(microsecond=0) if end else dt_time.min
    combined = datetime.combine(value, base_time)
    if timezone.is_naive(combined) and getattr(settings, "USE_TZ", False):
        combined = timezone.make_aware(combined, timezone.get_default_timezone())
    return combined.isoformat()


def _endpoint() -> str:
    return f"{_base_url()}/reports/reportrequest"


def _extract_report_id_from_href(href: str | None) -> str | None:
    if not href:
        return None
    candidate = href if "://" in href else (f"https://placeholder{href}" if href.startswith("/") else f"https://placeholder/{href}")
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.query:
        values = parse_qs(parsed.query).get("reportRequestId")
        if values:
            return str(values[0]).strip()
    tail = parsed.path.rsplit("/", 1)[-1].strip()
    if tail and tail.isdigit():
        return tail
    return None


def _extract_report_id(data: Dict[str, Any] | None, *, location: str | None = None) -> str | None:
    payload = data or {}
    for key in ("reportRequestId", "createdResourceId", "id"):
        raw = payload.get(key)
        if raw:
            return str(raw).strip()
    links = payload.get("_links") or payload.get("links")
    if isinstance(links, list):
        for item in links:
            href = item.get("href") if isinstance(item, dict) else None
            report_id = _extract_report_id_from_href(href)
            if report_id:
                return report_id
    if location:
        report_id = _extract_report_id_from_href(location)
        if report_id:
            return report_id
    return None


def create_report_request(start_date: date, end_date: date, report_type: str) -> str:
    normalized_type = str(report_type or "").strip().lower()
    if normalized_type not in VALID_REPORT_TYPES:
        raise ReportError(f"Tipo de reporte no soportado: {report_type}")
    if start_date > end_date:
        raise ReportError("La fecha inicial no puede ser posterior a la final")

    cfg = _poll_cfg()

    body = {
        "start_date": _iso_datetime(start_date),
        "end_date": _iso_datetime(end_date, end=True),
        "resource": normalized_type,
        "accountId": _account_id(),
    }

    logger.info("Solicitando reporte %s de %s a %s", normalized_type, start_date, end_date)

    try:
        response = requests.post(
            _endpoint(),
            json=body,
            headers=_headers("application/json", content_type="application/json"),
            timeout=cfg["DEFAULT_TIMEOUT"],
        )
    except requests.RequestException as exc:
        logger.error("Fallo la peticion de creacion de reporte: %s", exc)
        raise ReportError(f"Error realizando la peticion HTTP: {exc}") from exc

    if response.status_code not in {200, 201, 202}:
        snippet = response.text.strip()[:300]
        logger.error("Respuesta inesperada al crear reporte (%s): %s", response.status_code, snippet)
        raise ReportError(
            "Error creando el reporte",
            status=response.status_code,
            payload=snippet or None,
        )

    data: Dict[str, Any] = {}
    if response.content:
        try:
            data = response.json()
        except ValueError as exc:
            snippet = response.text.strip()[:300]
            logger.error("Respuesta JSON invalida al crear reporte: %s", snippet)
            raise ReportError("Respuesta JSON invalida al crear reporte", payload=snippet) from exc

    report_id = _extract_report_id(data, location=response.headers.get("Location"))
    if not report_id:
        logger.error("No se pudo obtener el identificador del reporte: %s", data)
        raise ReportError("La respuesta de creacion de reporte no contiene un identificador", payload=str(data) or None)

    logger.info("Reporte solicitado correctamente. ID=%s", report_id)
    return report_id


def wait_until_processed(report_id: str, *, timeout: int | None = None) -> Dict[str, Any]:
    cfg = _poll_cfg()
    total_timeout = int(timeout if timeout is not None else cfg["POLL_TOTAL_TIMEOUT"])
    delay = cfg["POLL_INITIAL_DELAY"]
    max_delay = cfg["POLL_MAX_DELAY"]
    logger.info("Esperando procesamiento del reporte %s", report_id)
    start = time.monotonic()

    while True:
        elapsed = time.monotonic() - start
        if elapsed > total_timeout:
            logger.error("Tiempo excedido esperando el reporte %s", report_id)
            raise ReportError("Tiempo de espera excedido al procesar el reporte")

        try:
            response = requests.get(
                _endpoint(),
                params={"reportRequestId": report_id},
                headers=_headers("application/json"),
                timeout=cfg["DEFAULT_TIMEOUT"],
            )
        except requests.RequestException as exc:
            logger.error("Error consultando estado del reporte %s: %s", report_id, exc)
            raise ReportError(f"Error consultando estado del reporte: {exc}") from exc

        if response.status_code not in {200, 202}:
            snippet = response.text.strip()[:300]
            logger.error("Respuesta inesperada al consultar estado (%s): %s", response.status_code, snippet)
            raise ReportError(
                "Error consultando estado del reporte",
                status=response.status_code,
                payload=snippet or None,
            )

        if response.status_code == 202 or not response.content:
            logger.debug("Reporte %s aun en proceso (HTTP %s)", report_id, response.status_code)
        else:
            try:
                data = response.json()
            except ValueError as exc:
                snippet = response.text.strip()[:300]
                logger.error("Respuesta JSON invalida al consultar estado: %s", snippet)
                raise ReportError("Respuesta JSON invalida al consultar estado", payload=snippet) from exc

            if data.get("processed"):
                logger.info("Reporte %s procesado", report_id)
                return data

            logger.debug("Reporte %s pendiente segun payload: %s", report_id, data)

        time.sleep(delay)
        delay = min(delay * 2, max_delay)


def build_report_filename(report_type: str) -> str:
    timestamp = timezone.now().strftime("%Y%m%d_%H%M")
    safe_type = report_type or "report"
    return f"doppler_{safe_type}_{timestamp}.csv"


def download_report_csv(report_id: str) -> bytes:
    cfg = _poll_cfg()
    logger.info("Descargando CSV del reporte %s", report_id)

    try:
        response = requests.get(
            _endpoint(),
            params={"reportRequestId": report_id, "format": "csv"},
            headers=_headers("text/csv"),
            timeout=cfg["DEFAULT_TIMEOUT"],
        )
    except requests.RequestException as exc:
        logger.error("Error consultando CSV del reporte %s: %s", report_id, exc)
        raise ReportError(f"Error descargando reporte CSV: {exc}") from exc

    content_type = (response.headers.get("Content-Type") or "").lower()
    if response.status_code == 200 and "text/csv" in content_type:
        logger.info("Reporte %s descargado directamente como CSV", report_id)
        return response.content

    try:
        data = response.json()
    except ValueError as exc:
        snippet = response.text.strip()[:300]
        logger.error("Respuesta invalida al descargar reporte %s: %s", report_id, snippet)
        raise ReportError("No se pudo interpretar la respuesta del reporte", payload=snippet) from exc

    file_path = data.get("file_path") or next(
        (link.get("href") for link in data.get("_links", []) if "files.dopplerrelay.com" in (link.get("href") or "")),
        None,
    )
    if not file_path:
        snippet = response.text.strip()[:300]
        logger.error("No se encontro file_path en la respuesta del reporte %s", report_id)
        raise ReportError("No se encontro el archivo del reporte", payload=snippet or None)

    logger.info("Descargando CSV desde enlace externo para reporte %s", report_id)
    try:
        follow = requests.get(file_path, timeout=120)
        follow.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Error descargando archivo externo %s: %s", file_path, exc)
        raise ReportError(f"Error descargando archivo externo: {exc}") from exc

    return follow.content


# Expose paths/helpers for consumers
ATTACHMENTS_ROOT = _attachments_root()

