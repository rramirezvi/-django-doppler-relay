# Flujo De Envio Y Reporteria

Este documento resume el flujo actual de punta a punta para Bulk Send, la UI operativa `/app/`, los workers y la reportería post-envio.

## Envio Desde La UI Operativa `/app/`

- El operador entra a "Nuevo envio", selecciona plantilla, remitente, CSV y fecha opcional.
- Si marca "Encolar envio inmediatamente", la UI crea:
  - Un `BulkSend`.
  - Un `BackgroundJob` tipo `bulk_send` en estado `queued`.
- El envio real no corre dentro del request web. Lo ejecuta el worker `doppler-background-jobs.service`.
- El worker corre:

```bash
python manage.py process_background_jobs --loop --sleep 3
```

- Cuando termina, el job pasa a `done` y el `BulkSend.status` queda en `done` o `error`.
- Si el worker no esta activo, el envio queda en `queued` hasta ejecutar manualmente:

```bash
python manage.py process_background_jobs --limit 10
```

## Envio Manual Desde Admin

- Guardar un BulkSend en admin no dispara el envio. Queda en `pending`.
- En el listado del admin, selecciona el registro y usa la accion "Procesar envio masivo seleccionado".
- El procesamiento actualiza `status` a `done` o `error`, con `result` y `log`.
- En "Bulk Sends (por remitente)" se respeta el remitente elegido desde `UserEmailConfig`.

## Envio Programado

- Completa `scheduled_at` con fecha/hora futura y guarda.
- El scheduler opcional toma los envios vencidos y llama internamente `process_bulk_id(...)`.
- Comando manual de prueba:

```bash
python manage.py process_bulk_scheduled
```

## Reporteria Post-Envio Automatica

- No se genera en vivo durante el envio.
- El timer `post-send-reports.timer` ejecuta `process_post_send_reports` cada hora aproximadamente.
- El comando procesa BulkSend en `done` con al menos 1 hora de antiguedad.
- Crea o reutiliza `GeneratedReport` por dia/tipo, descarga CSV desde Doppler Relay, carga datos a BD local y marca:
  - `post_reports_status='done'`
  - `post_reports_loaded_at`

Comando:

```bash
python manage.py process_post_send_reports
```

## Actualizar Reporte Desde La UI

- El boton "Actualizar reporte" no reenvia correos.
- Crea un `BackgroundJob` tipo `post_report`.
- Lo procesa `doppler-background-jobs.service`.
- La UI bloquea la actualizacion temprana por seguridad: por defecto se habilita despues de 15 minutos.
- El flujo reutiliza el reporte del mismo dia/tipo cuando corresponde para evitar duplicar archivos y registros.

## Descargar Reporte

- La descarga usa reportes ya generados localmente.
- No consulta Doppler en vivo al descargar.
- El boton se habilita cuando el reporte esta `ready`.

## Servicios En Produccion

Obligatorios para la UI operativa:

```bash
django.service
doppler-background-jobs.service
post-send-reports.timer
```

Responsabilidades:

- `django.service`: Gunicorn/Django, sirve `/admin/` y `/app/`.
- `doppler-background-jobs.service`: procesa jobs encolados por la UI, como envio masivo y actualizacion manual de reportes.
- `post-send-reports.timer`: dispara la reporteria automatica post-envio.

## Comandos Utiles

Ver jobs de la UI:

```bash
python manage.py shell -c "from relay.models import BackgroundJob; [print(j.id, j.job_type, j.state, j.attempts, j.message, j.created_at, j.started_at, j.finished_at) for j in BackgroundJob.objects.order_by('-id')[:10]]"
```

Procesar jobs manualmente:

```bash
python manage.py process_background_jobs --limit 10
```

Worker continuo:

```bash
python manage.py process_background_jobs --loop --sleep 3
```

Reporteria automatica manual:

```bash
python manage.py process_post_send_reports
```

Procesar reportes pendientes:

```bash
python manage.py process_reports_pending
```

Ver ultimo BulkSend:

```bash
python manage.py shell -c "from relay.models import BulkSend; b=BulkSend.objects.order_by('-id').first(); print('id=', b.id); print('status=', b.status); print('result=', b.result); print('log_tail=', (b.log or '')[-1000:])"
```

## Diagnostico Systemd

```bash
systemctl list-units --type=service --all | grep -Ei 'background|worker|jobs|doppler|relay|django'
ps aux | grep -Ei 'process_background_jobs|manage.py' | grep -v grep
systemctl status doppler-background-jobs.service --no-pager
systemctl status post-send-reports.timer --no-pager
systemctl list-timers --all | grep -Ei 'post-send|report'
```

Logs:

```bash
sudo journalctl -u django.service -n 100 --no-pager
sudo journalctl -u doppler-background-jobs.service -n 100 --no-pager
sudo journalctl -u post-send-reports.service -n 100 --no-pager
```

Detalle de deploy, archivos systemd y rollback: ver `DEPLOY.md`.
