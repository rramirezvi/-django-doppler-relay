# Flujo de Envío y Reportería

Este documento resume el flujo actual de punta a punta para Bulk Send (normal y por remitente), tanto manual como programado, y cómo se dispara la reportería post‑envío.

## Envío manual (inmediato)
- Guardar un BulkSend NO dispara el envío. Queda en `pending`.
- En el listado del admin, selecciona el/los registros y usa la acción: “Procesar envío masivo seleccionado”.
- El proceso corre en background (no bloquea el admin) y actualiza `status` a `done` o `error`, con `result` y `log`.
- En “por remitente” se respeta el remitente elegido (guardado como `__sender_user_config_id`).

## Envío programado
- Completa `scheduled_at` con fecha/hora futura y guarda.
- El scheduler (timer opcional) toma los vencidos y llama internamente `process_bulk_id(...)`.
- Comando manual de prueba: `python manage.py process_bulk_scheduled`.

## Reportería post‑envío (desacoplada)
- No se genera “en vivo” durante el envío.
- Un job horario crea/carga la reportería del día del envío para los BulkSend en `done` con ≥ 1 hora de antigüedad.
- Comando: `python manage.py process_post_send_reports`.
  - Crea `GeneratedReport` por tipo si faltan (deliveries, bounces, opens, clicks, spam, unsubscribed, sent).
  - Ejecuta `process_reports_pending` y descarga los CSV de Doppler Relay.
  - Carga tipada a BD local (`load_report_to_db(..., target_alias="default")`).
  - Marca el envío con `post_reports_status='done'` y `post_reports_loaded_at`.

## Botón “Ver reporte” (ambos admins)
- Condición de visibilidad: `status == 'done'` y `post_reports_loaded_at` no nulo.
- Muestra resumen local por tipo para el día del envío consultando tablas `reports_*` (sin API en vivo).

## Comandos útiles
- `python manage.py process_bulk_scheduled`  → toma envíos programados vencidos.
- `python manage.py process_post_send_reports` → crea/carga reportería del día para envíos `done` (≥ 1h).
- `python manage.py process_reports_pending` → procesa `GeneratedReport` en `PENDING/PROCESSING`.

## Timers (opcional en producción)
- Scheduler de envíos: servicio/timer `bulk-scheduler` (cada pocos minutos).
- Post‑envío: servicio/timer `post-send-reports` (cada 60 min).
- Detalle de archivos systemd y comandos: ver `DEPLOY.md` (secciones 14 y 15).

