import logging
from types import SimpleNamespace
from typing import Any
import threading
import time

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.core.cache import cache
from django.http import HttpResponse
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import EmailMessage, BulkSend, Attachment, UserEmailConfig
from .services.doppler_relay import DopplerRelayClient, DopplerRelayError


logger = logging.getLogger(__name__)


# Formulario para la configuración de email del usuario


class UserEmailConfigForm(forms.ModelForm):
    class Meta:
        model = UserEmailConfig
        fields = ['user', 'from_email', 'from_name', 'is_active']

# Formulario personalizado para EmailMessage


class EmailMessageForm(forms.ModelForm):
    class Meta:
        model = EmailMessage
        fields = ('subject', 'from_email', 'to_emails', 'html', 'text')
        widgets = {
            'to_emails': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Varios correos separados por coma'}),
            'html': forms.Textarea(attrs={'rows': 10}),
            'text': forms.Textarea(attrs={'rows': 10}),
        }

    def __init__(self, *args, **kwargs):
        # Extraer el request para obtener el usuario
        self.request = kwargs.pop('request', None)
        super().__init__(*args, **kwargs)

        # Usar el email del usuario logueado como remitente
        if not self.instance.pk and not self.fields['from_email'].initial:
            if self.request and self.request.user.is_authenticated:
                # Prioridad 1: Configuración personalizada del usuario (si existe)
                user_config = UserEmailConfig.get_user_email_config(
                    self.request.user)
                if user_config:
                    self.fields['from_email'].initial = user_config.from_email
                # Prioridad 2: Email del usuario de Django
                elif self.request.user.email:
                    self.fields['from_email'].initial = self.request.user.email
                # Prioridad 3: Fallback al valor del .env
                else:
                    self.fields['from_email'].initial = settings.DOPPLER_RELAY.get(
                        'DEFAULT_FROM_EMAIL', '')
            else:
                # Fallback al valor por defecto si no hay usuario logueado
                self.fields['from_email'].initial = settings.DOPPLER_RELAY.get(
                    'DEFAULT_FROM_EMAIL', '')


@admin.register(EmailMessage)
class EmailMessageAdmin(admin.ModelAdmin):
    form = EmailMessageForm
    list_display = ("id", "subject", "from_email",
                    "to_emails", "status", "created_at")
    search_fields = ("subject", "from_email", "to_emails", "relay_message_id")
    list_filter = ("status",)
    actions = ['send_email']

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # Crear una nueva clase de formulario que incluya el request

        class FormWithRequest(form):
            def __init__(self, *args, **kwargs):
                kwargs['request'] = request
                super().__init__(*args, **kwargs)
        return FormWithRequest

    def send_email(self, request, queryset):
        from django.utils import timezone
        client = DopplerRelayClient()
        success = 0
        errors = 0

        for email in queryset:
            if email.status == 'created':
                try:
                    # Preparar los destinatarios en el formato (email, nombre)
                    to_list = [(email.strip(), None)
                               for email in email.to_emails.split(',')]

                    # Verificar configuración
                    if not hasattr(settings, 'DOPPLER_RELAY'):
                        raise ValueError(
                            "DOPPLER_RELAY no está configurado en settings.py")

                    # Obtener account_id de la configuración o usar valor por defecto
                    account_id = getattr(
                        settings, 'DOPPLER_RELAY', {}).get('ACCOUNT_ID', 1)

                    # Intentar enviar el mensaje
                    response = client.send_message(
                        account_id=account_id,
                        from_email=email.from_email,
                        subject=email.subject,
                        html=email.html,
                        text=email.text,
                        to=to_list
                    )

                    if response and response.get('messageId'):
                        email.relay_message_id = response['messageId']
                        email.status = 'sent'
                        email.save()
                        success += 1
                        self.message_user(
                            request,
                            f"Email {email.id} enviado exitosamente (Message ID: {response['messageId']})"
                        )
                    else:
                        raise ValueError(
                            f"Respuesta inesperada de la API: {response}")

                except Exception as e:
                    errors += 1
                    error_message = str(e)
                    if hasattr(e, 'payload'):
                        error_message += f"\nDetalles: {e.payload}"

                    self.message_user(
                        request,
                        f"Error enviando email {email.id} a {email.to_emails}: {error_message}",
                        level='ERROR'
                    )

                    # Guardar el error en los metadatos del correo
                    meta = email.meta or {}
                    meta.update({
                        'last_error': error_message,
                        'error_timestamp': timezone.now().isoformat()
                    })
                    email.meta = meta
                    email.save()
            else:
                errors += 1
                self.message_user(
                    request, f"El email {email.id} no está en estado 'created'", level='WARNING')

        if success:
            self.message_user(
                request, f"{success} email(s) enviado(s) exitosamente.")
        if errors:
            self.message_user(
                request, f"{errors} email(s) no pudieron ser enviados.", level='WARNING')

    send_email.short_description = "Enviar emails seleccionados"

    def get_readonly_fields(self, request, obj=None):
        # Si es un objeto existente, todos los campos son readonly excepto el contenido
        if obj:
            return ["relay_message_id", "subject", "from_email", "to_emails",
                    "status", "location", "created_at", "updated_at", "meta"]
        return ["relay_message_id", "status", "location", "created_at", "updated_at", "meta"]

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            # Para nuevo correo, mostrar solo los campos necesarios
            return (
                ('Nuevo Correo', {
                    'fields': ('subject', 'from_email', 'to_emails', 'html', 'text'),
                    'description': 'Ingresa los detalles del correo a enviar'
                }),
            )
        else:
            # Para correo existente, mostrar todos los campos
            return (
                ('Información básica', {
                    'fields': ('subject', 'from_email', 'to_emails')
                }),
                ('Contenido', {
                    'fields': ('html', 'text'),
                    'description': 'Contenido del correo'
                }),
                ('Estado y metadatos', {
                    'fields': ('status', 'relay_message_id', 'location', 'meta'),
                    'classes': ('collapse',),
                    'description': 'Información generada automáticamente'
                }),
                ('Fechas', {
                    'fields': ('created_at', 'updated_at'),
                    'classes': ('collapse',),
                }),
            )


@admin.register(UserEmailConfig)
class UserEmailConfigAdmin(admin.ModelAdmin):
    form = UserEmailConfigForm
    list_display = ('user', 'from_email', 'from_name',
                    'is_active', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('user__username', 'from_email', 'from_name')
    autocomplete_fields = ['user']

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj is None:  # Solo para objetos nuevos
            form.base_fields['user'].initial = request.user
        return form

    def save_model(self, request, obj, form, change):
        # Al activar una configuración, desactivar otras configuraciones del mismo usuario
        if obj.is_active:
            UserEmailConfig.objects.filter(user=obj.user).exclude(
                id=obj.id).update(is_active=False)
        super().save_model(request, obj, form, change)

# Formulario personalizado para BulkSend


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'content_type', 'created_at', 'file_link')
    search_fields = ('name', 'content_type')
    readonly_fields = ('created_at',)

    def file_link(self, obj):
        if obj.file:
            return format_html('<a href="{}" target="_blank">Descargar</a>', obj.file.url)
        return '-'
    file_link.short_description = 'Archivo'


class BulkSendForm(forms.ModelForm):
    TEMPLATE_CACHE_PREFIX = "relay:templates"
    TEMPLATE_CACHE_FRESH_SECONDS = 300
    TEMPLATE_CACHE_TIMEOUT = 600
    TEMPLATE_CACHE_LOCK_SECONDS = 45
    TEMPLATE_MAX_CHOICES = 200

    template_id = forms.CharField(
        max_length=128,
        help_text="ID de la plantilla en Doppler Relay"
    )
    subject = forms.CharField(
        max_length=255,
        required=False,
        help_text="Asunto del correo (opcional, se puede usar el de la plantilla)"
    )
    recipients_file = forms.FileField(
        help_text="Archivo CSV con los destinatarios. Debe tener al menos una columna 'email'",
        widget=forms.ClearableFileInput(attrs={'accept': '.csv,text/csv'})
    )
    scheduled_at = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        help_text="Déjalo vacío para enviar ahora. Si especificas fecha/hora futura, se programará automáticamente.",
        label="Programar envío"
    )
    variables = forms.CharField(
        widget=forms.Textarea,
        required=False,
        help_text="""Mapeo de columnas CSV a variables de la plantilla (opcional).
        Solo es necesario si los nombres de las columnas en tu CSV no coinciden con las variables de la plantilla.

        Ejemplo: Si tu plantilla usa {{nombre}} y {{monto}} pero tu CSV tiene las columnas "nombres_completos" y "valor_deuda":
        {
            "nombre": "nombres_completos",
            "monto": "valor_deuda"
        }

        Si los nombres de las columnas en tu CSV coinciden con las variables de la plantilla, deja este campo vacío."""
    )

    class Meta:
        model = BulkSend
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)
        self._template_warnings: set[str] = set()
        self._configure_template_field()

    def clean_variables(self):
        data = self.cleaned_data["variables"]
        import json
        if not data:
            return {}
        try:
            return json.loads(data)
        except Exception:
            raise forms.ValidationError(
                "El campo variables debe ser JSON válido.")

    def clean_attachments(self):
        data = self.cleaned_data.get("attachments")
        if not data:
            return []
        return data

    def clean_template_id(self):
        value = self.cleaned_data.get("template_id", "")
        if value is None:
            return None
        return str(value).strip()

    def _configure_template_field(self) -> None:
        template_field = self.fields['template_id']
        template_field.required = True
        template_field.widget.attrs.setdefault('placeholder', 'Ingresa el ID de la plantilla')

        choices = self._fetch_template_choices()
        if not choices:
            return

        choices = sorted(choices, key=lambda item: item[1].lower())
        if len(choices) > self.TEMPLATE_MAX_CHOICES:
            choices = choices[:self.TEMPLATE_MAX_CHOICES]
            self._warn("Se muestran solo 200 plantillas. Escribe el ID manual si no aparece.")

        initial_value = (
            self.initial.get('template_id')
            or getattr(self.instance, 'template_id', '')
            or ''
        )

        select_choices = [('', '— Selecciona una plantilla —')] + choices
        if initial_value and not any(value == str(initial_value) for value, _ in select_choices):
            select_choices.append((str(initial_value), f"{initial_value} (actual)"))

        field = forms.ChoiceField(
            label=template_field.label,
            help_text=template_field.help_text,
            required=True,
            choices=select_choices,
        )
        field.widget.attrs.setdefault('required', 'required')
        if initial_value:
            field.initial = str(initial_value)
        self.fields['template_id'] = field

    def _fetch_template_choices(self) -> list[tuple[str, str]]:
        account_id = self._resolve_account_id()
        if not account_id:
            self._warn('No se pudo determinar la cuenta de Doppler Relay. Ingresa el ID manualmente.')
            return []

        cache_key = self._cache_key(account_id)
        cache_entry = cache.get(cache_key) if cache_key else None
        now = time.time()
        choices: list[tuple[str, str]] = []
        stale = False

        if isinstance(cache_entry, dict):
            choices = cache_entry.get('choices') or []
            fetched_at = cache_entry.get('fetched_at') or 0.0
            age = now - fetched_at
            stale = age > self.TEMPLATE_CACHE_FRESH_SECONDS
            logger.info(
                'template list cache hit',
                extra={'account': account_id, 'age': round(age, 2), 'items': len(choices)},
            )
        else:
            logger.info('template list cache miss', extra={'account': account_id})

        if choices and stale:
            self._schedule_refresh(account_id, cache_key)

        if not choices:
            choices = self._refresh_templates_cache(account_id, cache_key)

        return choices

    def _cache_key(self, account_id) -> str:
        return f"{self.TEMPLATE_CACHE_PREFIX}:{account_id}"

    def _schedule_refresh(self, account_id, cache_key: str) -> None:
        lock_key = f"{self.TEMPLATE_CACHE_PREFIX}:refresh:{account_id}"
        if not cache.add(lock_key, True, self.TEMPLATE_CACHE_LOCK_SECONDS):
            return

        def worker():
            try:
                self._refresh_templates_cache(account_id, cache_key, suppress_messages=True)
            finally:
                cache.delete(lock_key)

        try:
            threading.Thread(target=worker, daemon=True).start()
        except RuntimeError:
            worker()

    def _refresh_templates_cache(self, account_id, cache_key: str | None, *, suppress_messages: bool = False) -> list[tuple[str, str]]:
        if cache_key is None:
            return []
        try:
            choices = self._load_templates_from_api(account_id)
        except DopplerRelayError as exc:
            logger.warning(
                'No se pudieron cargar las plantillas (API error)',
                extra={'account': account_id, 'error': str(exc)},
            )
            if not suppress_messages:
                self._warn('No se pudieron cargar las plantillas de Doppler Relay. Ingresa el ID manualmente.')
            return []
        except Exception as exc:
            logger.exception('Fallo inesperado cargando plantillas', extra={'account': account_id})
            if not suppress_messages:
                self._warn('No se pudieron cargar las plantillas de Doppler Relay. Ingresa el ID manualmente.')
            return []

        cache.set(
            cache_key,
            {'choices': choices, 'fetched_at': time.time()},
            self.TEMPLATE_CACHE_TIMEOUT,
        )
        return choices

    def _load_templates_from_api(self, account_id) -> list[tuple[str, str]]:
        start = time.perf_counter()
        client = DopplerRelayClient()
        data = client.list_templates(account_id)
        latency_ms = (time.perf_counter() - start) * 1000
        choices = self._normalize_template_items(data)
        logger.info(
            'template list fetch',
            extra={'account': account_id, 'latency_ms': round(latency_ms, 2), 'items': len(choices)},
        )
        return choices

    def _normalize_template_items(self, payload: Any) -> list[tuple[str, str]]:
        items: list[dict[str, Any]] = []
        if isinstance(payload, list):
            items = [item for item in payload if isinstance(item, dict)]
        elif isinstance(payload, dict):
            for key in ('items', 'templates', 'data'):
                value = payload.get(key)
                if isinstance(value, list):
                    items = [item for item in value if isinstance(item, dict)]
                    break
                if isinstance(value, dict) and isinstance(value.get('items'), list):
                    items = [item for item in value['items'] if isinstance(item, dict)]
                    break
            else:
                if isinstance(payload.get('id'), (str, int)):
                    items = [payload]

        choices: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in items:
            tpl_id = item.get('id') or item.get('templateId') or item.get('template_id')
            name = item.get('name') or tpl_id
            if not tpl_id:
                continue
            tpl_id_str = str(tpl_id).strip()
            if not tpl_id_str or tpl_id_str in seen:
                continue
            seen.add(tpl_id_str)
            display_name = str(name).strip() if isinstance(name, str) else tpl_id_str
            label = f"{display_name} (id={tpl_id_str})"
            choices.append((tpl_id_str, label))
        return choices

    def _resolve_account_id(self):
        cfg = getattr(settings, 'DOPPLER_RELAY', {}) or {}
        account_id = cfg.get('ACCOUNT_ID') or getattr(settings, 'DOPPLER_RELAY_ACCOUNT_ID', None)
        if not account_id:
            return None
        try:
            return int(account_id)
        except (TypeError, ValueError):
            return account_id

    def _warn(self, message: str) -> None:
        warnings = getattr(self, '_template_warnings', set())
        if message in warnings:
            return
        warnings.add(message)
        self._template_warnings = warnings
        if self.request:
            messages.warning(self.request, message)

# Admin para BulkSend


@admin.register(BulkSend)
class BulkSendAdmin(admin.ModelAdmin):
    form = BulkSendForm
    list_display = ("id", "template_id", "created_at", "scheduled_at",
                    "status", "attachment_count")
    readonly_fields = ("result", "log", "status", "created_at")
    search_fields = ("template_id", "subject")
    list_filter = ("status", "scheduled_at")
    filter_horizontal = ('attachments',)  # Para selección múltiple de adjuntos

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)

        class FormWithRequest(form):
            def __init__(self, *args, **kw):
                kw['request'] = request
                super().__init__(*args, **kw)

        return FormWithRequest

    def attachment_count(self, obj):
        return obj.attachments.count()
    attachment_count.short_description = 'Adjuntos'

    actions = ["procesar_envio_masivo"]

    def procesar_envio_masivo(self, request, queryset):
        import csv
        import io
        import json
        import base64
        from .views import process_bulk_template_send
        for bulk in queryset:
            if bulk.status != "pending":
                messages.warning(request, f"BulkSend {bulk.id} ya procesado.")
                continue
            recipients = []
            try:
                # Obtener información de la plantilla para validar variables
                client = DopplerRelayClient()
                ACCOUNT_ID = settings.DOPPLER_RELAY["ACCOUNT_ID"]
                template_info = client.get_template_fields(
                    ACCOUNT_ID, bulk.template_id)
                required_vars = set(template_info["variables"])

                # Abrir el archivo de destinatarios en modo binario y decodificar
                with bulk.recipients_file.open('rb') as f:
                    content = f.read().decode('utf-8-sig')
                    print("\n=== DEBUG CSV ===")
                    print(f"Contenido del CSV:\n{content[:500]}...")

                    reader = csv.DictReader(
                        io.StringIO(content), delimiter=';')
                    print(
                        f"\nNombres de columnas sin procesar: {reader.fieldnames}")

                    headers = [h.strip().lower() for h in reader.fieldnames]
                    print(f"Nombres de columnas procesados: {headers}")

                    # Buscar columna de email con variaciones comunes
                    email_column_variants = [
                        'email', 'correo', 'e-mail', 'mail', 'email_address', 'correo_electronico']
                    email_column = None

                    for variant in email_column_variants:
                        if variant in headers:
                            email_column = variant
                            break

                    if not email_column:
                        raise ValueError(
                            f"El archivo CSV debe tener una columna para el correo electrónico. "
                            f"Nombres válidos: {', '.join(email_column_variants)}. "
                            f"Columnas encontradas: {', '.join(headers)}")

                    # Mostrar las variables disponibles en el CSV
                    available_vars = [h for h in headers if h != 'email']
                    bulk.log = f"Variables disponibles en CSV: {', '.join(available_vars)}\n"
                    bulk.log += f"Variables requeridas por la plantilla: {', '.join(required_vars)}\n"

                    # Obtener el mapeo de variables (si existe)
                    try:
                        variables_mapping = json.loads(
                            bulk.variables) if bulk.variables else {}
                        if variables_mapping:
                            bulk.log += f"Usando mapeo personalizado: {json.dumps(variables_mapping, indent=2)}\n"
                        else:
                            bulk.log += "Usando nombres de columnas directamente como variables\n"
                    except json.JSONDecodeError:
                        raise ValueError(
                            "El mapeo de variables no es un JSON válido")
                    if 'email' not in headers:
                        raise ValueError(
                            "El archivo CSV debe tener una columna 'email'")

                    for row in reader:
                        clean_row = {k.strip().lower(): (v.strip() if v else v)
                                     for k, v in row.items()}

                        email_value = clean_row.get("email")
                        if not email_value:
                            continue

                        # Construir variables según el mapeo o usar todas las columnas
                        if variables_mapping:
                            # Usar el mapeo personalizado
                            variables = {
                                template_var: clean_row.get(csv_col.lower())
                                for template_var, csv_col in variables_mapping.items()
                            }
                        else:
                            # Si no hay mapeo, usar los nombres de columnas directamente
                            variables = {
                                k: v for k, v in clean_row.items()
                                if k != 'email' and v
                            }

                        # Registrar las variables que se usarán para este destinatario
                        # Solo para el primer destinatario
                        if email_value == clean_row.get('email'):
                            bulk.log += f"\nEjemplo de variables para {email_value}:\n"
                            bulk.log += json.dumps(variables, indent=2) + "\n"

                        # Validar variables requeridas
                        missing_vars = required_vars - set(variables.keys())
                        if missing_vars:
                            raise ValueError(
                                f"Faltan variables requeridas para {email_value}: {', '.join(missing_vars)}"
                            )
                        recipients.append({
                            "email": email_value,
                            "name": clean_row.get("nombres", ""),
                            "variables": variables
                        })
            except Exception as e:
                import traceback
                print("EXCEPCIÓN:", str(e))
                print("ARGS:", getattr(e, 'args', None))
                print("CAUSE:", getattr(e, '__cause__', None))
                print("TRACEBACK:\n", traceback.format_exc())
                bulk.result = json.dumps({
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                    "recipients": recipients,
                    "template_id": bulk.template_id
                })
                bulk.log = f"Error leyendo archivo: {e}"
                bulk.status = "error"
                bulk.save()
                continue
            # Convertir adjuntos al formato de Doppler
            adj_list = [
                attachment.to_doppler_format()
                for attachment in bulk.attachments.all()
            ]
            # Subject
            subject = bulk.subject
            try:
                response = process_bulk_template_send(
                    template_id=bulk.template_id,
                    recipients=recipients,
                    subject=subject,
                    adj_list=adj_list,
                    user=request.user  # ¡ESTO FALTABA!
                )
                bulk.result = response.content.decode(
                    "utf-8") if hasattr(response, 'content') else json.dumps(response)
                bulk.status = "done"
                bulk.log = "Envío realizado"
            except Exception as e:
                import traceback
                print("EXCEPCIÓN:", str(e))
                print("ARGS:", getattr(e, 'args', None))
                print("CAUSE:", getattr(e, '__cause__', None))
                print("TRACEBACK:\n", traceback.format_exc())
                # Si la función retornó un response, guárdalo aunque sea error
                api_error = None
                if hasattr(e, 'payload'):
                    api_error = getattr(e, 'payload', None)
                if 'response' in locals():
                    bulk.result = response.content.decode(
                        "utf-8") if hasattr(response, 'content') else json.dumps(response)
                else:
                    bulk.result = json.dumps({
                        "error": str(e),
                        "traceback": traceback.format_exc(),
                        "recipients": recipients,
                        "subject": subject,
                        "attachments": adj_list,
                        "template_id": bulk.template_id,
                        "api_error": api_error
                    })
                bulk.status = "error"
                bulk.log = f"Error en envío: {e}"
            bulk.save()
            messages.info(request, f"BulkSend {bulk.id} procesado.")

    procesar_envio_masivo.short_description = "Procesar envío masivo seleccionado"

