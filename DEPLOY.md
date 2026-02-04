# Guía de Deploy en Producción (DigitalOcean)

Objetivo: provisionar una Droplet limpia y dejar el proyecto corriendo con Nginx + Gunicorn + PostgreSQL local + systemd timer opcional para reportería. Este documento es el flujo final que usamos en producción real.

Requisitos previos
- Dominio apuntando a la IP pública de la Droplet (A record)
- Llave SSH para acceso
- Credenciales/API de Doppler Relay
- (Opcional) Credenciales de la base analítica `analytics`

Tamaño recomendado
- Droplet Ubuntu LTS (24.04 o 22.04)
- 2 vCPU / 4 GB RAM / 80?160 GB SSD
- Si almacenarás muchos CSV: añade un Volume (100?250 GB) y móntalo en `attachments/`
- Base analítica: DO Managed PostgreSQL (opcional)

1) Acceso inicial, usuario no root y hardening
- Conéctate por SSH como `root`
- Crea usuario y dale sudo:
  ```bash
  adduser app
  usermod -aG sudo app
  ```
- Copia tu llave a `app`: `ssh-copy-id app@IP`
- Firewall básico:
  ```bash
  ufw allow OpenSSH
  ufw allow http
  ufw allow https
  ufw enable
  ufw status
  ```

2) Paquetes del sistema
```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3-pip python3-venv git nginx certbot python3-certbot-nginx postgresql postgresql-contrib
```

3) Ruta del proyecto (/opt/app/django-doppler-relay)
```bash
sudo mkdir -p /opt/app
sudo chown app:app /opt/app
cd /opt/app
git clone https://github.com/rramirezvi/-django-doppler-relay.git django-doppler-relay
cd /opt/app/django-doppler-relay
```

4) Dependencias Python y virtualenv (instalar gunicorn dentro del venv)
```bash
cd /opt/app/django-doppler-relay
python3 -m venv .venv
source .venv/bin/activate
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install gunicorn

/opt/app/django-doppler-relay/.venv/bin/gunicorn --version
```
Si `gunicorn` no está en esa ruta exacta, systemd fallará con `status=203/EXEC`.

5) Base de datos en producción (PostgreSQL local)
En producción NO usamos SQLite. Creamos PostgreSQL local y asignamos propietario y permisos al esquema `public` para evitar errores de migración.

```bash
sudo -u postgres psql

CREATE DATABASE doppler_prod;
CREATE USER doppler_user WITH PASSWORD 'poner_password_segura';
GRANT ALL PRIVILEGES ON DATABASE doppler_prod TO doppler_user;

-- Muy importante para que 'python manage.py migrate' funcione
ALTER DATABASE doppler_prod OWNER TO doppler_user;
\c doppler_prod;
ALTER SCHEMA public OWNER TO doppler_user;
GRANT ALL ON SCHEMA public TO doppler_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO doppler_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO doppler_user;
\q
```
Si esto no se hace, Django no puede crear la tabla `django_migrations` y `migrate` falla con ?permission denied for schema public?.

6) Variables de entorno (.env)
```dotenv
DEBUG=False
USE_SQLITE=0
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=doppler_prod
DB_USER=doppler_user
DB_PASSWORD=la_password_segura

ALLOWED_HOSTS=IP_PUBLICA,dominio.com

DOPPLER_RELAY_API_KEY=...
DOPPLER_RELAY_ACCOUNT_ID=...
DOPPLER_RELAY_FROM_EMAIL=...
DOPPLER_RELAY_FROM_NAME=...
DOPPLER_RELAY_BASE_URL=https://api.dopplerrelay.com/
DOPPLER_RELAY_AUTH_SCHEME=Bearer
```
Notas:
- En desarrollo: `DEBUG=True`, `USE_SQLITE=1` (no se necesita Postgres ni psycopg).
- En producción: `USE_SQLITE=0` y completar `DB_*` (el servidor SÍ necesita `psycopg2-binary` o `psycopg[binary]` en el venv).
- `analytics` es opcional (segunda conexión Postgres, por ejemplo una base administrada). Se usa para el botón ?Cargar BD (analytics)?.

ALLOWED_HOSTS debe incluir el dominio, la IP pública del droplet y hosts locales:
- Dominio: por ejemplo `app1.ramirezvi.com`
- IP pública de la VPS
- `localhost` y `127.0.0.1` (para pruebas internas)

Ejemplo final recomendado para producción:
```dotenv
DEBUG=False
USE_SQLITE=0

DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=doppler_prod
DB_USER=doppler_user
DB_PASSWORD=la_password_segura

ALLOWED_HOSTS=app1.ramirezvi.com,165.232.xx.xx,localhost,127.0.0.1

DOPPLER_RELAY_API_KEY=...
DOPPLER_RELAY_ACCOUNT_ID=...
DOPPLER_RELAY_FROM_EMAIL=...
DOPPLER_RELAY_FROM_NAME=...
DOPPLER_RELAY_BASE_URL=https://api.dopplerrelay.com/
DOPPLER_RELAY_AUTH_SCHEME=Bearer
```
Importante: si cambias o agregas un dominio nuevo, actualiza `ALLOWED_HOSTS` en `.env` y reinicia Django para que Gunicorn lea las nuevas variables:
```bash
sudo systemctl restart django
```

7) Migraciones, collectstatic y superusuario (orden real)
```bash
cd /opt/app/django-doppler-relay
source .venv/bin/activate
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

8) Servicio systemd de Django (archivo final)
`/etc/systemd/system/django.service`
```
[Unit]
Description=Django Gunicorn Service
After=network.target

[Service]
User=app
Group=www-data
WorkingDirectory=/opt/app/django-doppler-relay
EnvironmentFile=/opt/app/django-doppler-relay/.env
ExecStart=/opt/app/django-doppler-relay/.venv/bin/gunicorn \
  --workers 3 \
  --bind unix:/run/django/django.sock \
  config.wsgi:application
Restart=always
RestartSec=3

# Esto hace que systemd cree /run/django/ con permisos correctos
RuntimeDirectory=django
RuntimeDirectoryMode=0775

[Install]
WantedBy=multi-user.target
```
Comandos:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now django
sudo systemctl status django
```
El socket es `/run/django/django.sock`. Si ves ?Permission denied creating /run/django.sock?, no usaron este archivo actualizado.

9) Nginx
`/etc/nginx/sites-available/django`
```
server {
    listen 80;
    server_name MI_IP_PUBLICA O_TU_DOMINIO;

    location /static/ {
        alias /opt/app/django-doppler-relay/staticfiles/;
    }

    location / {
        include proxy_params;
        proxy_pass http://unix:/run/django/django.sock;
    }
}
```
Habilitar y recargar:
```bash
sudo ln -s /etc/nginx/sites-available/django /etc/nginx/sites-enabled/django
sudo nginx -t
sudo systemctl reload nginx
```
Certbot (opcional si ya tienes dominio):
```bash
sudo certbot --nginx -d midominio.com
```
Con IP directa solo se usa HTTP y el navegador mostrará ?no seguro? (esperado).

9.1) Dominio + HTTPS (Let's Encrypt)

Objetivo: servir la app en `https://subdominio.tu-dominio.com` con certificado válido de Let's Encrypt.

Paso A. DNS
- Crear un registro A en el DNS del dominio:
  - Host/Name: `app1` (o el subdominio que quieres usar)
  - Valor/IP: la IP pública del droplet (ej: `165.232.xx.xx`)
- Esperar a que `ping app1.tu-dominio.com` resuelva a esa IP.

Paso B. Bloque inicial de Nginx
Editar `/etc/nginx/sites-available/django` para que escuche en ese dominio. Ejemplo:
```
server {
    listen 80;
    server_name app1.tu-dominio.com;

    location /static/ {
        alias /opt/app/django-doppler-relay/staticfiles/;
    }

    location / {
        include proxy_params;
        proxy_pass http://unix:/run/django/django.sock;
    }
}
```
Activar y validar:
```bash
sudo ln -sf /etc/nginx/sites-available/django /etc/nginx/sites-enabled/django
sudo nginx -t
sudo systemctl reload nginx
```
En este punto `http://app1.tu-dominio.com/admin` debe cargar (aún ?no seguro?).

Paso C. Emitir certificado SSL con Certbot

Instalar Certbot (si no está):
```bash
sudo apt install -y certbot python3-certbot-nginx
```
Ejecutar:
```bash
sudo certbot --nginx -d app1.tu-dominio.com
```
Durante el asistente:
- Poner un correo válido
- Aceptar términos
- Elegir la opción que redirige HTTP ? HTTPS (force redirect)

Esto hace dos cosas automáticamente:
- Crea configuración `listen 443 ssl;` con el certificado de Let's Encrypt
- Configura redirección `80 ? 443`

Después de esto, la app queda disponible en `https://app1.tu-dominio.com/admin` con candado verde.

Paso D. Renovación automática
Certbot deja una tarea en cron/systemd. Probar con:
```bash
sudo certbot renew --dry-run
```

Importante: cada vez que agregues un nuevo dominio/subdominio:
- Añádelo en DNS apuntando al droplet
- Agrégalo a `server_name` en Nginx
- Agrégalo a `ALLOWED_HOSTS` en `.env`
- Reinicia Django: `sudo systemctl restart django`
- Corre: `sudo certbot --nginx -d nuevo-subdominio.dominio.com`

10) Timer de reportería (opcional)
Servicio `/etc/systemd/system/reports-process.service`:
```
[Unit]
Description=Process pending Doppler reports (process_reports_pending)
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=app
Group=www-data
WorkingDirectory=/opt/app/django-doppler-relay
EnvironmentFile=/opt/app/django-doppler-relay/.env
Environment="PATH=/opt/app/django-doppler-relay/.venv/bin"
ExecStart=/opt/app/django-doppler-relay/.venv/bin/python manage.py process_reports_pending
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```
Aviso: No declares `RuntimeDirectory` en este servicio. Ese directorio (`/run/django/`) es del servicio principal `django.service` (Gunicorn). Si el timer reclama ese directorio, Nginx puede perder el socket `/run/django/django.sock` y la app devolverá 502 Bad Gateway hasta reiniciar `django`.

Timer `/etc/systemd/system/reports-process.timer`:
```
[Unit]
Description=Run Doppler reports processor periodically

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
Unit=reports-process.service
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
```
`OnUnitActiveSec=15min` controla la frecuencia. Si quieres otra (p. ej. 10 minutos), cambia ese valor.

Comandos (recarga/enable y verificación):
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now reports-process.timer
sudo systemctl status reports-process.timer
sudo systemctl status reports-process.service
sudo journalctl -u reports-process.service -n 50 --no-pager
```

#### ¿Qué pasa si el operador también hace clic en ?Procesar pendientes ahora? desde el admin?
Es seguro tener timer automático y botón manual a la vez. Si se solapan ejecuciones, no se envían correos duplicados ni se rompe nada; puede haber trabajo duplicado sobre un mismo `GeneratedReport`, pero el flujo termina marcándolo `READY` igual. La reportería corre sobre `GeneratedReport` (PENDING ? PROCESSING ? READY); no dispara campañas ni reenvía emails.

#### Flujo del timer
Cada vez que corre el timer:
- Ejecuta `manage.py process_reports_pending`.
- Busca reportes en estado `PENDING`/`PROCESSING`.
- Pide el CSV a Doppler Relay.
- Descarga el archivo a `attachments/reports/...`.
- Marca el reporte como `READY` (o `ERROR` si falló).
Si no hay pendientes, termina en 1?2 segundos. El timer no queda residente: systemd lo despierta cada X minutos.

Nota: este timer es opcional. Si no lo habilitas, todo sigue funcionando y el operador puede procesar manualmente desde el admin. Si lo habilitas (`enable --now`), la reportería se procesa en background y los reportes pasarán a `READY` sin intervención humana.

11) Adjuntos y CSV (Volume recomendado)
- Crear Volume en DO, montarlo (ej. `/mnt/attachments`)
- Dentro del proyecto: `ln -s /mnt/attachments attachments` para que `attachments/reports/` quede en el volumen (o ajusta rutas en settings)

12) Admin post?deploy (permisos y UI)
- El admin incluye: badge ?Cargado en: <alias>?, bloqueo de doble carga por alias, botones ?Cargar BD (default)? y opcional ?Cargar BD (analytics)?, y ?Procesar pendientes ahora?.
- Crear grupo `Report Managers` y asignar:
  - Ver/descargar reportes
  - `reports.can_process_reports`
  - `reports.can_load_to_db`
  - Ver/agregar/cambiar `GeneratedReport`
- Asignar el grupo a los usuarios operativos (no es necesario que sean superusers).

13) Actualizaciones (pull y restart)
```bash
cd /opt/app/django-doppler-relay
sudo -u app git pull
source .venv/bin/activate
.venv/bin/python -m pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
sudo systemctl restart django
```
Si cambió la tarea de reportería:
```bash
sudo systemctl daemon-reload
sudo systemctl restart reports-process.timer
```

Solución de problemas
- 502 Bad Gateway: verifica `systemctl status django`, que exista `/run/django/django.sock` y que Nginx apunte a ese socket.
- Gunicorn: `journalctl -u django -f`
- Nginx: `sudo nginx -t && sudo tail -f /var/log/nginx/error.log`
- Timer: `journalctl -u reports-process -f`

### Errores comunes
- `DisallowedHost at /admin/` con ?You may need to add 'app1.tu-dominio.com' to ALLOWED_HOSTS.?
  - El dominio no está incluido en `ALLOWED_HOSTS`.
  - Solución: editar `/opt/app/django-doppler-relay/.env`, añadir el dominio a `ALLOWED_HOSTS` y reiniciar:
    ```bash
    sudo systemctl restart django
    ```
- `502 Bad Gateway` en el navegador:
  - Revisa que `django.service` esté activo: `sudo systemctl status django`
  - Revisa que exista `/run/django/django.sock`: `ls -l /run/django/django.sock`
  - Revisa que Nginx apunte a `proxy_pass http://unix:/run/django/django.sock;`

#### Parámetros opcionales de reportería y timer (pueden ir también en .env)

DOPPLER_REPORTS_TIMEOUT=30
DOPPLER_REPORTS_POLL_INITIAL_DELAY=5
DOPPLER_REPORTS_POLL_MAX_DELAY=15
DOPPLER_REPORTS_POLL_TOTAL_TIMEOUT=900

REPORTS_TIMER_ENABLED=True
REPORTS_TIMER_INTERVAL=15
REPORTS_TIMER_LOCK_PATH=/opt/app/django-doppler-relay/tmp/timer.lock

Los `DOPPLER_REPORTS_*` controlan la paciencia y la frecuencia del polling contra Doppler Relay cuando se genera la reportería.
Los `REPORTS_TIMER_*` documentan la operación del systemd timer.
Estas variables son opcionales: la app sigue funcionando aunque no estén presentes. Si no activas el timer en systemd, puedes procesar reportes manualmente desde el admin.

14) Scheduler de envíos programados (opcional)

Nota previa
- Requiere la migración `relay/migrations/20251029151212_scheduled_fields.py` (corre `python manage.py migrate`).

Servicio `/etc/systemd/system/bulk-scheduler.service`
```
[Unit]
Description=Process scheduled bulk sends (process_bulk_scheduled)
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=app
Group=www-data
WorkingDirectory=/opt/app/django-doppler-relay
Environment="PATH=/opt/app/django-doppler-relay/.venv/bin"
ExecStart=/opt/app/django-doppler-relay/.venv/bin/python manage.py process_bulk_scheduled
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Timer `/etc/systemd/system/bulk-scheduler.timer`
```
[Unit]
Description=Run bulk scheduler periodically

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
Unit=bulk-scheduler.service
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
```

Comandos
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bulk-scheduler.timer
sudo systemctl list-timers | grep bulk-scheduler
journalctl -u bulk-scheduler -n 50 --no-pager
```

Concurrencia y seguridad
- El scheduler usa `select_for_update(skip_locked=True)` y `processing_started_at` para evitar solapes/doble ejecución.
- Si un operador dispara manualmente antes de la hora, el envío se hace inmediato; el scheduler ya lo encontrará como `done`/`error`.

Variables de entorno (documentativas, opcionales; el intervalo real lo define systemd)
```dotenv
BULK_SCHEDULER_ENABLED=True
BULK_SCHEDULER_INTERVAL_MIN=2
```

15) Post‑envío automatizado (opcional)

Objetivo
- Cargar automáticamente la reportería del día del envío para los BulkSend `done` con más de 1 hora de antigüedad.
- Este job ejecuta `manage.py process_post_send_reports`, que:
  - Crea `GeneratedReport` por tipo (deliveries, bounces, opens, clicks, spam, unsubscribed, sent) para ese día si no existen.
  - Procesa pendientes (`process_reports_pending`) y descarga CSVs.
  - Carga tipado a BD local (`load_report_to_db(..., target_alias="default")`).
  - Marca el BulkSend con `post_reports_status='done'` y `post_reports_loaded_at`.

Servicio `/etc/systemd/system/post-send-reports.service`
```
[Unit]
Description=Post-send reporting job (process_post_send_reports)
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=app
Group=www-data
WorkingDirectory=/opt/app/django-doppler-relay
Environment="PATH=/opt/app/django-doppler-relay/.venv/bin"
ExecStart=/opt/app/django-doppler-relay/.venv/bin/python manage.py process_post_send_reports
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Timer `/etc/systemd/system/post-send-reports.timer`
```
[Unit]
Description=Run post-send reporting periodically

[Timer]
OnBootSec=10min
OnUnitActiveSec=60min
Unit=post-send-reports.service
AccuracySec=2min
Persistent=true

[Install]
WantedBy=timers.target
```

Comandos
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now post-send-reports.timer
sudo systemctl list-timers | grep post-send-reports
sudo journalctl -u post-send-reports -n 50 --no-pager
```

Notas
- El botón “Ver reporte” del admin aparece cuando el BulkSend está en `done` y ya tiene `post_reports_loaded_at` (es decir, cuando este job cargó los datos del día en la BD local).
- Si no habilitas este timer, puedes ejecutar manualmente:
  - `python manage.py process_post_send_reports`
  - `python manage.py process_reports_pending` (si quieres forzar el ciclo de PENDING/PROCESSING)

16) Actualizaciones rápidas (pull y restart)

```bash
cd /opt/app/django-doppler-relay
sudo -u app git pull
source .venv/bin/activate
.venv/bin/python -m pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
sudo systemctl restart django

# Si cambió la reportería o timers
sudo systemctl daemon-reload
sudo systemctl restart post-send-reports.timer
# (opcional) si usas el procesador de pendientes
sudo systemctl restart reports-process.timer
```

Tips de configuración
- Para ocultar el módulo “Reports” del menú del admin, deja `REPORTS_ADMIN_VISIBLE=0` (default). Si deseas verlo, define `REPORTS_ADMIN_VISIBLE=1` en `.env` y reinicia `django`.
- El botón “Ver reporte (nuevo)” y las descargas siguen funcionando aunque el módulo Reports esté oculto, porque usan rutas internas del admin.

17) Solo post‑envío (deshabilitar reports‑process.timer)

Si ya no necesitas el flujo “solicitar/pendientes” y quieres quedarte únicamente con el job de 1 hora (post‑envío):

Opción A — Deshabilitar y detener el timer de pendientes (recomendado)
```bash
sudo systemctl disable --now reports-process.timer
sudo systemctl daemon-reload
systemctl list-timers | grep reports-process   # no debería listar
```

Opción B — Dejar el servicio para uso manual (sin timer)
```bash
sudo systemctl disable --now reports-process.timer
sudo systemctl daemon-reload
# cuando necesites correrlo manualmente:
sudo systemctl start reports-process.service
sudo journalctl -u reports-process -n 50 --no-pager
```

Opción C — Reutilizar el mismo timer para post‑envío (si no quieres crear otro)
1) Edita `/etc/systemd/system/reports-process.service` y cambia ExecStart:
```
ExecStart=/opt/app/django-doppler-relay/.venv/bin/python manage.py process_post_send_reports
```
2) Edita `/etc/systemd/system/reports-process.timer` y ajusta el intervalo:
```
OnUnitActiveSec=60min
```
3) Recarga y habilita:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now reports-process.timer
```

Recomendación: dejar activo solo `post-send-reports.timer` y mantener `reports-process.service` disponible para ejecuciones manuales si hiciera falta.

18) Checkout de versión estable por cliente

Para fijar la VPS del cliente a una versión estable y evitar que un `git pull` mueva la app sin revisión, crea una rama de producción desde el tag:

```bash
cd /opt/app/django-doppler-relay
git fetch --tags
git checkout -B prod-<cliente> v2025.11.10-stable+indent
```

Volver a master cuando quieras actualizar desde principal:

```bash
git checkout master
git pull
```

19) Crear .env desde el ejemplo

Partir de `.env.example` y editar:

```bash
cd /opt/app/django-doppler-relay
cp .env.example .env
# Edita .env y completa DEBUG/USE_SQLITE/DB_*/ALLOWED_HOSTS/DOPPLER_*
```

20) Zona horaria del servidor

Configurar TZ local mejora la lectura de logs del sistema. La app sigue usando `TIME_ZONE` de Django para cálculos.

```bash
sudo timedatectl set-timezone America/Guayaquil

# Reiniciar servicios para aplicar
sudo systemctl restart django
sudo systemctl reload nginx
sudo systemctl restart post-send-reports.timer
# (opcional) si usas el procesador de pendientes
sudo systemctl restart reports-process.timer
```
