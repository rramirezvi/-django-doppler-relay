from __future__ import annotations

from django.contrib import admin, messages
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.shortcuts import redirect
from django.conf import settings

from relay.services.doppler_relay import DopplerRelayClient, DopplerRelayError
from .forms import TemplateForm
from .utils import read_cached_html, write_cached_html


class TemplatesAdminViews:
    title = "Templates"

    def __init__(self, admin_site: admin.AdminSite) -> None:
        self.admin_site = admin_site
        self._register()

    # ---- helpers ----
    def _account_id(self) -> int:
        return int(getattr(settings, "DOPPLER_RELAY", {}).get("ACCOUNT_ID", 0))

    def _client(self) -> DopplerRelayClient:
        return DopplerRelayClient()

    # ---- list ----
    def list_view(self, request):
        client = self._client()
        items: list[dict] = []
        try:
            data = client.list_templates(self._account_id())
            # Normalizar estructuras comunes (la API puede devolver diferentes formas)
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                for key in ("items", "templates", "data"):
                    value = data.get(key)
                    if isinstance(value, list):
                        items = value
                        break
                if not items and data.get("id"):
                    items = [data]
        except Exception as exc:
            messages.error(request, f"Error loading templates: {exc}")

        context = {
            **self.admin_site.each_context(request),
            "title": self.title,
            "templates_items": items,
        }
        return TemplateResponse(request, "templates_admin/list.html", context)

    # ---- create/edit ----
    def create_view(self, request):
        if request.method == "POST":
            form = TemplateForm(request.POST)
            if form.is_valid():
                try:
                    client = self._client()
                    client.create_template(
                        self._account_id(),
                        name=form.cleaned_data["name"],
                        subject=form.cleaned_data["subject"],
                        from_email=form.cleaned_data["from_email"],
                        from_name=form.cleaned_data.get("from_name") or None,
                        body_html=form.cleaned_data["body_html"],
                    )
                    messages.success(request, "Template created")
                    return redirect("admin:templates_admin_list")
                except DopplerRelayError as exc:
                    messages.error(request, f"API error: {exc}")
        else:
            form = TemplateForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "Create template",
            "form": form,
            "add": True,
            "change": False,
            # Fake opts for breadcrumbs
            "opts": type("_opts", (), {"app_label": "templates_admin", "model_name": "template", "object_name": "Template"})(),
        }
        return TemplateResponse(request, "templates_admin/form.html", context)

    def edit_view(self, request, template_id: str):
        client = self._client()
        try:
            data = client.get_template(self._account_id(), template_id)
        except Exception as exc:
            messages.error(request, f"Error loading template: {exc}")
            return redirect("admin:templates_admin_list")

        def _extract_html(payload: dict) -> str:
            for key in ("html", "htmlContent", "body", "content", "textContent"):
                val = payload.get(key)
                if isinstance(val, str) and val.strip():
                    return val
            for k in ("template", "data", "attributes"):
                sub = payload.get(k)
                if isinstance(sub, dict):
                    v = _extract_html(sub)
                    if v:
                        return v
            return ""

        initial = {
            "name": data.get("name", ""),
            "from_email": data.get("from_email", ""),
            "from_name": data.get("from_name", ""),
            "subject": data.get("subject", ""),
            "body_html": _extract_html(data),
        }

        # Seguir el enlace con rel '/docs/rels/get-template-body' si no vino en el JSON principal
        if not initial["body_html"]:
            try:
                links = data.get("_links")
                if isinstance(links, list):
                    body_href = None
                    for link in links:
                        if isinstance(link, dict) and link.get("rel") == "/docs/rels/get-template-body":
                            body_href = link.get("href")
                            break
                    if body_href:
                        resp = client._request("GET", body_href)
                        ct = (resp.headers.get("Content-Type") or "").lower()
                        if "text/html" in ct or "text/plain" in ct:
                            initial["body_html"] = resp.text
                        else:
                            try:
                                j = resp.json()
                                if isinstance(j, dict):
                                    initial["body_html"] = (
                                        j.get("html")
                                        or j.get("htmlContent")
                                        or j.get("body")
                                        or j.get("content")
                                        or j.get("textContent")
                                        or ""
                                    )
                            except Exception:
                                pass
            except Exception:
                pass

        # Como último recurso, leer del caché local
        if not initial["body_html"]:
            cached = read_cached_html(template_id)
            if isinstance(cached, str) and cached.strip():
                initial["body_html"] = cached

        if request.method == "POST":
            form = TemplateForm(request.POST)
            if form.is_valid():
                try:
                    client.update_template(
                        self._account_id(),
                        template_id,
                        name=form.cleaned_data["name"],
                        subject=form.cleaned_data["subject"],
                        from_email=form.cleaned_data["from_email"],
                        from_name=form.cleaned_data.get("from_name") or None,
                        body_html=form.cleaned_data["body_html"],
                    )
                    write_cached_html(template_id, form.cleaned_data["body_html"])
                    messages.success(request, "Template updated")
                    return redirect("admin:templates_admin_list")
                except DopplerRelayError as exc:
                    messages.error(request, f"API error: {exc}")
        else:
            form = TemplateForm(initial=initial)

        context = {
            **self.admin_site.each_context(request),
            "title": f"Edit template {template_id}",
            "form": form,
            "original": template_id,
            "opts": type("_opts", (), {"app_label": "templates_admin", "model_name": "template", "object_name": "Template"})(),
            "template_id": template_id,
        }
        return TemplateResponse(request, "templates_admin/form.html", context)

    def delete_view(self, request, template_id: str):
        if request.method != "POST":
            # small confirmation page
            context = {
                **self.admin_site.each_context(request),
                "title": "Confirm delete",
                "template_id": template_id,
            }
            return TemplateResponse(request, "templates_admin/confirm_delete.html", context)

        try:
            client = self._client()
            client.delete_template(self._account_id(), template_id)
            messages.success(request, "Template deleted")
        except DopplerRelayError as exc:
            messages.error(request, f"API error: {exc}")
        return redirect("admin:templates_admin_list")

    # ---- registration in admin ----
    def _register(self) -> None:
        if getattr(self.admin_site, "_templates_custom_registered", False):
            return

        original_get_urls = self.admin_site.get_urls

        def get_urls():
            custom = [
                path("templates/", self.admin_site.admin_view(self.list_view), name="templates_admin_list"),
                path("templates/new/", self.admin_site.admin_view(self.create_view), name="templates_admin_new"),
                path("templates/<str:template_id>/edit/", self.admin_site.admin_view(self.edit_view), name="templates_admin_edit"),
                path("templates/<str:template_id>/delete/", self.admin_site.admin_view(self.delete_view), name="templates_admin_delete"),
            ]
            return custom + original_get_urls()

        self.admin_site.get_urls = get_urls

        original_each_context = self.admin_site.each_context

        def each_context(request):
            context = original_each_context(request)
            try:
                list_url = reverse("admin:templates_admin_list")
            except Exception:
                list_url = None
            if list_url:
                apps = context.setdefault("available_apps", [])
                entry = {
                    "name": self.title,
                    "app_label": "templates_admin",
                    "app_url": list_url,
                    "has_module_perms": True,
                    "models": [
                        {"name": "Templates", "object_name": "Template", "admin_url": list_url, "view_only": True}
                    ],
                }
                # Si ya existe, reemplazar nombre/URL
                found = False
                for i, app in enumerate(apps):
                    if app.get("app_label") == "templates_admin":
                        apps[i] = entry
                        found = True
                        break
                if not found:
                    apps.append(entry)
            return context

        self.admin_site.each_context = each_context

        # Ensure it appears on admin index app list as well (without models)
        original_get_app_list = getattr(self.admin_site, "get_app_list")

        def get_app_list(request):
            app_list = list(original_get_app_list(request))
            try:
                list_url = reverse("admin:templates_admin_list")
            except Exception:
                list_url = None
            if list_url:
                # If not present, append a minimal entry so it shows up on index
                found = any(app.get("app_label") == "templates_admin" for app in app_list)
                if not found:
                    app_list.append({
                        "name": self.title,
                        "app_label": "templates_admin",
                        "app_url": list_url,
                        "has_module_perms": True,
                        "models": [
                            {"name": "Templates", "object_name": "Template", "admin_url": list_url, "view_only": True}
                        ],
                    })
            return app_list

        self.admin_site.get_app_list = get_app_list
        self.admin_site._templates_custom_registered = True


# Register the custom admin views
TemplatesAdminViews(admin.site)

