# AGENTS

GuÃ­a rÃ¡pida para agentes humanos o automÃ¡ticos que trabajan en este repositorio.

## Flujo de envío
- Usar `POST /relay/send/` como punto de entrada principal.
- Normalizar la entrada a `recipients -> { email, variables }` antes de invocar `process_bulk_template_send`.
- Evitar lotes vacÃ­os: no llamar a la API si no hay destinatarios vÃ¡lidos.

## PriorizaciÃ³n de remitente
1. Respetar `from_email` y `from_name` recibidos en la solicitud cuando ambos existan.
2. Si falta alguno, consultar `UserEmailConfig` del usuario autenticado.
3. Como Ãºltimo recurso, usar `DOPPLER_RELAY_FROM_EMAIL` y `DOPPLER_RELAY_FROM_NAME` del settings.
4. Validar el formato de correo con `validate_email` antes de enviar.

## Validaciones obligatorias
- `template_id` no puede ser vacÃ­o.
- Cada destinatario debe incluir email vÃ¡lido; descartar filas sin correo.
- Convertir variables a `str` y eliminar valores `None`.
- Para CSV, asegurar columna `email`; opcionalmente respetar `email_column` del request.
- Adjuntos: incluir `name/filename` y `content` (base64). Codificar si se recibe contenido crudo.

## Adjuntos
- En vistas y admin usar `Attachment.to_doppler_format()` para transformar archivos almacenados.
- Validar base64 (`base64.b64decode`) antes de enviar.

## Persistencia y auditorÃ­a
- Registrar Ã©xitos creando `EmailMessage` con `relay_message_id`.
- Guardar errores en `resultados` (respuesta HTTP) y en `bulk.log`/`bulk.result` (admin).
- Mantener `BulkSend.status` en `pending|done|error` segÃºn el resultado real.

## ConfiguraciÃ³n y entorno
- Cargar variables desde `.env` con `envíon.Env`.
- Confirmar `DOPPLER_RELAY_API_KEY` (y `DOPPLER_RELAY_ACCOUNT_ID` para envío simple).

## Operación en admin
- BulkSend: CSV `;`, validaciÃ³n de variables Mustache, selector de plantillas con caché© SWR y fallback manual; adjuntos con selector de dos columnas.
- EmailMessage: acciÃ³n â€œEnviar emails seleccionadosâ€ para `status == 'created'`.
- ReporterÃ­a: flujo oficial `reportrequest` (POST + polling + CSV). Los errores se muestran vÃ­a `messages`.
- Bulk Sends (por remitente): mismas funciones que BulkSend con campo adicional para elegir remitente desde `UserEmailConfig` (se usa como `from_email`/`from_name`). Disponible para usuarios `is_staff` con permisos de modelo en `relay_super` (no requiere superusuario). El action de envío masivo exige permiso `change` del proxy.

## Observaciones
- Extender pruebas para flujos de envío y caché© de plantillas.
- Preferir `logging` sobre `print` fuera de desarrollo.

## Reporteria desacoplada (app `reports`)
- No bloquear requests web: la solicitud de reportes crea `GeneratedReport` en `PENDING` y retorna.
- Procesamiento fuera de request: `python manage.py process_reports_pending` (o desde admin con permiso `reports.can_process_reports`).
- Descarga historica: los CSV quedan en `attachments/reports/` y pueden descargarse desde el admin cuando el estado es `READY`.
- Carga tipada a BD: usar `load_report_to_db(generated_report_id, target_alias="default|analytics")`. Crea/ALTER tablas `reports_<tipo>` con tipos apropiados.
- Multi-conexion: el alias de destino se resuelve desde `settings.DATABASES` (ej. `analytics`).
- Trazabilidad en `GeneratedReport`: `rows_inserted`, `loaded_to_db`, `loaded_at`, `last_loaded_alias`.
- Doble carga: se evita por alias (no se permite recargar al mismo alias; si ya se cargo en `default` aun puede cargarse en `analytics`).
- Esquemas y logs: `attachments/reports/schemas/` contiene `schema_*.json`, `summary_all.txt` y `load_<id>.log`.

### Permisos
- `reports.can_process_reports`: ejecuta procesamiento desde admin.
- `reports.can_load_to_db`: permite ejecutar carga a BD desde admin.

### Admin
- "Reports > Solicitar reporte": formulario simple para crear `GeneratedReport`.
- "Reports > Reportes generados": listado con estados, descarga, boton "Procesar pendientes ahora", y botones de "Cargar BD (alias)". Muestra badge "Cargado en: <alias>" cuando `last_loaded_alias` existe.


## Permisos de `relay_super` (Bulk Send por remitente)
- Visibilidad del m�dulo: usuario `is_staff` con al menos uno de estos permisos del proxy `BulkSendUserConfigProxy` o permisos de m�dulo:
  - `relay_super.view_bulksenduserconfigproxy`
  - `relay_super.add_bulksenduserconfigproxy`
  - `relay_super.change_bulksenduserconfigproxy`
  - (opcional) `relay_super.delete_bulksenduserconfigproxy`
- Permisos efectivos:
  - Ver: `view_bulksenduserconfigproxy`
  - Crear: `add_bulksenduserconfigproxy`
  - Editar/Procesar (action): `change_bulksenduserconfigproxy`
  - Borrar: deshabilitado por defecto
