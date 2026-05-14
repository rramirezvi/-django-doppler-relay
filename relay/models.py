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

    def save(self, *args, **kwargs):
        # Completar template_name de forma centralizada (best‑effort)
        try:
            if self.template_id and not self.template_name:
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
    TYPE_CHOICES = (
        (TYPE_BULK_SEND, "Bulk send"),
        (TYPE_POST_REPORT, "Post-send report"),
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
