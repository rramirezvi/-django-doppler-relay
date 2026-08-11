from __future__ import annotations
from datetime import datetime
import logging
import base64
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple
from collections import deque
from urllib.parse import urljoin

import requests
import time
from tenacity import retry, stop_after_attempt, wait_exponential
from dateutil.parser import isoparse
from django.conf import settings
from django.utils import timezone


DEFAULT_BASE_URL = "https://api.dopplerrelay.com/"
USER_AGENT = "doppler-relay-python/1.0"
ADMIN_USER_AGENT = "relay-admin/1.0"

logger = logging.getLogger(__name__)
_TEMPLATE_CIRCUIT_STATE: Dict[str, Dict[str, Any]] = {}
_TEMPLATE_FAILURE_WINDOW = 60
_TEMPLATE_CIRCUIT_BLOCK = 60


def _debug_api_logs_enabled() -> bool:
    relay_settings = getattr(settings, "DOPPLER_RELAY", {}) or {}
    return bool(getattr(settings, "DOPPLER_RELAY_DEBUG", False) or relay_settings.get("DEBUG_LOGS"))


def _template_circuit_state(account_key: str) -> Dict[str, Any]:
    state = _TEMPLATE_CIRCUIT_STATE.setdefault(
        account_key,
        {"failures": deque(), "block_until": 0.0},
    )
    return state


def _register_template_failure(account_key: str) -> None:
    state = _template_circuit_state(account_key)
    now = time.monotonic()
    failures: deque = state["failures"]
    failures.append(now)
    while failures and now - failures[0] > _TEMPLATE_FAILURE_WINDOW:
        failures.popleft()
    if len(failures) >= 3:
        state["block_until"] = now + _TEMPLATE_CIRCUIT_BLOCK
        logger.error(
            "list_templates circuit open",
            extra={"account": account_key, "block_seconds": _TEMPLATE_CIRCUIT_BLOCK},
        )


def _reset_template_circuit(account_key: str) -> None:
    state = _template_circuit_state(account_key)
    state["failures"].clear()
    state["block_until"] = 0.0


def _templates_count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("items", "templates", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
            if isinstance(value, dict) and isinstance(value.get("items"), list):
                return len(value["items"])
        if {"id", "name"} <= payload.keys():
            return 1
    return 0


def _parse_retry_after(value: str | None) -> float:
    if not value:
        return 1.0
    try:
        return max(float(value), 0.5)
    except ValueError:
        return 1.0



class DopplerRelayError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class DopplerRelayClient:
    def __init__(self, *, api_key: str | None = None, base_url: str | None = None, auth_scheme: str | None = None, timeout: int | None = None, max_attempts: int = 3):
        cfg = settings.DOPPLER_RELAY
        self.base_url = (base_url or cfg.get(
            "BASE_URL", DEFAULT_BASE_URL)).rstrip("/") + "/"
        self.timeout = timeout or cfg.get("TIMEOUT", 30)
        # bulk-v2-real-send-canary (design.md §10/D9): every existing V1
        # construction site calls DopplerRelayClient() with no
        # max_attempts, so self.max_attempts == 3 there and V1's retry
        # bound/backoff/logging are unchanged. V2's real-send path obtains
        # a client via bulk_v2_send.build_single_attempt_client(), which
        # passes max_attempts=1.
        self.max_attempts = max(int(max_attempts), 1)
        self.session = requests.Session()

        # Usar 'token' como esquema de autorización por defecto
        auth_scheme = auth_scheme or cfg.get('AUTH_SCHEME', 'token')
        api_key = api_key or cfg.get('API_KEY', '')

        self.session.headers.update({
            "Authorization": f"{auth_scheme} {api_key}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })

    def send_bulk_email(self, account_id: str, template_id: str, recipients_model: dict) -> dict:
        """
        Envía correos masivos usando una plantilla de Doppler Relay.

        Args:
            account_id: ID de la cuenta en Doppler Relay
            template_id: ID de la plantilla a utilizar
            recipients_model: Modelo con los destinatarios y sus variables

        Returns:
            Diccionario con la respuesta de la API
        """
        logger.info(
            "Enviando bulk email con template %s a %s destinatarios",
            template_id,
            len(recipients_model.get("recipients") or []),
        )
        if _debug_api_logs_enabled():
            logger.debug("Recipients model: %s", recipients_model)

        # Validación de datos básicos
        if not recipients_model.get("recipients"):
            raise ValueError("El modelo no contiene destinatarios")

        try:
            # Enviar utilizando el método de plantillas individual
            return self.send_template_message(
                account_id=account_id,
                template_id=template_id,
                recipients_model=recipients_model
            )
        except Exception as e:
            raise DopplerRelayError(
                f"Error al enviar correo masivo: {str(e)}",
                status=getattr(e, 'status', None),
                payload=getattr(e, 'payload', None)
            )

        # Debug de la configuración (sin mostrar la API key completa)
        debug_info = {
            "base_url": self.base_url,
            "auth_scheme": auth_scheme,
            "api_key": f"{api_key[:4]}...{api_key[-4:]}" if api_key else None,
            "timeout": self.timeout,
        }
        logger.debug("DopplerRelayClient configuracion: %s", debug_info)

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def _raise_for_api(self, resp: requests.Response):
        # Primero, loguear la respuesta completa para depuración cuando se active debug.
        if _debug_api_logs_enabled():
            logger.debug(
                "Doppler request failed: %s %s status=%s request_headers=%s response_headers=%s body=%s",
                resp.request.method,
                resp.request.url,
                resp.status_code,
                dict(resp.request.headers),
                dict(resp.headers),
                resp.text,
            )

        if 200 <= resp.status_code < 300:
            return

        try:
            data = resp.json()
        except Exception:
            # Si no es JSON, guarda el texto plano
            data = resp.text

        # Mostrar el payload enviado y la respuesta para depuración
        error_info = {
            "response": data if isinstance(data, dict) else None,
            "response_text": resp.text if not isinstance(data, dict) else None,
            "request_url": resp.request.url,
            "request_method": resp.request.method,
            "request_headers": dict(resp.request.headers),
            "request_body": resp.request.body.decode('utf-8') if resp.request.body else None,
            "response_headers": dict(resp.headers),
            "response_status": resp.status_code
        }

        # Si es un error de límite excedido, dar un mensaje más amigable
        if resp.status_code == 402 and isinstance(data, dict) and data.get("errorCode") == 1:
            reset_date = datetime.fromisoformat(
                data["resetDate"].replace("Z", "+00:00"))
            reset_date_local = reset_date.astimezone(
                timezone.localtime().tzinfo)
            error_message = (
                f"Se ha alcanzado el límite de envíos ({data['deliveriesCount']}/{data['limit']} "
                f"envíos {data['period']}). El límite se reiniciará el "
                f"{reset_date_local.strftime('%Y-%m-%d %H:%M:%S')} hora local."
            )
        else:
            # Para otros errores, mostrar información detallada
            error_message = f"HTTP {resp.status_code} en {resp.request.method} {resp.request.url}\n"
            if isinstance(data, dict):
                if "title" in data:
                    error_message += f"\nError: {data['title']}"
                if "detail" in data:
                    error_message += f"\nDetalle: {data['detail']}"
                if "errors" in data:
                    error_message += f"\nErrors: {data['errors']}"

        raise DopplerRelayError(
            error_message,
            status=resp.status_code,
            payload=error_info,
        )

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Realiza una petición HTTP a la API de Doppler Relay."""
        url = self._url(path)
        max_retries = self.max_attempts  # was: max_retries = 3 (design.md §10/D9)
        retry_count = 0
        last_error = None

        while retry_count < max_retries:
            try:
                if _debug_api_logs_enabled():
                    logger.debug(
                        "Doppler request attempt %s/%s: %s %s payload=%s",
                        retry_count + 1,
                        max_retries,
                        method,
                        url,
                        kwargs.get("json"),
                    )

                # Asegurarnos de no duplicar el timeout
                if 'timeout' not in kwargs:
                    kwargs['timeout'] = self.timeout

                # Hacer la petición
                resp = self.session.request(method, url, **kwargs)

                if _debug_api_logs_enabled():
                    logger.debug(
                        "Doppler response: status=%s headers=%s body=%s",
                        resp.status_code,
                        dict(resp.headers),
                        resp.text[:1000],
                    )

                if resp.status_code >= 400:
                    self._raise_for_api(resp)
                return resp

            except (requests.RequestException, DopplerRelayError) as e:
                retry_count += 1
                last_error = e
                logger.warning(
                    "Doppler request error on attempt %s/%s: %s: %s",
                    retry_count,
                    max_retries,
                    type(e).__name__,
                    str(e),
                )

                if retry_count < max_retries:
                    # Calcular tiempo de espera exponencial
                    wait_time = min(0.8 * (2 ** retry_count), 8)
                    import time
                    time.sleep(wait_time)
                else:
                    logger.error("Se agotaron los reintentos contra Doppler")
                    break

        # Si llegamos aquí, todos los intentos fallaron
        if isinstance(last_error, DopplerRelayError):
            raise last_error
        else:
            # KNOWN PRE-EXISTING V1 DEFECT (discovered during
            # bulk-v2-real-send-canary design, design.md §10.1 / §17 risk 2):
            # for a requests.Timeout/ConnectionError, `last_error.response`
            # EXISTS and is None, so `getattr(last_error, 'response', {})`
            # returns None (the {} default only applies when the attribute
            # is MISSING, not when it is None) and `None.status_code` raises
            # AttributeError instead of the DopplerRelayError this branch
            # intends to construct. NOT FIXED HERE — V1 must stay
            # byte-identical. relay/services/bulk_v2_send.py's outcome
            # classifier absorbs this by treating any post-dispatch
            # exception, explicitly including AttributeError, as
            # send_status="ambiguous" (error_code="dispatch_exception").
            raise DopplerRelayError(
                f"Error después de {max_retries} intentos: {str(last_error)}",
                status=getattr(last_error, 'response', {}).status_code,
                payload={
                    "original_error": str(last_error),
                    "error_type": type(last_error).__name__
                }
            )

    def get_template_fields(self, account_id: int, template_id: str) -> Dict[str, Any]:
        """
        Obtiene los detalles de una plantilla, incluyendo sus variables Mustache.
        Doppler Relay utiliza el sistema de plantillas Mustache que permite variables
        en el formato {{variable}}. Las variables pueden ser simples ({{name}}) o
        pueden incluir puntos para acceder a propiedades anidadas ({{user.name}}).

        Args:
            account_id: ID de la cuenta
            template_id: ID de la plantilla

        Returns:
            Dict con los detalles de la plantilla, incluyendo las variables Mustache requeridas
        """
        # Validar los parámetros
        if not account_id:
            raise ValueError("account_id es requerido")
        if not template_id:
            raise ValueError("template_id es requerido")

        logger.debug("Obteniendo campos Mustache de plantilla %s para cuenta %s", template_id, account_id)

        try:
            response = self._request(
                "GET",
                f"/accounts/{account_id}/templates/{template_id}"
            )
            template_data = response.json()
        except Exception:
            logger.exception("Error al obtener la plantilla %s", template_id)
            raise

        # Extraer las variables de la plantilla
        content = template_data.get(
            "htmlContent", "") or template_data.get("textContent", "")
        if not content:
            logger.warning("La plantilla %s no tiene contenido HTML ni texto", template_id)
            return {
                "id": template_data.get("id"),
                "name": template_data.get("name"),
                "subject": template_data.get("subject"),
                "variables": []
            }

        # Buscar variables Mustache en el formato {{variable}} o {{object.property}}
        import re
        variables = []
        matches = re.finditer(r'\{\{([^}]+)\}\}', content)

        for match in matches:
            var_name = match.group(1).strip()
            # Validar que sea una variable Mustache válida (permite puntos para acceso a propiedades)
            if re.match(r'^[a-zA-Z_][a-zA-Z0-9_.]*[a-zA-Z0-9_]$', var_name):
                if var_name not in variables:
                    variables.append(var_name)
            else:
                logger.debug("Variable Mustache invalida encontrada: %s", var_name)

        logger.debug("Variables Mustache encontradas para %s: %s", template_id, sorted(variables))

        result = {
            "id": template_data.get("id"),
            "name": template_data.get("name"),
            "subject": template_data.get("subject"),
            "variables": sorted(variables)
        }

        logger.debug("Informacion de plantilla: %s", result)

        return result

    # --- Mensajes ---
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def send_message(self, account_id: int, from_email: str, subject: str, html: str | None = None, text: str | None = None,
                     *, from_name: str | None = None, to: Iterable[tuple[str, str | None]] = (),
                     cc: Iterable[tuple[str, str | None]] = (), bcc: Iterable[tuple[str, str | None]] = (),
                     reply_to: str | None = None, headers: dict[str, str] | None = None,
                     tags: list[str] | None = None, metadata: dict[str, Any] | None = None,
                     attachments: list[tuple[str, bytes, str]] | None = None) -> dict[str, Any]:
        if not html and not text:
            raise ValueError("Debes proveer 'html' o 'text'.")

        to = list(to)
        cc = list(cc)
        bcc = list(bcc)
        logger.info("Enviando mensaje simple a %s destinatarios", len(to) + len(cc) + len(bcc))

        # Validar la API key
        if not self.session.headers.get('Authorization'):
            raise ValueError("No se encontró el header de autorización")

        recipients = []
        for email, name in to:
            recipients.append(
                {"type": "to", "email": email, "name": name or ""})
        for email, name in cc:
            recipients.append(
                {"type": "cc", "email": email, "name": name or ""})
        for email, name in bcc:
            recipients.append(
                {"type": "bcc", "email": email, "name": name or ""})
        payload: dict[str, Any] = {
            "from_email": from_email,
            "from_name": from_name,
            "subject": subject,
            "recipients": recipients,
        }
        if html:
            payload["html"] = html
        if text:
            payload["text"] = text
        if reply_to:
            payload["reply_to"] = reply_to
        if headers:
            payload["headers"] = headers
        if tags:
            payload["tags"] = tags
        if metadata:
            payload["metadata"] = metadata
        if attachments:
            payload["attachments"] = [{
                "name": fname,
                "content": base64.b64encode(content).decode("ascii"),
                "type": mime or "application/octet-stream"
            } for (fname, content, mime) in attachments]
        resp = self._request("POST", f"/accounts/{account_id}/messages",
                             json=payload, headers={"Content-Type": "application/json"})
        data = resp.json()
        data["_location"] = resp.headers.get("Location")
        return data

    def get_message(self, account_id: int, message_id: str) -> dict[str, Any]:
        return self._request("GET", f"/accounts/{account_id}/messages/{message_id}").json()

    def list_messages(self, account_id: int, page_url: str | None = None) -> dict[str, Any]:
        path = page_url or f"/accounts/{account_id}/messages"
        return self._request("GET", path).json()

    # --- Plantillas CRUD ---
    def list_templates(self, account_id: int) -> dict[str, Any]:
        return self._request("GET", f"/accounts/{account_id}/templates").json()

    def create_template(self, account_id: int, name: str, subject: str, from_email: str, body_html: str, from_name: str | None = None) -> dict[str, Any]:
        payload = {"name": name, "subject": subject,
                   "from_email": from_email, "from_name": from_name, "body": body_html}
        resp = self._request("POST", f"/accounts/{account_id}/templates",
                             json=payload, headers={"Content-Type": "application/json"})
        data = resp.json()
        data["_location"] = resp.headers.get("Location")
        return data

    def get_template(self, account_id: int, template_id: str) -> dict[str, Any]:
        return self._request("GET", f"/accounts/{account_id}/templates/{template_id}").json()

    def update_template(self, account_id: int, template_id: str, *, name: str | None = None,
                        subject: str | None = None, from_email: str | None = None,
                        body_html: str | None = None, from_name: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if subject is not None:
            payload["subject"] = subject
        if from_email is not None:
            payload["from_email"] = from_email
        if from_name is not None:
            payload["from_name"] = from_name
        if body_html is not None:
            payload["body"] = body_html
        resp = self._request("PUT", f"/accounts/{account_id}/templates/{template_id}",
                             json=payload, headers={"Content-Type": "application/json"})
        return resp.json()

    def delete_template(self, account_id: int, template_id: str) -> None:
        self._request(
            "DELETE", f"/accounts/{account_id}/templates/{template_id}")
        return None

    # Helper to obtain HTML body from the template metadata, body link, or nested payloads.
    def get_template_html(self, account_id: int, template_id: str) -> str:
        def _extract_html(payload: Any) -> str:
            if isinstance(payload, str):
                return payload if payload.strip() else ""
            if isinstance(payload, list):
                for item in payload:
                    html = _extract_html(item)
                    if html:
                        return html
                return ""
            if not isinstance(payload, dict):
                return ""

            for key in ("html", "htmlContent", "body", "content", "textContent"):
                val = payload.get(key)
                if isinstance(val, str) and val.strip():
                    return val
                if isinstance(val, (dict, list)):
                    nested = _extract_html(val)
                    if nested:
                        return nested
            for k in ("template", "data", "attributes", "message", "resource"):
                sub = payload.get(k)
                nested = _extract_html(sub)
                if nested:
                    return nested
            return ""

        def _body_links(payload: Any) -> list[str]:
            if not isinstance(payload, dict):
                return []
            links = payload.get("_links") or payload.get("links") or []
            if isinstance(links, dict):
                links = list(links.values())
            if not isinstance(links, list):
                return []
            hrefs = []
            for link in links:
                if not isinstance(link, dict):
                    continue
                rel = str(link.get("rel") or link.get("name") or "").strip()
                href = str(link.get("href") or link.get("url") or "").strip()
                if href and (rel == "/docs/rels/get-template-body" or rel.endswith("get-template-body") or href.rstrip("/").endswith("/body")):
                    hrefs.append(href)
            return hrefs

        try:
            data = self.get_template(account_id, template_id)
            html = _extract_html(data)
            if html:
                return html

            for body_href in _body_links(data):
                resp = self._request("GET", body_href)
                content_type = (resp.headers.get("Content-Type") or "").lower()
                body_text = resp.text or ""
                if "text/html" in content_type or "text/plain" in content_type:
                    return body_text
                if body_text.lstrip().startswith("<"):
                    return body_text
                try:
                    body_payload = resp.json()
                except Exception:
                    body_payload = None
                html = _extract_html(body_payload)
                if html:
                    return html
                if body_text.strip():
                    return body_text
        except Exception:
            return ""
        return ""

    def send_template_message(self, account_id: int, template_id: str, recipients_model: dict[str, Any]) -> dict[str, Any]:
        """
        Envía un mensaje usando una plantilla de Doppler Relay con variables Mustache.

        Args:
            account_id: ID de la cuenta
            template_id: ID de la plantilla
            recipients_model: Diccionario con la información del mensaje y destinatarios

        La plantilla puede contener variables en formato Mustache: {{variable}}
        Las variables se envían sin las llaves en el payload.

        Ejemplo de plantilla:
            "Hola {{nombre}}, tu saldo es {{monto}}"

        Ejemplo de variables en el payload:
            { "data": { "nombre": "Juan", "monto": "1000" } }
        """
        logger.info(
            "Enviando template %s a %s destinatarios",
            template_id,
            len(recipients_model.get("recipients") or recipients_model.get("model", {}).get("recipients") or []),
        )

        # Validación del modelo de datos
        if not isinstance(recipients_model, dict):
            raise ValueError("recipients_model debe ser un diccionario")

        if _debug_api_logs_enabled():
            logger.debug("Modelo de datos recibido: %s", recipients_model)

        # Extraer y validar los destinatarios
        if "recipients" not in recipients_model:
            if "model" in recipients_model and "recipients" in recipients_model["model"]:
                recipients_model = recipients_model["model"]
            else:
                raise ValueError(
                    "El modelo debe contener una lista de destinatarios")

        recipients = recipients_model["recipients"]
        if not recipients:
            raise ValueError("La lista de destinatarios está vacía")

        # Validación y configuración del remitente
        model = recipients_model.get("model", {})
        from_email = str(recipients_model.get(
            "from_email", model.get("from_email", ""))).strip()
        from_name = str(recipients_model.get(
            "from_name", model.get("from_name", ""))).strip()
        subject = str(recipients_model.get(
            "subject", model.get("subject", ""))).strip()

        if not from_email:
            raise ValueError("from_email es requerido")

        # Validación de la plantilla
        if not template_id:
            raise ValueError("template_id es requerido")

        # Preparar el modelo para el envío
        model = {
            "from_email": from_email,
            "from_name": from_name,
            "reply_to": {
                "email": from_email,
                "name": from_name
            },
            "subject": subject,
            "templateId": str(template_id),
            "model": {},  # Variables globales del template
            "recipients": []
        }

        # Procesar los destinatarios y sus variables
        for recipient in recipients:
            email = str(recipient.get("email", "")).strip()
            if not email:
                continue

            # Las variables vienen en el campo 'variables' del recipiente
            recipient_variables = recipient.get("variables", {})

            # Procesar las variables para cada destinatario
            variables = {
                key: value
                for key, value in recipient_variables.items()
                if isinstance(key, str) and value not in (None, "")
            }

            recipient_payload = {
                "email": email,
                # Nombre del destinatario
                "name": recipient.get("name", ""),
                "type": "to",  # Tipo de destinatario
                "model": variables,
            }
            if variables:
                # Agregar las variables al modelo global para compatibilidad
                model.setdefault("model", {}).update(variables)
            model["recipients"].append(recipient_payload)

        if "attachments" in recipients_model:
            attachments = []
            for attachment in recipients_model["attachments"]:
                if not isinstance(attachment, dict):
                    continue
                if "content" not in attachment or "filename" not in attachment:
                    continue
                try:
                    # Si el contenido ya está en base64, verificar que sea válido
                    if isinstance(attachment["content"], str):
                        try:
                            base64.b64decode(attachment["content"])
                            content = attachment["content"]
                        except:
                            # Si no es base64 válido, codificarlo
                            content = base64.b64encode(
                                attachment["content"].encode()).decode()
                    else:
                        # Si es bytes, codificar a base64
                        content = base64.b64encode(
                            attachment["content"]).decode()

                    attachments.append({
                        "content": content,
                        "filename": str(attachment["filename"]).strip()
                    })
                except Exception as e:
                    logger.warning("Error procesando adjunto: %s", str(e))
                    continue

            if attachments:
                model["attachments"] = attachments

        # Validar que tengamos destinatarios para procesar
        if not model["recipients"]:
            raise ValueError(
                "No hay destinatarios con variables para procesar")

        logger.info(
            "Payload Doppler preparado para template %s con %s destinatarios",
            template_id,
            len(model["recipients"]),
        )
        if _debug_api_logs_enabled():
            logger.debug("Payload final Doppler: %s", model)

        try:
            response = self._request(
                "POST",
                f"/accounts/{str(account_id)}/templates/{str(template_id)}/message",
                json=model,
                headers={"Content-Type": "application/json"}
            )

            result = response.json()
            result["_location"] = response.headers.get("Location")

            logger.info("Envio Doppler aceptado para template %s", template_id)
            if _debug_api_logs_enabled():
                logger.debug("Respuesta Doppler: %s", result)

            # Transformar la respuesta al formato requerido
            resultados = []
            message_id = result.get("message_id") or (result.get(
                "_location", "").split("/")[-1] if result.get("_location") else "")

            for recipient in model["recipients"]:
                resultados.append({
                    "email": recipient["email"],
                    "status": "ok",
                    "message_id": message_id,
                    # Incluimos las variables usadas
                    "variables": model["model"]
                })

            return {
                "ok": True,
                "resultados": resultados,
                "total": len(resultados)
            }

        except Exception as e:
            logger.exception("Error durante el envio con template %s", template_id)
            if hasattr(e, 'payload'):
                logger.debug("Detalles del error Doppler: %s", e.payload)
            raise

    # --- Entregas & Eventos ---

    def list_deliveries(self, account_id: int, *, from_iso: str | None = None, to_iso: str | None = None, page_url: str | None = None) -> dict[str, Any]:
        if page_url:
            url = page_url
        else:
            path = f"/accounts/{account_id}/deliveries"
            params = {}
            if from_iso:
                _ = isoparse(from_iso)
                params["from"] = from_iso
            if to_iso:
                _ = isoparse(to_iso)
                params["to"] = to_iso
            url = path + ("" if not params else "?" +
                          "&".join(f"{k}={v}" for k, v in params.items()))
        return self._request("GET", url).json()

    def get_delivery(self, account_id: int, delivery_id: str) -> dict[str, Any]:
        return self._request("GET", f"/accounts/{account_id}/deliveries/{delivery_id}").json()

    def deliveries_aggregation(self, account_id: int, *, from_iso: str | None = None, to_iso: str | None = None) -> dict[str, Any]:
        params = {}
        if from_iso:
            _ = isoparse(from_iso)
            params["from"] = from_iso
        if to_iso:
            _ = isoparse(to_iso)
            params["to"] = to_iso
        return self._request("GET", f"/accounts/{account_id}/deliveries/aggregation", params=params).json()

    def list_events(self, account_id: int, *, from_iso: str | None = None, to_iso: str | None = None, page_url: str | None = None) -> dict[str, Any]:
        if page_url:
            url = page_url
        else:
            path = f"/accounts/{account_id}/events"
            params = {}
            if from_iso:
                _ = isoparse(from_iso)
                params["from"] = from_iso
            if to_iso:
                _ = isoparse(to_iso)
                params["to"] = to_iso
            url = path + ("" if not params else "?" +
                          "&".join(f"{k}={v}" for k, v in params.items()))
        return self._request("GET", url).json()

    @staticmethod
    def next_link(data: dict[str, Any]) -> str | None:
        links = data.get("_links") or data.get("links") or []
        for item in links:
            if (item.get("rel") or "").endswith("next") or item.get("rel") == "next":
                return item.get("href")
        return None
