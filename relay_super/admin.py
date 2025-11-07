from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext_lazy as _

from relay.models import BulkSend, UserEmailConfig
from relay_super.models import BulkSendUserConfigProxy
from relay.services.bulk_processing import process_bulk_id
from relay.admin import BulkSendForm as BaseBulkSendForm


class SenderChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj: UserEmailConfig) -> str:  # type: ignore[override]
        return f"{obj.user.username} — {obj.from_email}"


class BulkSendSenderForm(BaseBulkSendForm):
    sender = SenderChoiceField(
        queryset=UserEmailConfig.objects.filter(is_active=True).select_related("user"),
        required=True,
        label=("Remitente (UserEmailConfig)"),
        help_text=("Seleccione el remitente (from_name / from_email) para este envío."),
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

    def procesar_envio_masivo(self, request, queryset):
        if not (request.user.is_active and request.user.is_staff and request.user.has_perm("relay_super.change_bulksenduserconfigproxy")):
            raise PermissionDenied("No tiene permiso para procesar envíos masivos.")
        # Ejecutar en background para no bloquear la request del admin
        from django.utils import timezone
        import threading
        for bulk in queryset:
            if bulk.status != "pending":
                messages.warning(request, f"BulkSend {bulk.id} ya procesado.")
                continue
            bulk.processing_started_at = timezone.now()
            bulk.log = ((bulk.log or "") + "\n[BG] Envío iniciado desde admin (por remitente)").strip()
            bulk.save(update_fields=["processing_started_at", "log"]) 
            threading.Thread(target=process_bulk_id, args=(bulk.id,), daemon=True).start()
            messages.info(request, f"BulkSend {bulk.id} en proceso (background). Revise el estado en la lista.")
        return
