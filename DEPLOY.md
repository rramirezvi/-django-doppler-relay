# Guía de Deploy en Producción (DigitalOcean)

Objetivo: provisionar una Droplet limpia y dejar corriendo el proyecto con Nginx + Gunicorn + systemd timer para reportería, sin tener que leer todo el README.

Requisitos previos
- Dominio apuntando a la IP pública de la Droplet (A record)
- Llave SSH para acceso
- Credenciales/API de Doppler Relay
- Credenciales de la base analítica (si usas `analytics`)

Tamaño recomendado
- Droplet Ubuntu LTS (24.04 o 22.04)
- 2 vCPU / 4 GB RAM / 80–160 GB SSD
- Si almacenarás muchos CSV: añade un Volume (100–250 GB) y móntalo en `attachments/`
- Base analítica: DigitalOcean Managed PostgreSQL (opcional, recomendado)

1) Acceso inicial, usuario no root y hardening
- Conéctate por SSH como `root`
- Crea usuario y dale sudo:
  ```bash
  adduser app
  usermod -aG sudo app
  ```
- Copia tu llave a `app` (si usas ssh-copy-id): `ssh-copy-id app@IP`
- Activa firewall básico y puertos web:
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
sudo apt -y install python3-pip python3-venv git nginx certbot python3-certbot-nginx
```

3) Clonar el repositorio
```bash
sudo mkdir -p /opt/app
sudo chown app:app /opt/app
cd /opt/app
git clone https://github.com/rramirezvi/-django-doppler-relay.git
cd -django-doppler-relay
```

4) Virtualenv, requirements y .env
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Rellena `.env` (obligatorio):
- `SECRET_KEY=...`
- `DEBUG=False`
- `ALLOWED_HOSTS=mi-dominio.com,api.mi-dominio.com`
- `DOPPLER_RELAY_API_KEY=...`
- `DOPPLER_RELAY_ACCOUNT_ID=...`
- `DOPPLER_RELAY_AUTH_SCHEME=Bearer` (u otro)
- `DOPPLER_RELAY_BASE_URL=https://api.dopplerrelay.com/`
- `DOPPLER_RELAY_FROM_EMAIL=...`
- `DOPPLER_RELAY_FROM_NAME=...`

Parámetros de reportería (opcionales, defaults razonables):
- `DOPPLER_REPORTS_TIMEOUT`, `DOPPLER_REPORTS_POLL_INITIAL_DELAY`, `DOPPLER_REPORTS_POLL_MAX_DELAY`, `DOPPLER_REPORTS_POLL_TOTAL_TIMEOUT`

5) Ajustar bases de datos en settings
- Por defecto `default` es SQLite (`db.sqlite3`)
- Para usar una base analítica externa `analytics` en PostgreSQL, añade el alias leyendo variables del entorno. Edita `config/settings.py` y agrega después de `DATABASES`:

```python
AN_HOST = env('ANALYTICS_DB_HOST', default='')
AN_PORT = env('ANALYTICS_DB_PORT', default='5432')
AN_NAME = env('ANALYTICS_DB_NAME', default='')
AN_USER = env('ANALYTICS_DB_USER', default='')
AN_PASSWORD = env('ANALYTICS_DB_PASSWORD', default='')
if AN_HOST and AN_NAME and AN_USER:
    DATABASES['analytics'] = {
        'ENGINE': 'django.db.backends.postgresql',
        'HOST': AN_HOST,
        'PORT': AN_PORT,
        'NAME': AN_NAME,
        'USER': AN_USER,
        'PASSWORD': AN_PASSWORD,
        'OPTIONS': {
            'sslmode': env('ANALYTICS_DB_SSLMODE', default='require'),
        },
    }
```

Y en `.env` agrega:
```
ANALYTICS_DB_HOST=db-analytics.example.com
ANALYTICS_DB_PORT=5432
ANALYTICS_DB_NAME=relay_analytics
ANALYTICS_DB_USER=analytics_user
ANALYTICS_DB_PASSWORD=********
ANALYTICS_DB_SSLMODE=require
```

6) Migraciones, estáticos y superusuario
```bash
source .venv/bin/activate
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

7) Gunicorn con systemd
Archivo `/etc/systemd/system/django.service` (ajusta rutas si cambian):
```
[Unit]
Description=Django Gunicorn
After=network.target

[Service]
User=app
Group=www-data
WorkingDirectory=/opt/app/-django-doppler-relay
EnvironmentFile=/opt/app/-django-doppler-relay/.env
ExecStart=/opt/app/-django-doppler-relay/.venv/bin/gunicorn --workers 3 --bind unix:/run/django.sock config.wsgi:application
Restart=always

[Install]
WantedBy=multi-user.target
```

Habilitar y arrancar:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now django
sudo journalctl -u django -f
```

8) Nginx + HTTPS
Bloque `/etc/nginx/sites-available/django` (reemplaza dominio):
```
server {
    listen 80;
    server_name mi-dominio.com;

    location /static/ {
        alias /opt/app/-django-doppler-relay/staticfiles/;
    }

    location / {
        include proxy_params;
        proxy_pass http://unix:/run/django.sock;
    }
}
```
Activar y recargar:
```bash
sudo ln -s /etc/nginx/sites-available/django /etc/nginx/sites-enabled/django
sudo nginx -t && sudo systemctl reload nginx
```
Certbot (HTTPS):
```bash
sudo certbot --nginx -d mi-dominio.com
```

9) Timer de reportería (process_reports_pending)
Servicio `/etc/systemd/system/reports-process.service`:
```
[Unit]
Description=Process pending Doppler reports
After=network.target

[Service]
User=app
WorkingDirectory=/opt/app/-django-doppler-relay
ExecStart=/opt/app/-django-doppler-relay/.venv/bin/python manage.py process_reports_pending
```

Timer `/etc/systemd/system/reports-process.timer`:
```
[Unit]
Description=Run process_reports_pending periodically

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
Unit=reports-process.service

[Install]
WantedBy=timers.target
```

Habilitar:
```bash
sudo systemctl enable --now reports-process.timer
sudo systemctl list-timers | grep reports-process
```

10) Paso post‑deploy en el admin (manual)
- Ir a `/admin`
- Crear grupo `Report Managers`
- Asignar permisos: `reports.can_process_reports`, `reports.can_load_to_db`, y ver/agregar/cambiar `GeneratedReport`
- Asignar el grupo a los usuarios operativos

11) Adjuntos y CSV (Volume recomendado)
- Crear Volume en DO, montarlo (ej. `/mnt/attachments`)
- Dentro del proyecto: `ln -s /mnt/attachments attachments` para que `attachments/reports/` quede en el volumen (o ajusta rutas en settings si prefieres)

12) Actualizaciones (pull y restart)
```bash
cd /opt/app/-django-doppler-relay
git pull
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
sudo systemctl restart django
```

Solución de problemas
- Gunicorn: `journalctl -u django -f`
- Nginx: `sudo nginx -t && sudo tail -f /var/log/nginx/error.log`
- Timer: `journalctl -u reports-process.service -f`

