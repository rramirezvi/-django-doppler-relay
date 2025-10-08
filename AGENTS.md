# AGENTS

Guia rapida para agentes humanos o automaticos que trabajan con esta base de codigo.

## Flujo de envio
- Usar POST /relay/send/ como punto de entrada principal.
- Normalizar la entrada a la estructura ecipients -> {email, variables} antes de invocar process_bulk_template_send.
- Evitar lotes vacios: no llamar a Doppler si no hay destinatarios validos.

## Priorizacion de remitente
1. Respetar rom_email y rom_name recibidos en la solicitud cuando ambos existan.
2. Si falta alguno, consultar UserEmailConfig del usuario autenticado.
3. Como ultimo recurso, usar DOPPLER_RELAY_FROM_EMAIL y DOPPLER_RELAY_FROM_NAME del settings.
4. Nunca enviar sin validar formato de email (usar alidate_email).

## Validaciones obligatorias
- 	emplate_id no puede ser vacio.
- Cada destinatario debe incluir email valido; descartar filas sin correo.
- Convertir todas las variables a string y eliminar valores None.
- Para CSV, asegurar columna email; opcionalmente respetar email_column del request.
- Adjuntos: deben contener ilename/
ame y content en base64; codificar si se recibe contenido crudo.

## Manejo de adjuntos
- En vistas y admin usar Attachment.to_doppler_format() para transformar archivos almacenados.
- Validar base64 con ase64.b64decode antes de enviar a la API.
- Limitar el tamano final segun las politicas de Doppler (no implementado, documentar si se necesita).

## Persistencia y auditoria
- Registrar exitos creando EmailMessage con el elay_message_id retornado.
- Guardar errores en esultados para responder al cliente y en ulk.log/ulk.result en admin.
- Mantener BulkSend.status en pending|done|error segun resultado real del procesamiento.

## Configuracion y entorno
- Cargar variables desde .env mediante environ.Env al iniciar.
- Confirmar que DOPPLER_RELAY_API_KEY y DOPPLER_RELAY_ACCOUNT_ID esten definidos antes de enviar.
- Usa la API de Reports de Doppler Relay con DopplerRelayClient para sincronizar entregas/eventos mediante scripts de polling segun necesidad.

## Operacion en admin
- No se exponen Delivery ni Event en el admin; consulta la API de Reports para monitoreo.
- Accion "Procesar envio masivo" espera CSV delimitado por ; y valida variables Mustache contra la plantilla remota.
- Accion "Enviar emails seleccionados" solo funciona para EmailMessage.status == "created".
- Al activar una configuracion de remitente desde admin, desactiva las otras del mismo usuario.
- Seleccion de plantillas en BulkSend: combo con cache SWR (~5 min), reintentos y circuito de fallos; si falla la API, se muestra aviso y queda el campo manual.
- Reporteria en admin usa descarga directa (create_report_request + polling + CSV); si hay errores se informan via messages.

## Observaciones
- Tests incluyen casos para cache HIT/MISS, fallback de plantillas y limpieza de 	emplate_id; extender cobertura a flujos de envio.
- Registrar trazas relevantes con print solo en entornos de prueba; preferir logging (ya configurado en servicios/admin).
- Cuando se manipule BulkSend, respetar el circuito breaker/caches para evitar limitar la API de Doppler Relay.
