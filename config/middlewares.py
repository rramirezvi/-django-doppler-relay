from __future__ import annotations

from django.conf import settings
from django.contrib import admin
from django.shortcuts import redirect


class HideReportsAdminMiddleware:
    _patched = False

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Parchear una sola vez por proceso
        if not HideReportsAdminMiddleware._patched:
            self._maybe_patch_admin_menu()
            HideReportsAdminMiddleware._patched = True
        if self._should_redirect_operator_from_admin(request):
            return redirect("/app/")
        return self.get_response(request)

    def _should_redirect_operator_from_admin(self, request) -> bool:
        path = request.path_info or ""
        if not path.startswith("/admin/"):
            return False
        if path.startswith("/admin/login/") or path.startswith("/admin/logout/"):
            return False
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return False
        return not bool(user.is_superuser)

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
