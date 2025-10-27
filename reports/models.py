from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models


class GeneratedReport(models.Model):
    TYPE_DELIVERIES = "deliveries"
    TYPE_BOUNCES = "bounces"
    TYPE_OPENS = "opens"
    TYPE_CLICKS = "clicks"
    TYPE_SPAM = "spam"
    TYPE_UNSUBSCRIBED = "unsubscribed"
    TYPE_SENT = "sent"

    REPORT_TYPES = (
        (TYPE_DELIVERIES, "deliveries"),
        (TYPE_BOUNCES, "bounces"),
        (TYPE_OPENS, "opens"),
        (TYPE_CLICKS, "clicks"),
        (TYPE_SPAM, "spam"),
        (TYPE_UNSUBSCRIBED, "unsubscribed"),
        (TYPE_SENT, "sent"),
    )

    STATE_PENDING = "PENDING"
    STATE_PROCESSING = "PROCESSING"
    STATE_READY = "READY"
    STATE_ERROR = "ERROR"

    STATES = (
        (STATE_PENDING, "Pending"),
        (STATE_PROCESSING, "Processing"),
        (STATE_READY, "Ready"),
        (STATE_ERROR, "Error"),
    )

    report_type = models.CharField(max_length=32, choices=REPORT_TYPES)
    start_date = models.DateField()
    end_date = models.DateField()
    state = models.CharField(max_length=16, choices=STATES, default=STATE_PENDING)
    report_request_id = models.CharField(max_length=128, blank=True, default="")
    file_path = models.CharField(max_length=512, blank=True, default="")
    error_details = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    requested_by = models.ForeignKey(
        get_user_model(),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="generated_reports",
    )

    loaded_to_db = models.BooleanField(default=False)
    loaded_at = models.DateTimeField(null=True, blank=True)
    rows_inserted = models.IntegerField(default=0)
    last_loaded_alias = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        verbose_name = "Reporte generado"
        verbose_name_plural = "Reportes generados"
        permissions = (
            ("can_load_to_db", "Puede cargar reportes a la BD analítica"),
            ("can_process_reports", "Puede procesar reportes pendientes"),
        )

    def __str__(self) -> str:
        return f"{self.report_type} {self.start_date}..{self.end_date} [{self.state}]"
