# bulk-v2-real-send-canary (design.md §13): one additive, fully reversible
# migration. Every AddField is nullable or scalar-defaulted (no NOT NULL
# without a default). No RunPython, no RunSQL, no data migration — every
# operation has an automatic Django inverse.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('relay', '20260514150001_bulk_processing_v2_ledger'),
    ]

    operations = [
        migrations.AddField(
            model_name='bulksendrecipient',
            name='send_status',
            field=models.CharField(
                choices=[
                    ('not_started', 'Not started'),
                    ('sending', 'Sending'),
                    ('sent', 'Sent'),
                    ('send_failed', 'Send failed'),
                    ('ambiguous', 'Ambiguous'),
                ],
                default='not_started',
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='bulksendrecipient',
            name='send_attempt_number',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='bulksendrecipient',
            name='send_started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='bulksendrecipient',
            name='sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='bulksendrecipient',
            name='send_error_code',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='bulksendrecipient',
            name='send_error_message',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='bulksendrecipient',
            name='send_message_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='bulksendrecipient',
            name='send_location',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='bulksendrecipient',
            name='send_job_id',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='bulksendrecipient',
            index=models.Index(
                fields=['bulk_send', 'send_status'],
                name='bulk_recipient_send_status_idx',
            ),
        ),
        migrations.AddConstraint(
            model_name='bulksendrecipient',
            constraint=models.CheckConstraint(
                condition=models.Q(('send_status__in', [
                    'not_started', 'sending', 'sent', 'send_failed', 'ambiguous'
                ])),
                name='bulk_recipient_valid_send_status',
            ),
        ),
        migrations.AddConstraint(
            model_name='bulksendrecipient',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(('send_status', 'not_started'))
                    | ~models.Q(('status', 'invalid'))
                ),
                name='bulk_recipient_invalid_never_sends',
            ),
        ),
        migrations.AddConstraint(
            model_name='bulksendrecipient',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(('send_started_at__isnull', True), ('send_status', 'not_started'))
                    | (~models.Q(('send_status', 'not_started')) & models.Q(('send_started_at__isnull', False)))
                ),
                name='bulk_recipient_send_started_at_consistent',
            ),
        ),
        migrations.AddConstraint(
            model_name='bulksendrecipient',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(('send_attempt_number', 0), ('send_status', 'not_started'))
                    | (~models.Q(('send_status', 'not_started')) & models.Q(('send_attempt_number__gte', 1)))
                ),
                name='bulk_recipient_send_attempt_number_consistent',
            ),
        ),
        migrations.AddConstraint(
            model_name='bulksendrecipient',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(('send_status', 'sent'), ('sent_at__isnull', False))
                    | (~models.Q(('send_status', 'sent')) & models.Q(('sent_at__isnull', True)))
                ),
                name='bulk_recipient_sent_requires_sent_at',
            ),
        ),
        migrations.AddConstraint(
            model_name='bulksendrecipient',
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(('send_status__in', ['send_failed', 'ambiguous']))
                    | ~models.Q(('send_error_code', ''))
                ),
                name='bulk_recipient_send_outcome_requires_error_code',
            ),
        ),
        migrations.AlterField(
            model_name='backgroundjob',
            name='job_type',
            field=models.CharField(
                choices=[
                    ('bulk_send', 'Bulk send'),
                    ('post_report', 'Post-send report'),
                    ('bulk_send_v2_real', 'Bulk send V2 real'),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
    ]
