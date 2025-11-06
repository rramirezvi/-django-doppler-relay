from django.db import models


class TemplatesPermissionAnchor(models.Model):
    """
    Modelo ancla sin datos de negocio. Solo existe para declarar permisos
    asignables desde el admin (p. ej. templates_admin.manage_templates).
    """

    class Meta:
        managed = True
        db_table = "templates_admin_permission_anchor"
        default_permissions = ()  # no add/change/delete/view por defecto
        permissions = (
            ("manage_templates", "Puede administrar plantillas de Doppler (Templates)"),
        )

    def __str__(self) -> str:
        return "Templates admin permissions"

