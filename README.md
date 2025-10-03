# Django + Doppler Relay (ready-to-run, SQLite)

Proyecto Django mínimo con integración **Doppler Relay**:
- Enviar correos (HTML/Text/adjuntos).
- Consultar entregas y eventos (estado).
- CRUD de plantillas y envío por plantilla.
- Comando `relay_sync` para sincronización (cron/Celery Beat).
- Config listo para **SQLite**.

## Requisitos
- Python 3.10+

## Instalación rápida
```bash
cd django-doppler-relay
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# Linux/Mac:
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env  # (Windows) / cp .env.example .env (Linux/Mac)
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```
Endpoints:
- `POST /relay/send/`
- `GET  /relay/deliveries/?hours=24`
- `GET  /relay/events/?hours=24`
- `GET  /relay/templates/`
- `POST /relay/templates/create/`
- `GET  /relay/templates/<id>/`
- `PUT  /relay/templates/<id>/update/`
- `DELETE /relay/templates/<id>/delete/`
- `POST /relay/templates/<id>/send/`

## Ejecutar ejemplo rápido
```bash
python examples/send_sample.py
```

## Producción
- Verifica SPF/DKIM del dominio remitente en Doppler Relay.
- Programa `python manage.py relay_sync --hours 2` con cron.
