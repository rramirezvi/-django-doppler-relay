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
    def __init__(self, *, api_key: str | None = None, base_url: str | None = None, auth_scheme: str | None = None, timeout: int | None = None):
        cfg = settings.DOPPLER_RELAY
        self.base_url = (base_url or cfg.get(
            "BASE_URL", DEFAULT_BASE_URL)).rstrip("/") + "/"
        self.timeout = timeout or cfg.get("TIMEOUT", 30)
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
        print(f"\n=== ENVIANDO BULK EMAIL ===")
        print(f"Template ID: {template_id}")
        print(f"Recipients Model: {json.dumps(recipients_model, indent=2)}")

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
        print("DopplerRelayClient configuración:", debug_info)

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def _raise_for_api(self, resp: requests.Response):
        # Primero, loguear la respuesta completa para depuración
        print(f"\nRequest URL: {resp.request.url}")
        print(f"Request Method: {resp.request.method}")
        print(f"Request Headers: {dict(resp.request.headers)}")
        try:
            print(f"Request Body: {resp.request.body.decode()}")
        except:
            print(f"Request Body: {resp.request.body}")
        print(f"\nResponse Status: {resp.status_code}")
        print(f"Response Headers: {dict(resp.headers)}")
        try:
            print(f"Response Body: {resp.text}")
        except:
            print("No se pudo leer el cuerpo de la respuesta")

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
        max_retries = 3
        retry_count = 0
        last_error = None

        while retry_count < max_retries:
            try:
                # Log de la petición
                print(
                    f"\n=== REQUEST (intento {retry_count + 1}/{max_retries}) ===")
                print(f"URL: {url}")
                print(f"Method: {method}")
                print(f"Headers: {dict(self.session.headers)}")
                if 'json' in kwargs:
                    print(
                        f"JSON Payload: {json.dumps(kwargs['json'], indent=2)}")

                # Asegurarnos de no duplicar el timeout
                if 'timeout' not in kwargs:
                    kwargs['timeout'] = self.timeout

                # Hacer la petición
                resp = self.session.request(method, url, **kwargs)

                # Log de la respuesta
                print(f"\n=== RESPONSE ===")
                print(f"Status: {resp.status_code}")
                print(f"Headers: {dict(resp.headers)}")
                try:
                    # Primeros 1000 caracteres
                    print(f"Body: {resp.text[:1000]}...")
                except:
                    print("No se pudo leer el cuerpo de la respuesta")

                if resp.status_code >= 400:
                    self._raise_for_api(resp)
                return resp

            except (requests.RequestException, DopplerRelayError) as e:
                retry_count += 1
                last_error = e
                print(f"\n=== ERROR (intento {retry_count}/{max_retries}) ===")
                print(f"Type: {type(e).__name__}")
                print(f"Message: {str(e)}")

                if hasattr(e, 'response') and hasattr(e.response, 'text'):
                    print(f"Response Text: {e.response.text}")

                if retry_count < max_retries:
                    # Calcular tiempo de espera exponencial
                    wait_time = min(0.8 * (2 ** retry_count), 8)
                    print(
                        f"Esperando {wait_time:.2f} segundos antes de reintentar...")
                    import time
                    time.sleep(wait_time)
                else:
                    print("Se agotaron los reintentos")
                    break

        # Si llegamos aquí, todos los intentos fallaron
        if isinstance(last_error, DopplerRelayError):
            raise last_error
        else:
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

        print(f"\n=== OBTENIENDO CAMPOS DE LA PLANTILLA MUSTACHE ===")
        print(f"Account ID: {account_id}")
        print(f"Template ID: {template_id}")

        try:
            response = self._request(
                "GET",
                f"/accounts/{account_id}/templates/{template_id}"
            )
            template_data = response.json()
        except Exception as e:
            print(f"Error al obtener la plantilla: {str(e)}")
            raise

        # Extraer las variables de la plantilla
        content = template_data.get(
            "htmlContent", "") or template_data.get("textContent", "")
        if not content:
            print("Advertencia: La plantilla no tiene contenido HTML ni texto")
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
                print(
                    f"Advertencia: Variable Mustache inválida encontrada: {var_name}")

        print(f"\nVariables Mustache encontradas ({len(variables)}):")
        for var in sorted(variables):
            print(f"- {var}")

        result = {
            "id": template_data.get("id"),
            "name": template_data.get("name"),
            "subject": template_data.get("subject"),
            "variables": sorted(variables)
        }

        print(f"\nInformación de la plantilla:")
        print(f"- ID: {result['id']}")
        print(f"- Nombre: {result['name']}")
        print(f"- Asunto: {result['subject']}")

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

        print(f"\n=== INICIANDO ENVÍO DE MENSAJE ===")
        print(f"Account ID: {account_id}")
        print(f"From: {from_email}")
        print(f"To: {list(to)}")
        print(f"Subject: {subject}")
        print(f"HTML: {'Sí' if html else 'No'}")
        print(f"Text: {'Sí' if text else 'No'}")

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
        print(f"\n=== ENVIANDO TEMPLATE {template_id} ===")
        print("Formato de variables en plantilla: {{variable}}")

        # Validación del modelo de datos
        if not isinstance(recipients_model, dict):
            raise ValueError("recipients_model debe ser un diccionario")

        # Mostrar el modelo recibido para debug
        print(f"\nModelo de datos recibido:")
        print(json.dumps(recipients_model, indent=2, ensure_ascii=False))

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

        print("\n=== INICIANDO PROCESAMIENTO DE VARIABLES ===")

        # Procesar los destinatarios y sus variables
        for recipient in recipients:
            email = str(recipient.get("email", "")).strip()
            if not email:
                continue

            # Obtener todas las variables disponibles del destinatario
            print(f"\n📧 Procesando destinatario: {email}")

            # Las variables vienen en el campo 'variables' del recipiente
            recipient_variables = recipient.get("variables", {})
            print("Variables disponibles:", recipient_variables)

            # Procesar las variables para cada destinatario
            variables = {
                key: value
                for key, value in recipient_variables.items()
                if isinstance(key, str) and value not in (None, "")
            }

            print("Variables procesadas:", variables)

            # Crear y agregar el recipient al modelo con sus variables
            if variables:
                print(f"\nConfigurando payload para {email}:")
                recipient_payload = {
                    "email": email,
                    # Nombre del destinatario
                    "name": recipient.get("name", ""),
                    "type": "to",  # Tipo de destinatario
                    "model": variables,
                }
                # Agregar las variables al modelo global para compatibilidad
                model.setdefault("model", {}).update(variables)
                print("Payload del destinatario:")
                print(json.dumps(recipient_payload, indent=2, ensure_ascii=False))
                model["recipients"].append(recipient_payload)
                print("Destinatario agregado al modelo")
            else:
                print(f"No se agrego {email} porque no tiene variables")

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
                    print(f"Error procesando adjunto: {str(e)}")
                    continue

            if attachments:
                model["attachments"] = attachments

        # Validar que tengamos destinatarios para procesar
        if not model["recipients"]:
            raise ValueError(
                "No hay destinatarios con variables para procesar")

        # Debug detallado del envío
        print("\n=== RESUMEN DE ENVÍO ===")
        print(f"Template ID: {template_id}")
        print(f"Total Destinatarios: {len(model['recipients'])}")
        print("\nEstructura del Payload:")
        print("1. Variables en la plantilla: {{variable}}")
        print(
            "2. Variables en el payload: { email: '...', name: '...', variables: { variable: 'valor' } }")
        print("\nPayload Final:")
        print(json.dumps(model, indent=2, ensure_ascii=False))

        try:
            # Hacer la llamada a la API con plantilla
            print("\n📤 Enviando solicitud a Doppler Relay (Envío con Plantilla)...")
            response = self._request(
                "POST",
                f"/accounts/{str(account_id)}/templates/{str(template_id)}/message",
                json=model,
                headers={"Content-Type": "application/json"}
            )

            result = response.json()
            result["_location"] = response.headers.get("Location")

            print("\n✅ Envío exitoso")
            print("Respuesta de la API:")
            print(json.dumps(result, indent=2))

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
            print("\n❌ Error durante el envío:")
            print(str(e))
            if hasattr(e, 'payload'):
                print("\nDetalles del error:")
                print(json.dumps(e.payload, indent=2))
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
