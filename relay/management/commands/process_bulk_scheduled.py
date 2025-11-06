from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from relay.models import BulkSend
from relay.views import process_bulk_template_send
from relay.services.doppler_relay import DopplerRelayClient
from django.conf import settings

import csv
import io
import json


BATCH_SIZE = 50


class Command(BaseCommand):
    help = "Procesa envíos masivos programados (scheduled_at <= now) sin Celery"

    def handle(self, *args, **options):
        now = timezone.now()
        # Seleccionar candidatos programados (scheduled_at no nulo y en el pasado)
        qs = (
            BulkSend.objects.filter(status="pending", scheduled_at__isnull=False, scheduled_at__lte=now)
            .order_by("scheduled_at")
        )

        processed = 0
        for bulk in qs[:BATCH_SIZE]:
            # Intentar tomar lock/flag para evitar solapes
            acquired = self._acquire(bulk.id)
            if not acquired:
                continue
            try:
                self._process_bulk(bulk)
                processed += 1
            except Exception as exc:
                bulk.status = "error"
                bulk.log = (bulk.log or "") + f"\n[Scheduler] Error: {exc}"
                bulk.save(update_fields=["status", "log"])

        self.stdout.write(self.style.SUCCESS(f"Scheduler procesó {processed} envíos"))

    def _acquire(self, bulk_id: int) -> bool:
        try:
            with transaction.atomic():
                row = (
                    BulkSend.objects.select_for_update(skip_locked=True)
                    .filter(id=bulk_id, status="pending")
                    .first()
                )
                if not row:
                    return False
                row.processing_started_at = timezone.now()
                row.save(update_fields=["processing_started_at"])
                return True
        except Exception:
            return False

    def _process_bulk(self, bulk: BulkSend) -> None:
        # Cargar campos necesarios
        try:
            client = DopplerRelayClient()
            account_id = settings.DOPPLER_RELAY.get("ACCOUNT_ID", 0)
            template_info = client.get_template_fields(account_id, bulk.template_id)
            required_vars = set(template_info.get("variables", []))
        except Exception:
            required_vars = set()

        recipients: list[dict] = []
        try:
            with bulk.recipients_file.open("rb") as f:
                # Leer con tolerancia a BOM y saltos de línea
                content = f.read().decode("utf-8-sig")

                # Detectar delimitador o probar variantes comunes (; y ,)
                def parse_with(delim: str):
                    rdr = csv.DictReader(io.StringIO(content), delimiter=delim)
                    raw_headers = rdr.fieldnames or []
                    headers = [h.strip().lower() for h in raw_headers]
                    return rdr, headers, raw_headers

                reader = None
                headers = []
                raw_headers = []
                chosen_delim = None
                for delim in [";", ","]:
                    rdr, hdrs, raw = parse_with(delim)
                    # Variantes aceptadas para columna email
                    email_variants = [
                        "email", "correo", "e-mail", "mail", "email_address", "correo_electronico",
                        "\ufeffemail",  # por si arrastra BOM
                    ]
                    if any(v in hdrs for v in email_variants):
                        reader, headers, raw_headers, chosen_delim = rdr, hdrs, raw, delim
                        break

                # Si no se detectó, intentar con csv.Sniffer como último recurso
                if reader is None:
                    try:
                        sample = content[:2048]
                        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
                        rdr, hdrs, raw = parse_with(dialect.delimiter)
                        if any(v in hdrs for v in ["email", "\ufeffemail"]):
                            reader, headers, raw_headers, chosen_delim = rdr, hdrs, raw, dialect.delimiter
                    except Exception:
                        pass

                if reader is None:
                    raise ValueError(
                        f"No se pudo detectar el delimitador ni la columna de email. Columnas encontradas: {raw_headers}")

                # Determinar columna de email real
                email_column = None
                for v in ["email", "\ufeffemail", "correo", "e-mail", "mail", "email_address", "correo_electronico"]:
                    if v in headers:
                        email_column = v
                        break
                if not email_column:
                    raise ValueError(
                        f"El archivo CSV debe tener una columna 'email' (o variantes). Columnas: {headers}. Delimitador detectado: {chosen_delim!r}")

                # Map de variables personalizado (si existe)
                try:
                    variables_mapping = (bulk.variables if isinstance(bulk.variables, dict) else (json.loads(bulk.variables) if getattr(bulk, "variables", None) else {}))
                    # Filtrar claves reservadas y valores no-string
                    if isinstance(variables_mapping, dict):
                        variables_mapping = {
                            k: v
                            for k, v in variables_mapping.items()
                            if isinstance(k, str) and isinstance(v, str) and not k.startswith("__")
                        }
                except json.JSONDecodeError:
                    raise ValueError("El mapeo de variables no es un JSON válido")

                for row in reader:
                    clean = {str(k).strip().lower(): (v.strip() if isinstance(v, str) else v)
                             for k, v in row.items()}
                    email_value = (clean.get(email_column) or clean.get("email") or clean.get("\ufeffemail"))
                    if not email_value:
                        continue

                    # Construcción de variables: por mapeo o por todas las columnas excepto email
                    if variables_mapping:
                        variables = {tpl_var: clean.get(csv_col.lower()) for tpl_var, csv_col in variables_mapping.items()}
                    else:
                        variables = {k: v for k, v in clean.items() if k not in (email_column, "email", "\ufeffemail") and v}

                    missing = required_vars - set(variables.keys()) if required_vars else set()
                    if missing:
                        raise ValueError(
                            f"Faltan variables requeridas para {email_value}: {', '.join(sorted(missing))}")

                    recipients.append({
                        "email": email_value,
                        "name": clean.get("nombres", "") or clean.get("name", ""),
                        "variables": variables,
                    })
        except Exception as e:
            bulk.result = json.dumps({
                "error": str(e),
                "recipients": recipients,
                "template_id": bulk.template_id,
            })
            bulk.log = f"Error leyendo archivo: {e}"
            bulk.status = "error"
            bulk.save(update_fields=["result", "log", "status"])
            return

        # Adjuntos
        adj_list = [att.to_doppler_format() for att in bulk.attachments.all()]
        subject = bulk.subject

        try:
            response = process_bulk_template_send(
                template_id=bulk.template_id,
                recipients=recipients,
                subject=subject,
                adj_list=adj_list,
                user=None,
            )
            bulk.result = (
                response.content.decode("utf-8") if hasattr(response, "content") else json.dumps(response)
            )
            bulk.status = "done"
            bulk.log = (bulk.log or "") + f"\n[Scheduler] Ejecutado a {timezone.now().isoformat()}"
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
            bulk.log = (bulk.log or "") + f"\n[Scheduler] Error en envío: {e}"
        bulk.save(update_fields=["result", "status", "log", "processing_started_at"])
