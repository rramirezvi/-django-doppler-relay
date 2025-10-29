from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings

class Migration(migrations.Migration):
    dependencies = [
        ('relay', '0003_bulksend'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='bulksend',
            name='scheduled_at',
            field=models.DateTimeField(blank=True, null=True, db_index=True),
        ),
        migrations.AddField(
            model_name='bulksend',
            name='scheduled_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='bulksend',
            name='processing_started_at',
            field=models.DateTimeField(blank=True, null=True, db_index=True),
        ),
    ]
