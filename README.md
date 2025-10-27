
# Django Doppler Relay (ready-to-run, SQLite)

Aplicación Django mínima lista para integrarse con Doppler Relay y enfocada en envíos masivos por plantilla.

## Características
- Envío de correos con plantillas de Doppler Relay y soporte para variables por destinatario.
- Flujo único para JSON o CSV que normaliza destinatarios, variables y adjuntos.
- Selección de remitente con prioridad: datos del request > configuración por usuario > defaults en settings.
- Registro local de mensajes, adjuntos, lotes y eventos para auditoría.
- Panel de administración con acciones para reprocesar lotes y enviar mensajes manualmente.
- Reportería desde el admin (Reportes Doppler Relay) con descarga CSV directa (flujo reportrequest: POST + polling + CSV).
- “Bulk Sends (por remitente)” [solo superusuarios]: mismas funciones de BulkSend con campo para elegir remitente desde `UserEmailConfig`.

## Reglas funcionales actuales
- `template_id` es obligatorio en cualquier envío.
- Cada destinatario necesita un email válido; se rechazan vacíos o con formato inválido.
- Las variables se aceptan en `variables` o `substitution_data` y se convierten a string (sin `None`).
- Los adjuntos deben venir en base64; si no lo están se codifican antes de llamar a la API.
- La vista `send_bulk_email` envía cada destinatario de forma individual y persiste un `EmailMessage` por éxito.
- Para CSV se requiere al menos una columna `email` (por defecto `email_column=email`).
- `UserEmailConfig` garantiza que solo una configuración por usuario esté activa; al activar una nueva las demás se desactivan.
- Las entregas y eventos se consultan vía API de Reports (flujo reportrequest) o scripts de polling.

## Requisitos
- Python 3.10 o superior

## Puesta en marcha rápida
```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Linux/Mac
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env  # (Windows) / cp .env.example .env (Linux/Mac)
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## Variables de entorno clave
- `DEBUG`: activa modo debug (usar False en producción).
- `SECRET_KEY`: clave Django.
- `ALLOWED_HOSTS`: lista separada por comas para hosts permitidos.
- `DOPPLER_RELAY_API_KEY`: API key válida.
- `DOPPLER_RELAY_ACCOUNT_ID`: ID numérico de la cuenta (para envíos simples).
- `DOPPLER_RELAY_AUTH_SCHEME`: esquema de autorización (`Bearer`, `token`, etc.).
- `DOPPLER_RELAY_BASE_URL`: raíz de la API (por defecto `https://api.dopplerrelay.com`).
- `DOPPLER_RELAY_FROM_EMAIL` y `DOPPLER_RELAY_FROM_NAME`: remitente por defecto como último fallback.

## API HTTP
### POST `/relay/send/`
- Acepta `application/json` o `multipart/form-data` con `csv_file`.
- Campos comunes: `template_id` (obligatorio), `subject`, `from_email`, `from_name`, `attachments`.
- JSON: `to` o `recipients` puede ser una lista de emails (string) o diccionarios con `email`, `variables`.
- CSV: subir archivo en `csv_file`; opcionalmente `email_column` si el encabezado difiere.

Ejemplo JSON:
```json
{
  "template_id": "TPL-123",
  "subject": "Estado de cuenta",
  "from_email": "notificaciones@midominio.com",
  "from_name": "Equipo Cobranza",
  "to": [
    {
      "email": "cliente@example.com",
      "variables": {
        "identificacion": "0922334455",
        "nombres": "Maria Perez",
        "deuda": "1200",
        "total_a_pagar": "950"
      }
    }
  ],
  "attachments": [
    {
      "name": "detalle.pdf",
      "content": "<base64>"
    }
  ]
}
```

Respuesta típica:
```json
{
  "ok": true,
  "resultados": [
    {
      "email": "cliente@example.com",
      "status": "ok",
      "message_id": "abc123",
      "variables": {
        "identificacion": "0922334455",
        "nombres": "Maria Perez",
        "deuda": "1200",
        "total_a_pagar": "950"
      }
    }
  ],
  "total_enviados": 1,
  "total_errores": 0
}
```

### GET `/relay/user/email-config/`
- Retorna la configuración activa del remitente para el usuario autenticado o los valores por defecto.

### POST `/relay/user/email-config/update/`
- Actualiza `from_email` y `from_name` para el usuario autenticado.
- Campos requeridos: `from_email` (validado con regex). `from_name` es opcional.

## Panel de administración
- Modelos registrados: `EmailMessage`, `BulkSend`, `Attachment`, `UserEmailConfig`.
- BulkSend:
  - Selector de plantilla con caché SWR y circuito de fallos (fallback a campo manual).
  - `recipients_file` exige CSV `;` y valida variables reales de la plantilla.
  - La acción “Procesar envío masivo” transforma adjuntos y usa `process_bulk_template_send`.
- Reportería: acceso “Reportes Doppler Relay” con descarga CSV (flujo `reportrequest`).
- Bulk Sends (por remitente) [solo superusuarios]: mismas funciones que BulkSend, con campo para elegir remitente (`UserEmailConfig`).

## Reportes y sincronización
## Reportes (app `reports`)
- Reporteria desacoplada del request web. La generacion y gestion de reportes vive en la app `reports`.
- Flujo basado en `GeneratedReport` con estados: `PENDING` -> `PROCESSING` -> `READY` -> `ERROR`.
- Creacion desde admin: "Reports > Solicitar reporte" (no bloquea). Los reportes aparecen en "Reports > Reportes generados".
- Procesamiento: ejecutar `python manage.py process_reports_pending` (o usar el boton "Procesar pendientes ahora" en el listado con permiso `reports.can_process_reports`).
- Descarga: cuando el estado es `READY`, aparece el enlace "Descargar CSV".
- Carga a base tipada: boton "Cargar BD (default|analytics)" que invoca `load_report_to_db(generated_report_id, target_alias)` y persiste en tablas `reports_<tipo>` con columnas tipadas (INTEGER/REAL/BOOLEAN/TIMESTAMP/TEXT). Soporta multiples conexiones (`default`, `analytics`).
- Trazabilidad en `GeneratedReport`: `rows_inserted`, `loaded_to_db`, `loaded_at`, `last_loaded_alias`.
- Evita doble carga por alias: si un reporte ya se cargo en un alias, el boton para ese alias no se muestra y la vista rechaza recargas.
- Logs y esquemas inferidos: `attachments/reports/schemas/` (archivos `schema_<tipo>.json`, `summary_all.txt`, `load_<id>.log`).

### Comandos utiles
- `python manage.py process_reports_pending` procesa `PENDING/PROCESSING` y descarga los CSV.
- `python manage.py inspect_reports_schema --days 1` solicita una muestra por tipo y genera `schema_*.json` con tipos inferidos.

### Permisos
- `reports.can_process_reports`: ver y usar "Procesar pendientes ahora".
- `reports.can_load_to_db`: ver y usar "Cargar BD (...)".

### Ejemplo de DATABASES con alias `analytics`
```
DATABASES = {
  "default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": BASE_DIR / "db.sqlite3",
  },
  "analytics": {
    "ENGINE": "django.db.backends.postgresql",
    "HOST": "db-analytics.local",
    "PORT": "5432",
    "NAME": "relay_analytics",
    "USER": "analytics_user",
    "PASSWORD": "********",
  }
}
```
- Usa la API de Reports (reportrequest) para consultar entregas, eventos y agregados.
- Scripts de polling o CLI pueden basarse en `relay/services/reports.py`.

## Comandos y scripts
- `python examples/send_sample.py`: ejemplo rápido de envío simple usando `send_message`.

## Pruebas
- Cobertura en `relay/tests/` (incluye pruebas para caché y fallback de plantillas). Ejecuta `python manage.py test relay.tests`.

## Seguridad
- Revisa `SECURITY.md` para políticas y buenas prácticas de credenciales, dependencias y datos sensibles.
