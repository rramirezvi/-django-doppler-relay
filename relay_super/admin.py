from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext_lazy as _

from relay.models import BulkSend, UserEmailConfig
from relay_super.models import BulkSendUserConfigProxy
from relay.views import process_bulk_template_send
from relay.services.doppler_relay import DopplerRelayClient
from django.conf import settings
from relay.admin import BulkSendForm as BaseBulkSendForm


class SenderChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj: UserEmailConfig) -> str:  # type: ignore[override]
        return f"{obj.user.username} – {obj.from_email}"


class BulkSendSenderForm(BaseBulkSendForm):
    sender = SenderChoiceField(
        queryset=UserEmailConfig.objects.filter(is_active=True).select_related("user"),
        required=True,
        label=_("Remitente (UserEmailConfig)"),
        help_text=_("Seleccione el remitente (from_name / from_email) para este envío."),
    )

    class Meta(BaseBulkSendForm.Meta):
        model = BulkSend  # mantener el mismo modelo
        fields = BaseBulkSendForm.Meta.fields

    def save(self, commit=True):
        instance: BulkSend = super().save(commit=False)
        sender_obj: UserEmailConfig | None = self.cleaned_data.get("sender")
        # Persistir el remitente elegido dentro de variables con una clave reservada
        variables = instance.variables or {}
        if sender_obj:
            variables["__sender_user_config_id"] = sender_obj.pk
        instance.variables = variables
        if commit:
            instance.save()
            self.save_m2m()
        return instance


@admin.register(BulkSendUserConfigProxy)
class BulkSendUserConfigAdmin(admin.ModelAdmin):
    form = BulkSendSenderForm
    list_display = ("id", "template_id", "created_at", "status",)
    readonly_fields = ("result", "log", "status", "created_at", "processing_started_at")
    search_fields = ("template_id", "subject")
    list_filter = ("status",)
    actions = ["procesar_envio_masivo"]
    filter_horizontal = ("attachments",)

    # Permisos basados en el modelo proxy y usuarios staff
    def _has_any_relay_super_perm(self, request) -> bool:
        perms = (
            "relay_super.view_bulksenduserconfigproxy",
            "relay_super.add_bulksenduserconfigproxy",
            "relay_super.change_bulksenduserconfigproxy",
            "relay_super.delete_bulksenduserconfigproxy",
        )
        return any(request.user.has_perm(p) for p in perms)

    def has_module_permission(self, request):
        return (
            request.user.is_active
            and request.user.is_staff
            and (
                request.user.has_module_perms("relay_super")
                or self._has_any_relay_super_perm(request)
            )
        )

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_staff and request.user.has_perm(
            "relay_super.view_bulksenduserconfigproxy"
        )

    def has_add_permission(self, request):
        return request.user.is_active and request.user.is_staff and request.user.has_perm(
            "relay_super.add_bulksenduserconfigproxy"
        )

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_staff and request.user.has_perm(
            "relay_super.change_bulksenduserconfigproxy"
        )

    def has_delete_permission(self, request, obj=None):
        # Bloquear borrados siempre (ajustable si se desea con permiso delete)
        return False

    def procesar_envio_masivo(self, request, queryset):
        if not (request.user.is_active and request.user.is_staff and request.user.has_perm("relay_super.change_bulksenduserconfigproxy")):
            raise PermissionDenied("No tiene permiso para procesar envíos masivos.")
        # Basado en el flujo existente de BulkSendAdmin, pero forzando remitente explícito
        import csv
        import io
        import json
        from django.utils import timezone

        for bulk in queryset:
            if bulk.status != "pending":
                messages.warning(request, f"BulkSend {bulk.id} ya procesado.")
                continue

            # Remitente
            sender_config = None
            # Recuperar remitente persistido en variables (clave reservada)
            try:
                sender_id = None
                if isinstance(bulk.variables, dict):
                    sender_id = bulk.variables.get("__sender_user_config_id")
                if sender_id:
                    sender_config = UserEmailConfig.objects.filter(pk=sender_id, is_active=True).first()
            except Exception:
                sender_config = None

            if not sender_config:
                messages.error(request, f"BulkSend {bulk.id}: Seleccione un remitente válido (UserEmailConfig).")
                continue

            recipients = []
            try:
                # Obtener variables requeridas de la plantilla
                client = DopplerRelayClient()
                ACCOUNT_ID = settings.DOPPLER_RELAY.get("ACCOUNT_ID", 0)
                template_info = client.get_template_fields(ACCOUNT_ID, bulk.template_id)
                required_vars = set(template_info.get("variables", []))

                # Leer CSV delimitado por ;
                with bulk.recipients_file.open('rb') as f:
                    content = f.read().decode('utf-8-sig')
                    reader = csv.DictReader(io.StringIO(content), delimiter=';')
                    headers = [h.strip().lower() for h in reader.fieldnames or []]

                    if 'email' not in headers:
                        raise ValueError("El archivo CSV debe tener una columna 'email'")

                    for row in reader:
                        clean_row = {k.strip().lower(): (v.strip() if v else v) for k, v in row.items()}
                        email_value = clean_row.get("email")
                        if not email_value:
                            continue
                        # Variables: todas excepto email
                        variables = {k: v for k, v in clean_row.items() if k != 'email' and v}
                        # Validar variables requeridas
                        missing_vars = required_vars - set(variables.keys())
                        if missing_vars:
                            raise ValueError(
                                f"Faltan variables requeridas para {email_value}: {', '.join(missing_vars)}"
                            )
                        recipients.append({
                            "email": email_value,
                            "name": clean_row.get("nombres", ""),
                            "variables": variables
                        })
            except Exception as e:
                bulk.result = json.dumps({
                    "error": str(e),
                    "recipients": recipients,
                    "template_id": bulk.template_id
                })
                bulk.log = f"Error leyendo archivo: {e}"
                bulk.status = "error"
                bulk.save()
                continue

            # Adjuntos
            adj_list = [att.to_doppler_format() for att in bulk.attachments.all()]
            subject = bulk.subject

            try:
                response = process_bulk_template_send(
                    template_id=bulk.template_id,
                    recipients=recipients,
                    subject=subject,
                    adj_list=adj_list,
                    from_email=sender_config.from_email,
                    from_name=sender_config.from_name,
                    user=request.user,
                )
                bulk.result = (
                    response.content.decode("utf-8") if hasattr(response, 'content') else json.dumps(response)
                )
                bulk.status = "done"
                bulk.log = f"Envío realizado con remitente {sender_config.from_email}"
            except Exception as e:
                import traceback
                api_error = None
                if hasattr(e, 'payload'):
                    api_error = getattr(e, 'payload', None)
                bulk.result = json.dumps({
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                    "recipients": recipients,
                    "subject": subject,
                    "attachments": adj_list,
                    "template_id": bulk.template_id,
                    "api_error": api_error
                })
                bulk.status = "error"
                bulk.log = f"Error en envío: {e}"
            bulk.save()
            messages.info(request, f"BulkSend {bulk.id} procesado.")

    procesar_envio_masivo.short_description = "Procesar envío masivo seleccionado"

# Branding global del admin
from django.contrib import admin as _dj_admin
_dj_admin.site.site_header = "Ramirezvi Email Platform"
_dj_admin.site.site_title = "Ramirezvi Email Platform"
_dj_admin.site.index_title = "Welcome to Ramirezvi Email Platform"
