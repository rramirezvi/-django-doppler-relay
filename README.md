# Django Doppler Relay (ready-to-run, SQLite)

Aplicacion Django minimal lista para integrarse con Doppler Relay y enfocada en envios masivos por plantilla.

## Caracteristicas
- Envio de correos con plantillas de Doppler Relay y soporte para variables por destinatario.
- Flujo unico para JSON o CSV que normaliza destinatarios, variables y adjuntos.
- Seleccion de remitente con prioridad: datos del request > configuracion por usuario > defaults en settings.
- Registro local de mensajes, adjuntos, lotes y eventos para auditoria.
- Panel de administracion con acciones para reprocesar lotes y enviar mensajes manualmente.

## Reglas funcionales actuales
- `template_id` es obligatorio en cualquier envio.
- Cada destinatario necesita un email valido; se rechazan vacios o con formato invalido.
- Las variables se aceptan en `variables` o `substitution_data` y se convierten a string, descartando valores `None`.
- Los adjuntos deben venir en base64; si no lo estan se codifican antes de llamar a la API.
- La vista `send_bulk_email` envia cada destinatario de forma individual contra Doppler Relay y persiste un `EmailMessage` por exito.
- Para CSV se requiere al menos una columna `email` (por defecto se usa `email_column=email`).
- `UserEmailConfig` garantiza que solo una configuracion por usuario este activa; al activar una nueva las demas se desactivan.
- Los endpoints de configuracion de remitente requieren usuario autenticado.
- No se incluye comando de sincronizacion; implementa polling segun tus necesidades con `DopplerRelayClient`.

## Requisitos
- Python 3.10 o superior

## Puesta en marcha rapida
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
- `DEBUG`: activa modo debug (usar False en produccion).
- `SECRET_KEY`: clave Django.
- `ALLOWED_HOSTS`: lista separada por comas para hosts permitidos.
- `DOPPLER_RELAY_API_KEY`: API key valida.
- `DOPPLER_RELAY_ACCOUNT_ID`: ID numerico de la cuenta Doppler Relay.
- `DOPPLER_RELAY_AUTH_SCHEME`: esquema de autorizacion (`Bearer`, `token`, etc.).
- `DOPPLER_RELAY_BASE_URL`: raiz de la API (por defecto `https://api.dopplerrelay.com/`).
- `DOPPLER_RELAY_FROM_EMAIL` y `DOPPLER_RELAY_FROM_NAME`: remitente por defecto usado como ultimo fallback.

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

Respuesta tipica:
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
- Retorna la configuracion activa del remitente para el usuario autenticado o los valores por defecto.

### POST `/relay/user/email-config/update/`
- Actualiza `from_email` y `from_name` para el usuario autenticado.
- Campos requeridos: `from_email` (validado con regex). `from_name` es opcional.

## Panel de administracion
- Modelos registrados: `EmailMessage`, `BulkSend`, `Attachment`, `Delivery`, `Event`, `UserEmailConfig`.
- `BulkSend` permite cargar CSV (delimitador `;` por defecto) y adjuntos guardados; la accion "Procesar envio masivo" valida variables contra la plantilla y ejecuta `process_bulk_template_send`.
- `EmailMessage` incluye accion "Enviar emails seleccionados" que usa `DopplerRelayClient.send_message` y registra `relay_message_id`.

## Comandos y scripts
- Por ahora no se incluye un comando de sincronizacion; crea scripts personalizados para consultar entregas/eventos segun tu programacion.
- `python examples/send_sample.py`: ejemplo rapido de envio simple usando `send_message`.

## Pruebas
- Cobertura minima en `relay/tests.py`; se recomienda ampliar pruebas para los flujos JSON, CSV y admin.

## Seguridad
- Revisa `SECURITY.md` para politicas y buenas practicas de credenciales, dependencias y datos sensibles.

