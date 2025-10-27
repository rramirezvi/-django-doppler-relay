from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='GeneratedReport',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('report_type', models.CharField(choices=[('deliveries', 'deliveries'), ('bounces', 'bounces'), ('opens', 'opens'), ('clicks', 'clicks'), ('spam', 'spam'), ('unsubscribed', 'unsubscribed'), ('sent', 'sent')], max_length=32)),
                ('start_date', models.DateField()),
                ('end_date', models.DateField()),
                ('state', models.CharField(choices=[('PENDING', 'Pending'), ('PROCESSING', 'Processing'), ('READY', 'Ready'), ('ERROR', 'Error')], default='PENDING', max_length=16)),
                ('report_request_id', models.CharField(blank=True, default='', max_length=128)),
                ('file_path', models.CharField(blank=True, default='', max_length=512)),
                ('error_details', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('loaded_to_db', models.BooleanField(default=False)),
                ('loaded_at', models.DateTimeField(blank=True, null=True)),
                ('requested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='generated_reports', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Reporte generado',
                'verbose_name_plural': 'Reportes generados',
                'permissions': (('can_load_to_db', 'Puede cargar reportes a la BD analítica'),),
            },
        ),
    ]

