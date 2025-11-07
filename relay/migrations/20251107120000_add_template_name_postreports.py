from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("relay", "20251029151212_scheduled_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="bulksend",
            name="template_name",
            field=models.CharField(max_length=255, blank=True, null=True),
        ),
        migrations.AddField(
            model_name="bulksend",
            name="post_reports_status",
            field=models.CharField(max_length=16, blank=True, null=True),
        ),
        migrations.AddField(
            model_name="bulksend",
            name="post_reports_loaded_at",
            field=models.DateTimeField(null=True, blank=True),
        ),
    ]

