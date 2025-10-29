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
                content = f.read().decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(content))  # CSV esperado delimitado por comas
                headers = [h.strip().lower() for h in (reader.fieldnames or [])]
                if "email" not in headers:
                    raise ValueError("El archivo CSV debe tener una columna 'email'")
                for row in reader:
                    clean = {k.strip().lower(): (v.strip() if v else v) for k, v in row.items()}
                    email_value = clean.get("email")
                    if not email_value:
                        continue
                    variables = {k: v for k, v in clean.items() if k != "email" and v}
                    missing = required_vars - set(variables.keys())
                    if missing:
                        raise ValueError(
                            f"Faltan variables requeridas para {email_value}: {', '.join(missing)}"
                        )
                    recipients.append({
                        "email": email_value,
                        "name": clean.get("nombres", ""),
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

