from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="TemplatesPermissionAnchor",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ],
            options={
                "db_table": "templates_admin_permission_anchor",
                "managed": True,
                "default_permissions": (),
                "permissions": (
                    ("manage_templates", "Puede administrar plantillas de Doppler (Templates)"),
                ),
            },
        ),
    ]

