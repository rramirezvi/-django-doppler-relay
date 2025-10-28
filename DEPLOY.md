# Guía de Deploy en Producción (DigitalOcean)

Objetivo: provisionar una Droplet limpia y dejar el proyecto corriendo con Nginx + Gunicorn + PostgreSQL local + systemd timer opcional para reportería. Este documento es el flujo final que usamos en producción real.

Requisitos previos
- Dominio apuntando a la IP pública de la Droplet (A record)
- Llave SSH para acceso
- Credenciales/API de Doppler Relay
- (Opcional) Credenciales de la base analítica `analytics`

Tamaño recomendado
- Droplet Ubuntu LTS (24.04 o 22.04)
- 2 vCPU / 4 GB RAM / 80–160 GB SSD
- Si almacenarás muchos CSV: añade un Volume (100–250 GB) y móntalo en `attachments/`
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
Si esto no se hace, Django no puede crear la tabla `django_migrations` y `migrate` falla con “permission denied for schema public”.

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
- `analytics` es opcional (segunda conexión Postgres, por ejemplo una base administrada). Se usa para el botón “Cargar BD (analytics)”.

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
El socket es `/run/django/django.sock`. Si ves “Permission denied creating /run/django.sock”, no usaron este archivo actualizado.

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
Con IP directa solo se usa HTTP y el navegador mostrará “no seguro” (esperado).

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
Environment="PATH=/opt/app/django-doppler-relay/.venv/bin"
ExecStart=/opt/app/django-doppler-relay/.venv/bin/python manage.py process_reports_pending
Restart=on-failure
RestartSec=10
RuntimeDirectory=django
RuntimeDirectoryMode=0775

[Install]
WantedBy=multi-user.target
```

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
Comandos:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now reports-process.timer
sudo systemctl list-timers | grep reports-process
sudo journalctl -u reports-process -n 20 --no-pager
```
Este timer es OPCIONAL: sin timer puedes ir al admin y usar “Procesar pendientes ahora”. Con timer, el servidor procesa PENDING cada 15 min automáticamente.

11) Adjuntos y CSV (Volume recomendado)
- Crear Volume en DO, montarlo (ej. `/mnt/attachments`)
- Dentro del proyecto: `ln -s /mnt/attachments attachments` para que `attachments/reports/` quede en el volumen (o ajusta rutas en settings)

12) Admin post‑deploy (permisos y UI)
- El admin incluye: badge “Cargado en: <alias>”, bloqueo de doble carga por alias, botones “Cargar BD (default)” y opcional “Cargar BD (analytics)”, y “Procesar pendientes ahora”.
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
