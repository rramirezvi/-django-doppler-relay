from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html
from django.urls import reverse

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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Corregir ayuda del campo sender con acentos
        try:
            if 'sender' in self.fields:
                self.fields['sender'].help_text = (
                    'Seleccione el remitente (from_name / from_email) para este envío.'
                )
        except Exception:
            pass

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
    list_display = ("id", "template_display", "subject", "created_at", "status", "report_link", "report_link_v2", "report_csv_window")
    readonly_fields = ("result", "log", "status", "created_at", "processing_started_at", "template_name", "variables", "post_reports_status", "post_reports_loaded_at")
    search_fields = ("template_id", "subject")
    list_filter = ("status",)
    actions = ["procesar_envio_masivo"]
    filter_horizontal = ("attachments",)

    def get_exclude(self, request, obj=None):
        base = list(super().get_exclude(request, obj) or [])
        technical = [
            "processing_started_at",
            "post_reports_status",
            "post_reports_loaded_at",
            "template_name",
            "variables",
            "result",
            "log",
            "status",
            "created_at",
        ]
        if obj is None:
            return base + technical
        return base

    # Mostrar nombre de plantilla si existe; si no, el ID
    def template_display(self, obj: BulkSend):
        return obj.template_name or obj.template_id
    template_display.short_description = "Plantilla"

    def report_link(self, obj: BulkSend):
        # Mostrar solo cuando el envío terminó y la carga post‑envío está lista
        if getattr(obj, "status", "") == "done" and getattr(obj, "post_reports_loaded_at", None):
            try:
                url = reverse("admin:relay_bulksend_report", args=[obj.pk])
                return format_html('<a class="button" href="{}">Ver reporte</a>', url)
            except Exception:
                return ""
        return ""
    report_link.short_description = "Reporte"

    def report_link_v2(self, obj: BulkSend):
        # Mismas condiciones de visibilidad que el botón actual
        if getattr(obj, "status", "") == "done" and getattr(obj, "post_reports_loaded_at", None):
            try:
                url = reverse("admin:relay_bulksend_report_v2", args=[obj.pk])
                return format_html('<a class="button" href="{}">Ver reporte (nuevo)</a>', url)
            except Exception:
                return ""
        return ""
    report_link_v2.short_description = "Reporte v2"

    
    def report_csv_window(self, obj: BulkSend):
        # CSV del envío (ventana local)
        if getattr(obj, "status", "") == "done" and getattr(obj, "post_reports_loaded_at", None):
            try:
                url = reverse("admin:relay_bulksend_report_v2_csv_window", args=[obj.pk])
                return format_html('<a class="button" href="{}">CSV de este envío</a>', url)
            except Exception:
                return ""
        return ""
    report_csv_window.short_description = "CSV envío"`r`n`r`n    def procesar_envio_masivo(self, request, queryset):
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




