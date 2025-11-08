# Django Doppler Relay

Plataforma Django para envíos de email por plantilla con Doppler Relay, reportería desacoplada y soporte para cargas analíticas en múltiples conexiones de base de datos.

## Características
- Envío masivo por plantilla con variables por destinatario (CSV o JSON).
- Priorización de remitente: solicitud > configuración del usuario (`UserEmailConfig`) > defaults en settings.
- Adjuntos en base64 con validaciones y utilidades (`Attachment.to_doppler_format`).
- Admin para envíos “normales” y “por remitente” (elige `UserEmailConfig`).
- Reportería desacoplada (app `reports`): generación asincrónica, descarga histórica y carga tipada a BD local o analítica.
- Botón “Ver reporte” que consulta solo BD local (sin llamadas en vivo).

## Variables de entorno (desarrollo vs producción)
- Desarrollo:
  - `DEBUG=True`
  - `USE_SQLITE=1` (no requiere PostgreSQL ni `psycopg2` local)
- Producción:
  - `DEBUG=False`
  - `USE_SQLITE=0`
  - `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` apuntando al PostgreSQL de la VPS
  - `ALLOWED_HOSTS`: incluir dominio, IP pública, `localhost`, `127.0.0.1`
  - Instalar `psycopg2-binary` en el venv del servidor

Credenciales Doppler Relay (obligatorias para operar):
- `DOPPLER_RELAY_API_KEY`
- `DOPPLER_RELAY_ACCOUNT_ID`
- `DOPPLER_RELAY_BASE_URL` (default `https://api.dopplerrelay.com/`)
- `DOPPLER_RELAY_AUTH_SCHEME` (por ejemplo `Bearer`)
- `DOPPLER_RELAY_FROM_EMAIL`, `DOPPLER_RELAY_FROM_NAME` (fallback)

Parámetros de reportería (ajustables por settings/env):
- `DOPPLER_REPORTS_POLL_INITIAL_DELAY`, `DOPPLER_REPORTS_POLL_MAX_DELAY`, `DOPPLER_REPORTS_POLL_TOTAL_TIMEOUT`

## Flujo de envíos y reportería

En Bulk Send (normal y por remitente) el guardado NO dispara el envío. El registro queda en `pending`.

- Envío manual inmediato (desde admin):
  1) Guarda el BulkSend (estado `pending`).
  2) En el listado, selecciona y usa la acción “Procesar envío masivo seleccionado”.
  3) El procesamiento corre en background y actualiza `status` a `done` o `error`; `result` y `log` guardan el detalle.
  4) Reportería post‑envío: ejecuta `python manage.py process_post_send_reports` (o usa su timer horario). Crea/descarga reportes del día del envío y los carga tipados a la BD local.
  5) Cuando `post_reports_loaded_at` está seteado, aparece el botón “Ver reporte” que consulta solo la BD local.

- Envío programado (scheduled_at):
  1) Completa `scheduled_at` con fecha/hora futura y guarda.
  2) El scheduler toma envíos vencidos y llama internamente `process_bulk_id(...)` (timer opcional o `python manage.py process_bulk_scheduled`).
  3) La reportería post‑envío se carga con `python manage.py process_post_send_reports` (o su timer horario).

Condición del botón “Ver reporte”: `status == 'done'` y `post_reports_loaded_at` no nulo.

Comandos útiles (local):
- `python manage.py process_bulk_scheduled` → procesa envíos programados vencidos.
- `python manage.py process_post_send_reports` → crea/carga reportería del día para envíos `done` (≥ 1h).
- `python manage.py process_reports_pending` → procesa `GeneratedReport` en `PENDING/PROCESSING` (flujo general de reports).

## App `reports`
- Modelo `GeneratedReport` con estados `PENDING`, `PROCESSING`, `READY`, `ERROR`, `report_request_id`, `file_path`, `rows_inserted`, `loaded_to_db`, `loaded_at`, `last_loaded_alias`.
- Management commands:
  - `process_reports_pending`: genera/descarga CSVs y marca READY/ERROR.
  - `process_post_send_reports`: job de +1h post‑envío que crea/carga reportería del día para envíos `done`.
  - `inspect_reports_schema --days N`: infiere esquemas y tipos por `report_type` (guarda JSON en `attachments/reports/schemas/`).
- Carga tipada a BD (`load_report_to_db(id, target_alias="default|analytics")`), con creación/ALTER incremental de tablas `reports_<tipo>`.
- Previene doble carga por alias (no recarga al mismo alias dos veces).
- Admin “Reports”: solicitar, procesar pendientes, descargar CSV, cargar a BD; permisos `reports.can_process_reports`, `reports.can_load_to_db`.

## Admin
- BulkSend (normal) y Bulk Sends (por remitente):
  - CSV con al menos la columna `email` (tolerancia de delimitador en flujos principales), adjuntos opcionales.
  - En “por remitente”, se elige un `UserEmailConfig`; su id se persiste internamente y se respeta durante el envío.
  - Campos técnicos ocultos en alta y de solo lectura en edición (variables, post_reports_status, post_reports_loaded_at, etc.).
  - Columna “Plantilla” muestra `template_name` (fallback a `template_id`). Columna “Subject”. Botón “Ver reporte” según condición.

## Despliegue
- Guía operativa paso a paso en `DEPLOY.md` (Nginx + Gunicorn + PostgreSQL + systemd timers):
  - `bulk-scheduler.timer` → `process_bulk_scheduled` (cada pocos minutos).
  - `reports-process.timer` → `process_reports_pending` (cada 15 minutos, opcional).
  - `post-send-reports.timer` → `process_post_send_reports` (cada 60 minutos, opcional).

## Estructura de datos y logs
- Reportes históricos CSV en `attachments/reports/...`.
- Esquemas y logs de carga en `attachments/reports/schemas/`.

## Requisitos
- Python 3.10+
- (Prod) PostgreSQL local (o gestionado) con permisos sobre `public`.

## Notas
- La app funciona en desarrollo con SQLite (`USE_SQLITE=1`). La conexión `analytics` es opcional y se puede agregar luego como segunda base.

