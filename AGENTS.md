# AGENTS

Guia rapida para agentes humanos o automaticos que trabajan con esta base de codigo.

## Flujo de envio
- Usar `POST /relay/send/` como punto de entrada principal.
- Normalizar la entrada a la estructura `recipients -> {email, variables}` antes de invocar `process_bulk_template_send`.
- Evitar lotes vacios: no llamar a Doppler si no hay destinatarios validos.

## Priorizacion de remitente
1. Respetar `from_email` y `from_name` recibidos en la solicitud cuando ambos existan.
2. Si falta alguno, consultar `UserEmailConfig` del usuario autenticado.
3. Como ultimo recurso, usar `DOPPLER_RELAY_FROM_EMAIL` y `DOPPLER_RELAY_FROM_NAME` del settings.
4. Nunca enviar sin validar formato de email (usar `validate_email`).

## Validaciones obligatorias
- `template_id` no puede ser vacio.
- Cada destinatario debe incluir email valido; descartar filas sin correo.
- Convertir todas las variables a string y eliminar valores `None`.
- Para CSV, asegurar columna `email`; opcionalmente respetar `email_column` del request.
- Adjuntos: deben contener `filename`/`name` y `content` en base64; codificar si se recibe contenido crudo.

## Manejo de adjuntos
- En vistas y admin usar `Attachment.to_doppler_format()` para transformar archivos almacenados.
- Validar base64 con `base64.b64decode` antes de enviar a la API.
- Limitar el tamano final segun las politicas de Doppler (no implementado, documentar si se necesita).

## Persistencia y auditoria
- Registrar exitos creando `EmailMessage` con el `relay_message_id` retornado.
- Guardar errores en `resultados` para responder al cliente y en `bulk.log`/`bulk.result` en admin.
- Mantener `BulkSend.status` en `pending|done|error` segun resultado real del procesamiento.

## Configuracion y entorno
- Cargar variables desde `.env` mediante `environ.Env` al iniciar.
- Confirmar que `DOPPLER_RELAY_API_KEY` y `DOPPLER_RELAY_ACCOUNT_ID` esten definidos antes de enviar.
- No hay comando predefinido para sincronizar entregas/eventos; usa `DopplerRelayClient` en scripts de polling segun necesidad.

## Operacion en admin
- Accion "Procesar envio masivo" espera CSV delimitado por `;` y valida variables Mustache contra la plantilla remota.
- Accion "Enviar emails seleccionados" solo funciona para `EmailMessage.status == "created"`.
- Al activar una configuracion de remitente desde admin, desactiva las otras del mismo usuario.

## Observaciones
- Tests existentes no cubren el flujo principal; agregar casos para JSON, CSV y permisos cuando se automatice.
- Registrar trazas relevantes con `print` solo en entornos de prueba; considerar reemplazar por logging estructurado en produccion.

