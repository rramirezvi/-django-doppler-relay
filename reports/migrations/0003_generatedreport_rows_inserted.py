from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reports", "0002_alter_generatedreport_options"),
    ]

    operations = [
        migrations.AddField(
            model_name="generatedreport",
            name="rows_inserted",
            field=models.IntegerField(default=0),
        ),
    ]

