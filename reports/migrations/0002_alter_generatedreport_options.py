from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("reports", "0001_initial"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="generatedreport",
            options={
                "verbose_name": "Reporte generado",
                "verbose_name_plural": "Reportes generados",
                "permissions": (
                    ("can_load_to_db", "Puede cargar reportes a la BD analítica"),
                    ("can_process_reports", "Puede procesar reportes pendientes"),
                ),
            },
        ),
    ]

