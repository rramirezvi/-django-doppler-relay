from __future__ import annotations
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpRequest
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
import json
import csv
import io
from .models import EmailMessage
from .services.doppler_relay import DopplerRelayClient, DopplerRelayError


def process_csv_for_template(csv_content: str, email_column: str = "email") -> list:
    """Procesa un archivo CSV y extrae los destinatarios y sus variables."""
    recipients = []
    try:
        csvfile = io.StringIO(csv_content)
        reader = csv.DictReader(csvfile)

        if email_column not in reader.fieldnames:
            raise ValueError(
                f"La columna \'{email_column}\' no existe en el CSV")

        for row in reader:
            email = row.pop(email_column)
            if not email:
                continue
            variables = {k: str(v).strip() for k, v in row.items() if v}
            recipients.append({
                "email": email.strip(),
                "variables": variables
            })

    except Exception as e:
        raise ValueError(f"Error al procesar el CSV: {str(e)}")

    return recipients


def validate_email(email: str) -> bool:
    """Valida que el email tenga un formato válido."""
    import re
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))


def process_bulk_template_send(template_id, recipients, subject=None, adj_list=None, from_email=None, from_name=None):
    """
    Procesa el envío masivo de correos usando una plantilla.

    Args:
        template_id: ID de la plantilla a utilizar
        recipients: Lista de destinatarios con sus variables
        subject: Asunto del correo (opcional)
        adj_list: Lista de adjuntos (opcional)
        from_email: Email del remitente (opcional)
        from_name: Nombre del remitente (opcional)

    Returns:
        Lista con los resultados del envío
    """
    client = DopplerRelayClient()
    resultados = []
    ACCOUNT_ID = str(settings.DOPPLER_RELAY["ACCOUNT_ID"])

    # Configuración del remitente
    FROM_EMAIL = None
    if from_email:
        FROM_EMAIL = str(from_email).strip()
    elif "DEFAULT_FROM_EMAIL" in settings.DOPPLER_RELAY:
        FROM_EMAIL = str(settings.DOPPLER_RELAY["DEFAULT_FROM_EMAIL"]).strip()
    else:
        raise ValueError("No se ha configurado un email del remitente")

    FROM_NAME = None
    if from_name:
        FROM_NAME = str(from_name).strip()
    elif "DEFAULT_FROM_NAME" in settings.DOPPLER_RELAY:
        FROM_NAME = str(settings.DOPPLER_RELAY["DEFAULT_FROM_NAME"]).strip()
    else:
        FROM_NAME = ""  # El nombre del remitente es opcional

    SUBJECT = str(subject or "").strip()

    # Validaciones básicas
    if not FROM_EMAIL or not validate_email(FROM_EMAIL):
        raise ValueError(f"Email del remitente inválido: {FROM_EMAIL}")

    if not template_id:
        raise ValueError("El ID de la plantilla es requerido")

    print(f"Usando remitente: {FROM_EMAIL} ({FROM_NAME})")

    # Validar y procesar adjuntos si existen
    attachments = None
    if adj_list:
        try:
            import base64
            attachments = []
            for attachment in adj_list:
                # Si el contenido ya está en base64, lo usamos así
                if isinstance(attachment["content"], str):
                    content = attachment["content"]
                    try:
                        # Verificar si es base64 válido
                        base64.b64decode(content)
                    except:
                        # Si no es base64, lo convertimos
                        content = base64.b64encode(
                            attachment["content"].encode()).decode()
                else:
                    # Si es bytes, lo convertimos a base64
                    content = base64.b64encode(attachment["content"]).decode()

                attachments.append({
                    "content": content,
                    "name": str(attachment["filename"]).strip()
                })
        except Exception as e:
            raise ValueError(f"Error procesando adjuntos: {str(e)}")

    # Procesamos cada destinatario individualmente
    for recipient in recipients:
        try:
            # Validar email del destinatario
            email = str(recipient["email"]).strip()
            if not email or not validate_email(email):
                raise ValueError(f"Email inválido: {email}")

            # Convertir todas las variables a string y limpiar
            variables = {
                str(k).strip(): str(v).strip()
                for k, v in recipient.get("variables", {}).items()
                if v is not None  # Ignorar valores None
            }

            # Modelo para un solo destinatario con estructura requerida por la API
            single_model = {
                "from_email": FROM_EMAIL,
                "from_name": FROM_NAME,
                "subject": SUBJECT,
                "template_id": str(template_id),
                "recipients": [{
                    "email": email,
                    "name": recipient.get("name", ""),
                    "variables": variables,
                    "type": "to"
                }],
                "attachments": attachments or []
            }

            if attachments:
                # Debug pre-envío
                single_model["model"]["attachments"] = attachments
            print(f"\nIntentando enviar a {email}")
            print(f"Variables: {json.dumps(variables, indent=2)}")
            print(f"Modelo: {json.dumps(single_model, indent=2)}")

            # Envío individual
            sent = client.send_template_message(
                account_id=ACCOUNT_ID,
                template_id=str(template_id),
                recipients_model=single_model
            )

            print(f"Respuesta del servidor: {json.dumps(sent, indent=2)}")

            # Guardar en modelos locales
            email_obj = EmailMessage.objects.create(
                relay_message_id=str(sent.get("id", "")),
                subject=SUBJECT,
                from_email=FROM_EMAIL,
                to_emails=email,
                html=None,
                text=None,
            )

            resultados.append({
                "email": email,
                "status": "ok",
                "message_id": email_obj.relay_message_id,
                "variables": variables
            })

        except DopplerRelayError as e:
            error_info = {
                "email": recipient["email"],
                "status": "error",
                "error": str(e),
                "details": e.payload if hasattr(e, "payload") else None,
                "variables": recipient.get("variables", {})
            }
            resultados.append(error_info)
            print(f'Error Doppler para {recipient["email"]}: {str(e)}')
            if hasattr(e, "payload"):
                print(f"Payload de error: {e.payload}")

        except Exception as e:
            error_info = {
                "email": recipient["email"],
                "status": "error",
                "error": str(e),
                "variables": recipient.get("variables", {})
            }
            resultados.append(error_info)
            print(f'Error general para {recipient["email"]}: {str(e)}')

    return resultados


@require_POST
@csrf_exempt
def send_bulk_email(request: HttpRequest):
    """
    Endpoint para envío masivo de correos.
    Acepta tanto JSON como CSV para los destinatarios.
    """
    if request.FILES.get("csv_file"):
        # Procesar CSV
        try:
            csv_content = request.FILES["csv_file"].read().decode("utf-8")
            email_column = request.POST.get("email_column", "email")
            template_id = request.POST.get("template_id")

            if not template_id:
                return JsonResponse({
                    "ok": False,
                    "error": "Falta el ID de la plantilla"
                }, status=400)

            recipients = process_csv_for_template(csv_content, email_column)
            if not recipients:
                return JsonResponse({
                    "ok": False,
                    "error": "No hay destinatarios válidos en el CSV"
                }, status=400)

            resultados = process_bulk_template_send(
                template_id=template_id,
                recipients=recipients,
                subject=request.POST.get("subject"),
                from_email=request.POST.get("from_email"),
                from_name=request.POST.get("from_name")
            )

            return JsonResponse({
                "ok": True,
                "resultados": resultados,
                "total_enviados": len([r for r in resultados if r.get("status") == "ok"])
            })

        except ValueError as e:
            return JsonResponse({
                "ok": False,
                "error": str(e)
            }, status=400)
        except Exception as e:
            return JsonResponse({
                "ok": False,
                "error": str(e)
            }, status=500)

    else:
        # Procesar JSON
        try:
            data = json.loads(request.body)

            # Validar campos requeridos
            template_id = data.get("template_id")
            if not template_id:
                return JsonResponse({
                    "ok": False,
                    "error": "Falta el ID de la plantilla"
                }, status=400)

            # Obtener y normalizar destinatarios
            recipients = []
            raw_recipients = data.get("to") or data.get("recipients") or []

            if not raw_recipients:
                return JsonResponse({
                    "ok": False,
                    "error": "No hay destinatarios"
                }, status=400)

            # Normalizar formato de destinatarios
            for r in raw_recipients:
                if isinstance(r, str):
                    # Si es solo un email
                    recipients.append({"email": r, "variables": {}})
                elif isinstance(r, dict):
                    # Si es un diccionario, asegurarnos que tenga el formato correcto
                    if "email" not in r:
                        continue

                    recipient = {
                        "email": r["email"],
                        "variables": r.get("variables") or r.get("substitution_data") or {}
                    }
                    recipients.append(recipient)

            if not recipients:
                return JsonResponse({
                    "ok": False,
                    "error": "No hay destinatarios válidos después de procesar"
                }, status=400)

            # Procesar adjuntos si existen
            attachments = None
            if "attachments" in data:
                attachments = []
                for att in data["attachments"]:
                    if isinstance(att, dict) and "content" in att and "filename" in att:
                        attachments.append({
                            "content": att["content"],
                            "filename": att["filename"]
                        })

            # Enviar correos
            resultados = process_bulk_template_send(
                template_id=template_id,
                recipients=recipients,
                subject=data.get("subject"),
                from_email=data.get("from_email"),
                from_name=data.get("from_name"),
                adj_list=attachments
            )

            return JsonResponse({
                "ok": True,
                "resultados": resultados,
                "total_enviados": len([r for r in resultados if r.get("status") == "ok"]),
                "total_errores": len([r for r in resultados if r.get("status") == "error"])
            })

        except ValueError as e:
            return JsonResponse({
                "ok": False,
                "error": str(e)
            }, status=400)
        except json.JSONDecodeError:
            return JsonResponse({
                "ok": False,
                "error": "El cuerpo de la petición no es un JSON válido"
            }, status=400)
        except Exception as e:
            return JsonResponse({
                "ok": False,
                "error": str(e)
            }, status=500)
