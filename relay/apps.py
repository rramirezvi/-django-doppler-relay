from django.apps import AppConfig


class RelayConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'relay'

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(create_operator_group, sender=self, dispatch_uid="relay.create_operator_group")


def create_operator_group(sender, **kwargs):
    try:
        from relay.services.operator_permissions import ensure_operator_group

        ensure_operator_group()
    except Exception:
        # No bloquear migraciones si auth/contenttypes todavia no esta listo o falla la BD.
        pass
