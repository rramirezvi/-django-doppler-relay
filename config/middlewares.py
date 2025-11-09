from __future__ import annotations

from django.conf import settings
from django.contrib import admin


class HideReportsAdminMiddleware:
    _patched = False

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Parchear una sola vez por proceso
        if not HideReportsAdminMiddleware._patched:
            self._maybe_patch_admin_menu()
            HideReportsAdminMiddleware._patched = True
        return self.get_response(request)

    def _maybe_patch_admin_menu(self) -> None:
        try:
            if getattr(settings, "REPORTS_ADMIN_VISIBLE", False):
                return
            site = admin.site
            original_each_context = site.each_context

            def each_context(request):
                ctx = original_each_context(request)
                try:
                    apps = list(ctx.get("available_apps", []))
                    filtered = []
                    for app in apps:
                        if app.get("app_label") == "reports":
                            # Ocultar toda la sección Reports
                            continue
                        models = app.get("models") or []
                        if models:
                            app["models"] = [m for m in models if m.get("app_label") != "reports"]
                        filtered.append(app)
                    ctx["available_apps"] = filtered
                except Exception:
                    pass
                return ctx

            site.each_context = each_context
        except Exception:
            # No romper la request si el parche falla
            pass

