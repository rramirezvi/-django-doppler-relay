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

    def __str__(self):
        return f"BulkSend {self.id} - {self.template_id} ({self.created_at:%Y-%m-%d %H:%M})"


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
