# Django Doppler Relay

Plataforma Django para envÃ­os de email por plantilla con Doppler Relay, reporterÃ­a desacoplada y soporte para cargas analÃ­ticas en mÃºltiples conexiones de base de datos.

## CaracterÃ­sticas
- EnvÃ­o masivo por plantilla con variables por destinatario (CSV o JSON).
- PriorizaciÃ³n de remitente: solicitud > configuraciÃ³n del usuario (`UserEmailConfig`) > defaults en settings.
- Adjuntos en base64 con validaciones y utilidades (`Attachment.to_doppler_format`).
- Admin para envÃ­os â€œnormalesâ€ y â€œpor remitenteâ€ (elige `UserEmailConfig`).
- ReporterÃ­a desacoplada (app `reports`): generaciÃ³n asincrÃ³nica, descarga histÃ³rica y carga tipada a BD local o analÃ­tica.
- BotÃ³n â€œVer reporteâ€ que consulta solo BD local (sin llamadas en vivo).

## Variables de entorno (desarrollo vs producciÃ³n)
- Desarrollo:
  - `DEBUG=True`
  - `USE_SQLITE=1` (no requiere PostgreSQL ni `psycopg2` local)
- ProducciÃ³n:
  - `DEBUG=False`
  - `USE_SQLITE=0`
  - `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` apuntando al PostgreSQL de la VPS
  - `ALLOWED_HOSTS`: incluir dominio, IP pÃºblica, `localhost`, `127.0.0.1`
  - Instalar `psycopg2-binary` en el venv del servidor

Credenciales Doppler Relay (obligatorias para operar):
- `DOPPLER_RELAY_API_KEY`
- `DOPPLER_RELAY_ACCOUNT_ID`
- `DOPPLER_RELAY_BASE_URL` (default `https://api.dopplerrelay.com/`)
- `DOPPLER_RELAY_AUTH_SCHEME` (por ejemplo `Bearer`)
- `DOPPLER_RELAY_FROM_EMAIL`, `DOPPLER_RELAY_FROM_NAME` (fallback)

ParÃ¡metros de reporterÃ­a (ajustables por settings/env):
- `DOPPLER_REPORTS_POLL_INITIAL_DELAY`, `DOPPLER_REPORTS_POLL_MAX_DELAY`, `DOPPLER_REPORTS_POLL_TOTAL_TIMEOUT`

## Flujo de envÃ­os y reporterÃ­a

En Bulk Send (normal y por remitente) el guardado NO dispara el envÃ­o. El registro queda en `pending`.

- EnvÃ­o manual inmediato (desde admin):
  1) Guarda el BulkSend (estado `pending`).
  2) En el listado, selecciona y usa la acciÃ³n â€œProcesar envÃ­o masivo seleccionadoâ€.
  3) El procesamiento corre en background y actualiza `status` a `done` o `error`; `result` y `log` guardan el detalle.
  4) ReporterÃ­a postâ€‘envÃ­o: ejecuta `python manage.py process_post_send_reports` (o usa su timer horario). Crea/descarga reportes del dÃ­a del envÃ­o y los carga tipados a la BD local.
  5) Cuando `post_reports_loaded_at` estÃ¡ seteado, aparece el botÃ³n â€œVer reporteâ€ que consulta solo la BD local.

- EnvÃ­o programado (scheduled_at):
  1) Completa `scheduled_at` con fecha/hora futura y guarda.
  2) El scheduler toma envÃ­os vencidos y llama internamente `process_bulk_id(...)` (timer opcional o `python manage.py process_bulk_scheduled`).
  3) La reporterÃ­a postâ€‘envÃ­o se carga con `python manage.py process_post_send_reports` (o su timer horario).

CondiciÃ³n del botÃ³n â€œVer reporteâ€: `status == 'done'` y `post_reports_loaded_at` no nulo.

Comandos Ãºtiles (local):
- `python manage.py process_bulk_scheduled` â†’ procesa envÃ­os programados vencidos.
- `python manage.py process_post_send_reports` â†’ crea/carga reporterÃ­a del dÃ­a para envÃ­os `done` (â‰¥ 1h).
- `python manage.py process_reports_pending` â†’ procesa `GeneratedReport` en `PENDING/PROCESSING` (flujo general de reports).

## App `reports`
- Modelo `GeneratedReport` con estados `PENDING`, `PROCESSING`, `READY`, `ERROR`, `report_request_id`, `file_path`, `rows_inserted`, `loaded_to_db`, `loaded_at`, `last_loaded_alias`.
- Management commands:
  - `process_reports_pending`: genera/descarga CSVs y marca READY/ERROR.
  - `process_post_send_reports`: job de +1h postâ€‘envÃ­o que crea/carga reporterÃ­a del dÃ­a para envÃ­os `done`.
  - `inspect_reports_schema --days N`: infiere esquemas y tipos por `report_type` (guarda JSON en `attachments/reports/schemas/`).
- Carga tipada a BD (`load_report_to_db(id, target_alias="default|analytics")`), con creaciÃ³n/ALTER incremental de tablas `reports_<tipo>`.
- Previene doble carga por alias (no recarga al mismo alias dos veces).
- Admin â€œReportsâ€: solicitar, procesar pendientes, descargar CSV, cargar a BD; permisos `reports.can_process_reports`, `reports.can_load_to_db`.

## Admin
- BulkSend (normal) y Bulk Sends (por remitente):
  - CSV con al menos la columna `email` (tolerancia de delimitador en flujos principales), adjuntos opcionales.
  - En â€œpor remitenteâ€, se elige un `UserEmailConfig`; su id se persiste internamente y se respeta durante el envÃ­o.
  - Campos tÃ©cnicos ocultos en alta y de solo lectura en ediciÃ³n (variables, post_reports_status, post_reports_loaded_at, etc.).
  - Columna â€œPlantillaâ€ muestra `template_name` (fallback a `template_id`). Columna â€œSubjectâ€. BotÃ³n â€œVer reporteâ€ segÃºn condiciÃ³n.

## Despliegue
- GuÃ­a operativa paso a paso en `DEPLOY.md` (Nginx + Gunicorn + PostgreSQL + systemd timers):
  - `bulk-scheduler.timer` â†’ `process_bulk_scheduled` (cada pocos minutos).
  - `reports-process.timer` â†’ `process_reports_pending` (cada 15 minutos, opcional).
  - `post-send-reports.timer` â†’ `process_post_send_reports` (cada 60 minutos, opcional).

## Estructura de datos y logs
- Reportes histÃ³ricos CSV en `attachments/reports/...`.
- Esquemas y logs de carga en `attachments/reports/schemas/`.

## Requisitos
- Python 3.10+
- (Prod) PostgreSQL local (o gestionado) con permisos sobre `public`.

## Notas
- La app funciona en desarrollo con SQLite (`USE_SQLITE=1`). La conexión `analytics` es opcional y se puede agregar luego como segunda base.

## Actualización reportería v2 (resumen)

- Botón “Ver reporte (nuevo)”: es el único botón de reporte en Bulk Send (normal y por remitente). Se habilita cuando `status='done'` y `post_reports_loaded_at` tiene valor.
- Fuentes de datos: cuando la cuenta no expone endpoints de eventos, se usa el CSV “summary” de Doppler del día. El loader carga en la tabla `reports_deliveries` e incluye la columna `date_local` (hora local) para filtrar por ventana del envío.
- Ventana por envío: la vista v2 cuenta usando `[created_at, created_at + 24h)` en zona `America/Guayaquil` sobre `reports_deliveries.date_local`.
- Descargas desde v2:
  - “CSV de este envío”: exporta solo la ventana local del envío desde la BD.
  - “CSV consolidado del día”: descarga el último CSV READY del día si existe.
- Ocultar módulo Reports en el menú del admin: por defecto queda oculto. Para mostrarlo, definir `REPORTS_ADMIN_VISIBLE=1` en `.env` y reiniciar el servicio.
