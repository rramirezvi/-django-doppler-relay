from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reports", "0003_generatedreport_rows_inserted"),
    ]

    operations = [
        migrations.AddField(
            model_name="generatedreport",
            name="last_loaded_alias",
            field=models.CharField(max_length=64, blank=True, default=""),
        ),
    ]

