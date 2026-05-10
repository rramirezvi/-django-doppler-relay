from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import redirect
from django.views.generic import TemplateView

admin.site.site_header = "Email Platform Ramirezvi"
admin.site.site_title = "Email Platform Ramirezvi"
admin.site.index_title = "Panel de administración"


def home_redirect(request):
    if request.user.is_authenticated and request.user.is_superuser:
        return redirect("/admin/")
    return redirect("/app/")

urlpatterns = [
    path('', home_redirect, name='home'),
    path('admin/', admin.site.urls),
    path('app/', login_required(
        TemplateView.as_view(template_name='app/index.html'),
        login_url='/admin/login/',
    ), name='operator_app'),
    path('api/', include('relay.api_urls')),
    path('relay/', include('relay.urls')),
]

# Agregar URLs para archivos de media en desarrollo
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL,
                          document_root=settings.MEDIA_ROOT)
