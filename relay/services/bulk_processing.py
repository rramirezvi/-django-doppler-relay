from __future__ import annotations

import csv
import io
import json
from typing import Any

from django.utils import timezone

from relay.models import BulkSend, UserEmailConfig
from relay.services.doppler_relay import DopplerRelayClient
from relay.views import process_bulk_template_send
from django.conf import settings


def _detect_reader(content: str) -> tuple[csv.DictReader, list[str], str | None]:
    def parse_with(delim: str):
        rdr = csv.DictReader(io.StringIO(content), delimiter=delim)
        hdrs = [h.strip().lower() for h in (rdr.fieldnames or [])]
        return rdr, hdrs

    for d in [";", ","]:
        r, h = parse_with(d)
        if any(v in h for v in ["email", "correo", "e-mail", "mail", "email_address", "correo_electronico", "\ufeffemail"]):
            return r, h, d
    # Fallback sniffer
    try:
        sample = content[:2048]
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        r, h = parse_with(dialect.delimiter)
        return r, h, dialect.delimiter
    except Exception:
        r, h = parse_with(",")
        return r, h, ","


def process_bulk_id(bulk_id: int) -> None:
    bulk = BulkSend.objects.get(id=bulk_id)

    # Obtener variables requeridas por la plantilla
    try:
        client = DopplerRelayClient()
        account_id = settings.DOPPLER_RELAY.get("ACCOUNT_ID", 0)
        template_info: dict[str, Any] = client.get_template_fields(account_id, bulk.template_id)
        required_vars = set(template_info.get("variables", []) or [])
    except Exception:
        required_vars = set()

    recipients: list[dict] = []
    try:
        with bulk.recipients_file.open("rb") as f:
            content = f.read().decode("utf-8-sig")
            reader, headers, _delim = _detect_reader(content)

            # Columna email
            email_col = None
            for v in ["email", "\ufeffemail", "correo", "e-mail", "mail", "email_address", "correo_electronico"]:
                if v in headers:
                    email_col = v
                    break
            if not email_col:
                raise ValueError(f"El archivo CSV debe tener una columna 'email'. Columnas: {headers}")

            # Mapeo de variables soportando dict o string JSON
            vm_raw = getattr(bulk, "variables", None)
            if isinstance(vm_raw, str):
                vm_raw = vm_raw.strip()
                variables_mapping = json.loads(vm_raw) if vm_raw else {}
            elif isinstance(vm_raw, dict):
                variables_mapping = vm_raw
            else:
                variables_mapping = {}
            if isinstance(variables_mapping, dict):
                variables_mapping = {k: v for k, v in variables_mapping.items() if isinstance(k, str) and isinstance(v, str) and not k.startswith("__")}

            for row in reader:
                clean = {str(k).strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
                email_value = clean.get(email_col) or clean.get("email") or clean.get("\ufeffemail")
                if not email_value:
                    continue
                if variables_mapping:
                    variables = {tpl_var: clean.get(csv_col.lower()) for tpl_var, csv_col in variables_mapping.items()}
                else:
                    variables = {k: v for k, v in clean.items() if k not in (email_col, "email", "\ufeffemail") and v}
                missing = required_vars - set(variables.keys()) if required_vars else set()
                if missing:
                    raise ValueError(f"Faltan variables requeridas para {email_value}: {', '.join(sorted(missing))}")
                recipients.append({"email": email_value, "name": clean.get("nombres", "") or clean.get("name", ""), "variables": variables})
    except Exception as e:
        bulk.result = json.dumps({"error": str(e), "recipients": recipients, "template_id": bulk.template_id})
        bulk.log = ((bulk.log or "") + f"\n[BG] Error leyendo archivo: {e}").strip()
        bulk.status = "error"
        bulk.save(update_fields=["result", "log", "status"])
        return

    # Adjuntos y remitente opcional (por remitente)
    adj_list = [att.to_doppler_format() for att in bulk.attachments.all()]
    subject = bulk.subject

    from_email = None
    from_name = None
    try:
        sender_id = None
        if isinstance(bulk.variables, dict):
            sender_id = bulk.variables.get("__sender_user_config_id")
        if sender_id:
            sender = UserEmailConfig.objects.filter(pk=sender_id, is_active=True).first()
            if sender:
                from_email = sender.from_email
                from_name = sender.from_name
    except Exception:
        pass

    try:
        response = process_bulk_template_send(
            template_id=bulk.template_id,
            recipients=recipients,
            subject=subject,
            adj_list=adj_list,
            from_email=from_email,
            from_name=from_name,
            user=None,
        )
        bulk.result = (response.content.decode("utf-8") if hasattr(response, "content") else json.dumps(response))
        bulk.status = "done"
        bulk.log = ((bulk.log or "") + f"\n[BG] Ejecutado a {timezone.now().isoformat()}").strip()
    except Exception as e:
        import traceback
        api_error = getattr(e, "payload", None)
        bulk.result = json.dumps({
            "error": str(e),
            "traceback": traceback.format_exc(),
            "recipients": recipients,
            "subject": subject,
            "attachments": adj_list,
            "template_id": bulk.template_id,
            "api_error": api_error,
        })
        bulk.status = "error"
        bulk.log = ((bulk.log or "") + f"\n[BG] Error en envío: {e}").strip()
    bulk.save(update_fields=["result", "status", "log", "processing_started_at"])

