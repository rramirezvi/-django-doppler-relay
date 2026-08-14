# Modelo para registrar envíos masivos con plantilla
from __future__ import annotations
import base64
from django.db import models
from django.core.files.base import ContentFile
from django.contrib.auth.models import User


class UserEmailConfig(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    from_email = models.EmailField(
        verbose_name="Email del remitente",
        help_text="Email que se usará como remitente para los envíos"
    )
    from_name = models.CharField(
        max_length=255,
        verbose_name="Nombre del remitente",
        help_text="Nombre que aparecerá como remitente"
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="Activo",
        help_text="Indica si esta configuración está activa"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Configuración de email de usuario"
        verbose_name_plural = "Configuraciones de email de usuarios"

    def __str__(self):
        return f"{self.user.username} - {self.from_email}"

    @classmethod
    def get_user_email_config(cls, user):
        """
        Obtiene la configuración de email activa para un usuario.
        Si no existe, retorna None.
        """
        if not user or not user.is_authenticated:
            return None

        try:
            return cls.objects.get(user=user, is_active=True)
        except cls.DoesNotExist:
            return None

    @classmethod
    def get_from_email_for_user(cls, user, fallback=None):
        """
        Obtiene el email del remitente para un usuario específico.
        Prioridad: 1) Configuración personalizada, 2) Email del usuario Django, 3) Fallback
        """
        if not user or not user.is_authenticated:
            return fallback

        # Prioridad 1: Configuración personalizada del usuario
        config = cls.get_user_email_config(user)
        if config:
            return config.from_email

        # Prioridad 2: Email del usuario de Django
        if user.email:
            return user.email

        # Prioridad 3: Fallback proporcionado
        return fallback

    @classmethod
    def get_from_name_for_user(cls, user, fallback=None):
        """
        Obtiene el nombre del remitente para un usuario específico.
        Prioridad: 1) Configuración personalizada, 2) Nombre del usuario Django, 3) Fallback
        """
        if not user or not user.is_authenticated:
            return fallback

        # Prioridad 1: Configuración personalizada del usuario
        config = cls.get_user_email_config(user)
        if config:
            return config.from_name

        # Prioridad 2: Nombre completo del usuario de Django
        if user.first_name or user.last_name:
            return f"{user.first_name} {user.last_name}".strip()

        # Prioridad 3: Username del usuario
        if user.username:
            return user.username

        # Prioridad 4: Fallback proporcionado
        return fallback


class Attachment(models.Model):
    name = models.CharField(max_length=255)
    file = models.FileField(upload_to='attachments/%Y/%m/')
    content_type = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Adjunto"
        verbose_name_plural = "Adjuntos"

    def to_doppler_format(self):
        """Convierte el archivo a formato base64 para Doppler"""
        content = base64.b64encode(self.file.read()).decode('utf-8')
        # Usar el nombre real del archivo en lugar del nombre personalizado
        filename = self.file.name.split(
            '/')[-1] if '/' in self.file.name else self.file.name
        return {
            'filename': filename,
            'content': content
        }

    @classmethod
    def from_doppler_format(cls, attachment_data):
        """Crea un adjunto desde el formato de Doppler"""
        content = base64.b64decode(attachment_data['content'])
        instance = cls(
            name=attachment_data['name'],
            content_type=attachment_data.get(
                'type', 'application/octet-stream')
        )
        instance.file.save(
            name=attachment_data['name'],
            content=ContentFile(content),
            save=False
        )
        instance.save()
        return instance


class BulkSend(models.Model):
    ENGINE_LEGACY = "legacy"
    ENGINE_V2 = "v2"
    ENGINE_CHOICES = (
        (ENGINE_LEGACY, "Legacy"),
        (ENGINE_V2, "V2"),
    )

    IMPORT_NOT_STARTED = "not_started"
    IMPORT_IMPORTING = "importing"
    IMPORT_READY = "ready"
    IMPORT_READY_WITH_ERRORS = "ready_with_errors"
    IMPORT_ERROR = "error"
    IMPORT_STATUS_CHOICES = (
        (IMPORT_NOT_STARTED, "Not started"),
        (IMPORT_IMPORTING, "Importing"),
        (IMPORT_READY, "Ready"),
        (IMPORT_READY_WITH_ERRORS, "Ready with errors"),
        (IMPORT_ERROR, "Error"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    client_request_id = models.CharField(
        max_length=64, blank=True, null=True, unique=True, db_index=True
    )
    template_id = models.CharField(max_length=128)
    template_name = models.CharField(max_length=255, blank=True, null=True)
    subject = models.CharField(max_length=255, blank=True, null=True)
    variables = models.JSONField(default=dict, blank=True)
    recipients_file = models.FileField(upload_to="bulk_recipients/")
    attachments = models.ManyToManyField(Attachment, blank=True)
    status = models.CharField(max_length=32, default="pending")
    result = models.JSONField(default=dict, blank=True)
    log = models.TextField(blank=True, null=True)
    # Envíos programados (opcional)
    scheduled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    scheduled_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Flag/ts de trabajo para evitar solapes (uso interno)
    processing_started_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Trazabilidad de reportería post-envío (automatizada)
    post_reports_status = models.CharField(max_length=16, blank=True, null=True)
    post_reports_loaded_at = models.DateTimeField(null=True, blank=True)

    engine_version = models.CharField(
        max_length=16,
        choices=ENGINE_CHOICES,
        default=ENGINE_LEGACY,
        db_index=True,
    )
    import_version = models.PositiveIntegerField(default=0)
    import_status = models.CharField(
        max_length=24,
        choices=IMPORT_STATUS_CHOICES,
        default=IMPORT_NOT_STARTED,
        db_index=True,
    )
    imported_rows = models.PositiveBigIntegerField(default=0)
    valid_rows = models.PositiveBigIntegerField(default=0)
    invalid_rows = models.PositiveBigIntegerField(default=0)
    import_started_at = models.DateTimeField(null=True, blank=True)
    import_finished_at = models.DateTimeField(null=True, blank=True)
    import_error = models.TextField(blank=True, default="")

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                "engine_version", "import_status"
            ).first()
            if (
                previous
                and previous["engine_version"] != self.engine_version
                and previous["import_status"] != self.IMPORT_NOT_STARTED
            ):
                raise ValueError(
                    "engine_version no puede cambiar una vez iniciada la importacion."
                )
        if self.engine_version == self.ENGINE_V2 and not self.template_name:
            raise ValueError(
                "template_name es obligatorio para el motor v2 y no puede "
                "resolverse mediante servicios externos."
            )
        # Completar template_name de forma centralizada (best‑effort) solo en legacy.
        try:
            if (
                self.engine_version == self.ENGINE_LEGACY
                and self.template_id
                and not self.template_name
            ):
                from django.conf import settings
                from .services.doppler_relay import DopplerRelayClient
                account = getattr(settings, 'DOPPLER_RELAY', {}) or {}
                account_id = account.get('ACCOUNT_ID')
                if account_id:
                    client = DopplerRelayClient()
                    data = client.get_template(int(account_id), str(self.template_id))
                    name = (data.get('name') or '').strip()
                    if name:
                        self.template_name = name
                    else:
                        # fallback mínimo legible
                        self.template_name = str(self.template_id)
        except Exception:
            # No bloquear el guardado si la API falla
            if self.template_id and not self.template_name:
                self.template_name = str(self.template_id)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"BulkSend {self.id} - {self.template_id} ({self.created_at:%Y-%m-%d %H:%M})"

    class Meta:
        verbose_name = "Envio masivo"
        verbose_name_plural = "Envios masivos"


class BackgroundJob(models.Model):
    TYPE_BULK_SEND = "bulk_send"
    TYPE_POST_REPORT = "post_report"
    # bulk-v2-real-send-canary (design.md §13/§14, PR2b-T4): named constant
    # for the choice value widened by PR2a's migration (choices-only, no DB
    # CHECK on job_type). The executable dispatch branch lives in
    # relay/services/jobs.py::dispatch_background_job (function-local import
    # of process_bulk_id_v2, per design §14).
    TYPE_BULK_SEND_V2_REAL = "bulk_send_v2_real"
    TYPE_CHOICES = (
        (TYPE_BULK_SEND, "Bulk send"),
        (TYPE_POST_REPORT, "Post-send report"),
        (TYPE_BULK_SEND_V2_REAL, "Bulk send V2 real"),
    )

    STATE_QUEUED = "queued"
    STATE_RUNNING = "running"
    STATE_DONE = "done"
    STATE_ERROR = "error"
    STATE_CHOICES = (
        (STATE_QUEUED, "Queued"),
        (STATE_RUNNING, "Running"),
        (STATE_DONE, "Done"),
        (STATE_ERROR, "Error"),
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    job_type = models.CharField(max_length=32, choices=TYPE_CHOICES, db_index=True)
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=STATE_QUEUED, db_index=True)
    bulk = models.ForeignKey(BulkSend, null=True, blank=True, on_delete=models.CASCADE, related_name="jobs")
    triggered_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    attempts = models.PositiveIntegerField(default=0)
    message = models.CharField(max_length=255, blank=True, default="")
    error = models.TextField(blank=True, default="")
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "Trabajo en segundo plano"
        verbose_name_plural = "Trabajos en segundo plano"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["job_type", "state"]),
            models.Index(fields=["bulk", "created_at"]),
        ]

    def __str__(self):
        return f"{self.job_type} #{self.pk} [{self.state}]"


class BulkSendRecipient(models.Model):
    STATUS_PENDING = "pending"
    STATUS_INVALID = "invalid"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_INVALID, "Invalid"),
    )

    # bulk-v2-real-send-canary (design.md §2.1): independent send-progress
    # lifecycle, never reused from `status` above (`status` stays import-only
    # per design §2.6).
    SEND_NOT_STARTED = "not_started"
    SEND_SENDING = "sending"
    SEND_SENT = "sent"
    SEND_FAILED = "send_failed"
    SEND_AMBIGUOUS = "ambiguous"
    SEND_STATUS_CHOICES = (
        (SEND_NOT_STARTED, "Not started"),
        (SEND_SENDING, "Sending"),
        (SEND_SENT, "Sent"),
        (SEND_FAILED, "Send failed"),
        (SEND_AMBIGUOUS, "Ambiguous"),
    )
    SEND_TERMINAL = frozenset({SEND_SENT, SEND_FAILED, SEND_AMBIGUOUS})
    SEND_STARTED = frozenset({SEND_SENDING, SEND_SENT, SEND_FAILED, SEND_AMBIGUOUS})

    bulk_send = models.ForeignKey(
        BulkSend,
        on_delete=models.CASCADE,
        related_name="recipient_occurrences",
    )
    import_version = models.PositiveIntegerField()
    source_row_number = models.PositiveBigIntegerField()
    recipient = models.TextField(blank=True, default="")
    normalized_recipient = models.TextField(blank=True, default="")
    payload = models.JSONField(default=dict)
    payload_hash = models.CharField(max_length=64)
    idempotency_key = models.UUIDField(unique=True, editable=False)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    last_error_message = models.CharField(max_length=255, blank=True, default="")

    # bulk-v2-real-send-canary (design.md §2.1): nine additive fields, all
    # nullable or scalar-defaulted. `send_job_id` is deliberately a plain
    # BigIntegerField, NOT a ForeignKey — BackgroundJob is orchestration,
    # not the source of truth for the send ledger (design §2.1 rationale).
    send_status = models.CharField(
        max_length=16, choices=SEND_STATUS_CHOICES, default=SEND_NOT_STARTED
    )
    send_attempt_number = models.PositiveIntegerField(default=0)
    send_started_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    send_error_code = models.CharField(max_length=64, blank=True, default="")
    send_error_message = models.CharField(max_length=255, blank=True, default="")
    send_message_id = models.CharField(max_length=128, blank=True, default="")
    send_location = models.CharField(max_length=255, blank=True, default="")
    send_job_id = models.BigIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Ocurrencia de destinatario"
        verbose_name_plural = "Ocurrencias de destinatarios"
        constraints = [
            models.UniqueConstraint(
                fields=["bulk_send", "import_version", "source_row_number"],
                name="uniq_bulk_import_source_row",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["pending", "invalid"]),
                name="bulk_recipient_valid_status",
            ),
            models.CheckConstraint(
                condition=models.Q(import_version__gte=1),
                name="bulk_recipient_import_version_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(source_row_number__gte=1),
                name="bulk_recipient_source_row_gte_1",
            ),
            # bulk-v2-real-send-canary (design.md §2.4) — six new invariants,
            # verbatim, appended after the four existing constraints above.
            models.CheckConstraint(
                condition=models.Q(send_status__in=[
                    "not_started", "sending", "sent", "send_failed", "ambiguous"
                ]),
                name="bulk_recipient_valid_send_status",
            ),
            models.CheckConstraint(
                # status=invalid can never progress past not_started.
                condition=models.Q(send_status="not_started") | ~models.Q(status="invalid"),
                name="bulk_recipient_invalid_never_sends",
            ),
            models.CheckConstraint(
                # not_started <=> send_started_at IS NULL
                condition=(
                    models.Q(send_status="not_started", send_started_at__isnull=True)
                    | (~models.Q(send_status="not_started") & models.Q(send_started_at__isnull=False))
                ),
                name="bulk_recipient_send_started_at_consistent",
            ),
            models.CheckConstraint(
                # not_started <=> attempt 0; every started state has >= 1 attempt.
                condition=(
                    models.Q(send_status="not_started", send_attempt_number=0)
                    | (~models.Q(send_status="not_started") & models.Q(send_attempt_number__gte=1))
                ),
                name="bulk_recipient_send_attempt_number_consistent",
            ),
            models.CheckConstraint(
                # sent <=> sent_at IS NOT NULL
                condition=(
                    models.Q(send_status="sent", sent_at__isnull=False)
                    | (~models.Q(send_status="sent") & models.Q(sent_at__isnull=True))
                ),
                name="bulk_recipient_sent_requires_sent_at",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(send_status__in=["send_failed", "ambiguous"])
                    | ~models.Q(send_error_code="")
                ),
                name="bulk_recipient_send_outcome_requires_error_code",
            ),
        ]
        indexes = [
            models.Index(
                fields=["bulk_send", "status"],
                name="bulk_recipient_status_idx",
            ),
            models.Index(
                fields=["bulk_send", "import_version", "source_row_number"],
                name="bulk_recipient_order_idx",
            ),
            models.Index(
                fields=["bulk_send", "send_status"],
                name="bulk_recipient_send_status_idx",
            ),
        ]

    def __str__(self):
        return (
            f"BulkSend {self.bulk_send_id} v{self.import_version} "
            f"row {self.source_row_number} [{self.status}]"
        )

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                "bulk_send_id", "import_version", "source_row_number", "send_status"
            ).first()
            if previous and (
                previous["bulk_send_id"] != self.bulk_send_id
                or previous["import_version"] != self.import_version
                or previous["source_row_number"] != self.source_row_number
            ):
                raise ValueError(
                    "La posicion logica y version de una ocurrencia son inmutables."
                )
            # bulk-v2-real-send-canary (design.md §2.5 point 1): defensive
            # net against ORM-object writes originating outside
            # relay/services/bulk_v2_send_state.py, which is the primary
            # transition mechanism (compare-and-set UPDATE). Not atomic,
            # costs one extra query per save — intentional, per design.
            if previous and (
                (previous["send_status"] == self.SEND_SENT and self.send_status != self.SEND_SENT)
                or (
                    previous["send_status"] in self.SEND_TERMINAL
                    and self.send_status != previous["send_status"]
                )
            ):
                raise ValueError(
                    "send_status no puede retroceder desde un estado terminal "
                    "(bulk-v2-real-send-canary design.md §2.5)."
                )
        super().save(*args, **kwargs)


class EmailMessage(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    relay_message_id = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        help_text="Se genera automáticamente al enviar el correo"
    )
    subject = models.CharField(
        max_length=255,
        verbose_name="Asunto",
        help_text="Asunto del correo"
    )
    from_email = models.EmailField(
        verbose_name="Remitente",
        help_text="Correo del remitente"
    )
    to_emails = models.TextField(
        verbose_name="Destinatarios",
        help_text="Lista de correos separados por coma"
    )
    html = models.TextField(
        blank=True,
        null=True,
        verbose_name="Contenido HTML",
        help_text="Contenido del correo en formato HTML"
    )
    text = models.TextField(
        blank=True,
        null=True,
        verbose_name="Contenido texto plano",
        help_text="Versión en texto plano del correo"
    )
    status = models.CharField(max_length=64, default="created")
    location = models.URLField(blank=True, null=True)
    meta = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.id} - {self.subject}"

    class Meta:
        verbose_name = "Mensaje de correo"
        verbose_name_plural = "Mensajes de correo"


class Delivery(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    relay_delivery_id = models.CharField(max_length=64, unique=True)
    message = models.ForeignKey(
        EmailMessage, on_delete=models.SET_NULL, null=True, blank=True)
    email = models.EmailField()
    status = models.CharField(max_length=64)
    reason = models.CharField(max_length=255, blank=True, null=True)
    ts = models.DateTimeField()
    raw = models.JSONField(default=dict)


class Event(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(max_length=64)
    email = models.EmailField()
    ts = models.DateTimeField()
    message_id = models.CharField(
        max_length=64, blank=True, null=True, db_index=True)
    raw = models.JSONField(default=dict)

    class Meta:
        indexes = [
            models.Index(fields=["kind", "ts"]),
            models.Index(fields=["email"]),
        ]


class QuotaWindow(models.Model):
    """bulk-v2 quota guard (design round 5, PR A): a single local, atomic
    reservation counter per UTC window. Dormant infrastructure — nothing
    outside relay/services/bulk_quota.py may reference this model, and
    that module has no caller yet (PR B wires it into the V2 send path).

    `limit_value` is resolved once, at row-creation time, from
    `_resolve_effective_limit()` and then never rewritten — a later
    config change only affects windows created after the change, never
    an already-created row (design round 5, point 5).
    """

    WINDOW_MONTH = "month"
    WINDOW_DAY = "day"
    WINDOW_HOUR = "hour"
    WINDOW_TYPE_CHOICES = (
        (WINDOW_MONTH, "Month"),
        (WINDOW_DAY, "Day"),
        (WINDOW_HOUR, "Hour"),
    )

    window_type = models.CharField(max_length=8, choices=WINDOW_TYPE_CHOICES)
    window_start = models.DateTimeField()
    limit_value = models.PositiveIntegerField()
    consumed = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Ventana de cuota"
        verbose_name_plural = "Ventanas de cuota"
        constraints = [
            models.UniqueConstraint(
                fields=["window_type", "window_start"],
                name="uniq_quota_window",
            ),
            models.CheckConstraint(
                condition=models.Q(consumed__lte=models.F("limit_value")),
                name="quota_window_consumed_lte_limit",
            ),
            models.CheckConstraint(
                condition=models.Q(window_type__in=["month", "day", "hour"]),
                name="quota_window_valid_type",
            ),
        ]

    def __str__(self):
        return f"{self.window_type} {self.window_start.isoformat()} [{self.consumed}/{self.limit_value}]"
