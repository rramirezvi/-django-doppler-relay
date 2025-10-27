from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import GeneratedReport
from .services.loader import load_report_to_db


@admin.register(GeneratedReport)
class GeneratedReportAdmin(admin.ModelAdmin):
    change_list_template = "reports/generatedreport_changelist.html"
    list_display = (
        "id",
        "report_type",
        "start_date",
        "end_date",
        "state",
        "requested_by",
        "created_at",
        "download_link",
        "load_links",
        "loaded_to_db",
        "rows_inserted",
        "loaded_badge",
    )
    list_filter = ("state", "report_type", "loaded_to_db")
    search_fields = ("report_request_id", "file_path")
    readonly_fields = ("state", "report_request_id", "file_path", "error_details", "created_at", "updated_at", "loaded_to_db", "loaded_at")
    fields = ("report_type", "start_date", "end_date",) + readonly_fields

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("requested_by")

    def save_model(self, request, obj: GeneratedReport, form, change):
        if not change:
            obj.requested_by = request.user if request and request.user.is_authenticated else None
            obj.state = GeneratedReport.STATE_PENDING
        super().save_model(request, obj, form, change)

    def download_link(self, obj: GeneratedReport):
        if obj.state == GeneratedReport.STATE_READY and obj.file_path:
            url = reverse("admin:reports_generatedreport_download", args=(obj.pk,))
            return format_html('<a href="{}">Descargar CSV</a>', url)
        return ""
    download_link.short_description = "Descarga"


    def loaded_badge(self, obj: GeneratedReport):
        alias = (obj.last_loaded_alias or "").strip()
        if not alias:
            return ""
        style = (
            "display:inline-block;padding:2px 6px;border-radius:10px;"
            "background:#e1f3e8;color:#0a7a3b;font-size:11px;"
        )
        return format_html('<span style="{}">Cargado en: {}</span>', style, alias)
    loaded_badge.short_description = "Carga"
    def load_links(self, obj: GeneratedReport):
        if obj.state != GeneratedReport.STATE_READY:
            return ""
        links = []
        # Evitar doble carga para alias ya utilizado
        if not (obj.loaded_to_db and (obj.last_loaded_alias or "") == "default"):
            url_default = reverse("admin:reports_generatedreport_load", args=(obj.pk,)) + "?alias=default"
            links.append(format_html('<a href="{}">Cargar BD (default)</a>', url_default))
        # Si existe alias 'analytics', mostrar también si no fue usado
        from django.conf import settings
        if "analytics" in getattr(settings, "DATABASES", {}):
            if not (obj.loaded_to_db and (obj.last_loaded_alias or "") == "analytics"):
                url_analytics = reverse("admin:reports_generatedreport_load", args=(obj.pk,)) + "?alias=analytics"
                links.append(format_html('<a href="{}" style="margin-left:8px;">Cargar BD (analytics)</a>', url_analytics))
        return format_html("{}", format_html(" ".join([str(l) for l in links])))
    load_links.short_description = "Cargar a BD"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:pk>/download/",
                self.admin_site.admin_view(self.download_view),
                name="reports_generatedreport_download",
            ),
            path(
                "<int:pk>/load_to_db/",
                self.admin_site.admin_view(self.load_to_db_view),
                name="reports_generatedreport_load",
            ),
            path(
                "process_pending/",
                self.admin_site.admin_view(self.process_pending_view),
                name="reports_generatedreport_process_pending",
            ),
        ]
        return custom + urls

    def download_view(self, request, pk: int):
        from pathlib import Path
        from django.http import FileResponse, Http404
        obj = self.get_object(request, pk)
        if not obj or obj.state != GeneratedReport.STATE_READY or not obj.file_path:
            raise Http404("Reporte no disponible para descarga")
        path = Path(obj.file_path)
        if not path.exists():
            raise Http404("Archivo no encontrado")
        return FileResponse(open(path, "rb"), as_attachment=True, filename=path.name)

    def load_to_db_view(self, request, pk: int):
        if not request.user.has_perm("reports.can_load_to_db"):
            messages.error(request, "No tiene permiso para cargar datos a la BD")
            return HttpResponseRedirect(reverse("admin:reports_generatedreport_change", args=(pk,)))
        # Evitar doble carga para el mismo alias
        alias = request.GET.get("alias", "default")
        obj = self.get_object(request, pk)
        if obj and obj.loaded_to_db and (obj.last_loaded_alias or "") == alias:
            messages.info(request, f"Este reporte ya fue cargado a '{alias}'.")
            return HttpResponseRedirect(reverse("admin:reports_generatedreport_change", args=(pk,)))
        try:
            alias = request.GET.get("alias", "default")
            rows = load_report_to_db(pk, target_alias=alias)
            messages.success(request, f"Carga a BD completada en '{alias}' ({rows} filas)")
        except Exception as exc:
            messages.error(request, f"Error cargando a BD: {exc}")
        return HttpResponseRedirect(reverse("admin:reports_generatedreport_change", args=(pk,)))

    def process_pending_view(self, request):
        from django.shortcuts import redirect
        if request.method != "POST":
            return redirect("admin:reports_generatedreport_changelist")
        if not request.user.has_perm("reports.can_process_reports"):
            messages.error(request, "No tiene permiso para procesar reportes")
            return redirect("admin:reports_generatedreport_changelist")
        try:
            from .services.processor import process_pending_reports
            process_pending_reports()
            messages.success(request, "Procesamiento de reportes pendiente/processing ejecutado")
        except Exception as exc:
            messages.error(request, f"Error procesando reportes: {exc}")
        return redirect("admin:reports_generatedreport_changelist")


class ReportRequestForm(forms.Form):
    REPORT_CHOICES = [
        ("deliveries", "Deliveries"),
        ("bounces", "Bounces"),
        ("opens", "Opens"),
        ("clicks", "Clicks"),
        ("spam", "Spam"),
        ("unsubscribed", "Unsubscribed"),
        ("sent", "Sent"),
    ]

    tipo_reporte = forms.ChoiceField(label="Tipo de reporte", choices=REPORT_CHOICES)
    fecha_inicio = forms.DateField(label="Fecha inicio", widget=forms.DateInput(attrs={"type": "date"}))
    fecha_fin = forms.DateField(label="Fecha fin", widget=forms.DateInput(attrs={"type": "date"}))

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("fecha_inicio")
        end = cleaned.get("fecha_fin")
        if start and end and start > end:
            self.add_error("fecha_fin", "La fecha fin debe ser igual o posterior a la fecha inicio.")
        return cleaned


class ReportsAdminViews:
    title = "Solicitar reporte"
    form_class = ReportRequestForm

    def __init__(self, admin_site: admin.AdminSite) -> None:
        self.admin_site = admin_site
        self._register()

    def _register(self) -> None:
        if getattr(self.admin_site, "_reports_custom_registered", False):
            return

        original_get_urls = self.admin_site.get_urls

        def get_urls():
            custom = [
                path("reports/request/", self.admin_site.admin_view(self.request_view), name="reports_request"),
            ]
            return custom + original_get_urls()

        self.admin_site.get_urls = get_urls

        original_each_context = self.admin_site.each_context

        def each_context(request):
            context = original_each_context(request)
            try:
                request_url = reverse("admin:reports_request")
            except Exception:
                request_url = None
            if request_url:
                available_apps = context.setdefault("available_apps", [])
                link_entry = {
                    "name": self.title,
                    "object_name": "ReportRequest",
                    "admin_url": request_url,
                    "view_only": True,
                }
                reports_app = next((app for app in available_apps if app.get("app_label") == "reports"), None)
                if reports_app is None:
                    from django.urls import reverse as _rev
                    available_apps.append({
                        "name": "Reports",
                        "app_label": "reports",
                        "app_url": _rev("admin:app_list", args=("reports",)),
                        "has_module_perms": True,
                        "models": [link_entry],
                    })
                else:
                    models = reports_app.setdefault("models", [])
                    if not any(model.get("admin_url") == request_url for model in models):
                        models.insert(0, link_entry)
            return context

        self.admin_site.each_context = each_context
        self.admin_site._reports_custom_registered = True

    def request_view(self, request):
        if not request.user.has_perm("reports.add_generatedreport"):
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden("No tiene permiso para solicitar reportes")

        if request.method == "POST":
            form = self.form_class(request.POST)
            if form.is_valid():
                report_type = form.cleaned_data["tipo_reporte"]
                start_date = form.cleaned_data["fecha_inicio"]
                end_date = form.cleaned_data["fecha_fin"]
                GeneratedReport.objects.create(
                    report_type=str(report_type),
                    start_date=start_date,
                    end_date=end_date,
                    requested_by=request.user if request.user.is_authenticated else None,
                    state=GeneratedReport.STATE_PENDING,
                )
                messages.success(request, "Solicitud registrada. Revise 'Reportes generados' para el estado y descarga.")
                from django.shortcuts import redirect
                return redirect("admin:reports_generatedreport_changelist")
        else:
            today = timezone.localdate()
            form = self.form_class(initial={
                "tipo_reporte": "deliveries",
                "fecha_inicio": today,
                "fecha_fin": today,
            })

        fieldsets = ((None, {"fields": list(form.fields.keys())}),)
        admin_form = helpers.AdminForm(form, fieldsets, {})
        context = {
            **self.admin_site.each_context(request),
            "title": self.title,
            "adminform": admin_form,
            "form": form,
            "media": form.media,
            # Ajuste: usar GeneratedReport para que los breadcrumbs funcionen
            # y no intente resolver 'reports_reportrequest_changelist'.
            "opts": type("_opts", (), {"app_label": "reports", "model_name": "generatedreport", "object_name": "GeneratedReport"})(),
            "add": True,
            "change": False,
            "is_popup": False,
            "save_on_top": False,
            "save_on_bottom": True,
            "show_save": True,
            "show_save_and_add_another": False,
            "show_save_and_continue": False,
            "has_view_permission": True,
            "has_add_permission": True,
            "has_change_permission": False,
            "has_delete_permission": False,
            "has_editable_inline_admin_formsets": False,
            "inline_admin_formsets": [],
            "has_inlines": False,
            "original": None,
            "save_as": False,
            "preserved_filters": getattr(self.admin_site, 'get_preserved_filters', lambda r: '')(request),
            "form_url": "",
        }
        return TemplateResponse(request, "admin/change_form.html", context)


# Registrar vistas personalizadas de Reports en el admin
ReportsAdminViews(admin.site)
