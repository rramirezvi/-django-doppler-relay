# Manual De Usuario - UI Operativa De Envios

Este manual explica como usar la nueva interfaz operativa de envios masivos, que significa cada estado y como funciona la reporteria.

La interfaz esta pensada para operadores. No es necesario entrar al panel tecnico de administracion para crear envios, revisar estados o descargar reportes.

## Acceso

La interfaz operativa esta disponible en:

```text
/app/
```

En produccion:

```text
https://app1.ramirezvi.com/app/
```

Al iniciar sesion como operador, el sistema debe llevarte a esta pantalla operativa.

Si eres administrador, puedes tener acceso adicional al panel administrativo. Si eres operador, normalmente no veras el boton de administracion.

## Pantalla Principal: Envios

La pantalla principal muestra los envios masivos creados.

Columnas principales:

- `ID`: numero interno del envio.
- `Plantilla`: plantilla usada para el correo.
- `Asunto`: asunto del correo enviado.
- `Estado`: estado operativo del envio.
- `Reporte`: estado de la reporteria.
- `Creado`: fecha y hora en que se creo el envio.

Por defecto se muestran los envios del mes vigente.

Puedes usar:

- Buscador: busca por ID, plantilla, asunto, estado o fecha.
- Estado: filtra por todos, pending, done o error.
- Periodo: permite ver mes vigente o todo el historial.
- Actualizar: refresca la informacion manualmente.

La pantalla tambien se actualiza automaticamente cada cierto tiempo para mostrar avances sin recargar toda la pagina.

## Crear Un Nuevo Envio

Entra a `Nuevo envio`.

Completa:

1. `Plantilla`
   - Selecciona la plantilla que se va a enviar.
   - Al elegir una plantilla, el asunto se completa automaticamente si la plantilla lo trae.

2. `Remitente`
   - Puedes usar el correo predeterminado.
   - Si hay remitentes configurados, puedes elegir uno.

3. `Asunto`
   - Se puede ajustar antes de crear el envio.

4. `CSV de destinatarios`
   - Debe contener una columna de correo.
   - La columna puede llamarse normalmente `email`.
   - La UI muestra una vista previa con filas del CSV.

5. `Programar envio`
   - Si quieres enviarlo en una fecha/hora futura, selecciona una fecha.
   - Si dejas este campo vacio, puedes enviarlo inmediatamente.

6. `Encolar envio inmediatamente`
   - Si esta marcado, el envio entra a cola al crear.
   - Si no esta marcado, queda creado pero pendiente de encolar.
   - Si seleccionas una fecha programada, esta opcion se desactiva porque el envio se ejecutara a la hora elegida.

Finalmente presiona:

```text
Crear envio
```

El boton queda bloqueado mientras se crea el envio para evitar doble clic accidental.

## Vista Previa De Plantilla

En `Nuevo envio`, despues de seleccionar plantilla y cargar CSV, puedes usar la vista previa.

La vista previa muestra como se vera la plantilla usando los datos de la primera fila del CSV.

Esto sirve para confirmar que variables como nombre, cedula, valor, telefono u otras columnas se estan reemplazando correctamente.

Si una variable no se reemplaza, revisa que el nombre de la columna en el CSV coincida con la variable esperada por la plantilla.

## Estados Del Envio

La columna `Estado` muestra una version amigable del estado del envio.

### `programado`

El envio tiene fecha futura.

Todavia no se esta enviando.

Cuando llegue la hora programada, el sistema lo pondra en cola automaticamente.

En el detalle lateral veras algo como:

```text
Programado: en 12 min
```

### `pending`

El envio esta creado, pero todavia no ha sido enviado ni encolado.

Puede pasar cuando:

- Se creo el envio sin marcar `Encolar envio inmediatamente`.
- El envio programado ya llego a su hora, pero el scheduler aun no lo ha tomado.
- El envio quedo pendiente de accion manual.

Si corresponde, puedes usar el boton:

```text
Encolar envio
```

### `en cola`

El envio ya fue enviado a la cola de trabajo.

Todavia no empezo a ejecutarse.

Normalmente pasa a `enviando` en poco tiempo.

### `enviando`

El sistema esta procesando el envio.

En este estado se estan enviando los correos hacia Doppler.

No debes volver a presionar `Encolar envio`.

### `done`

El proceso de envio termino correctamente.

Esto significa que el sistema termino de procesar el CSV y de enviar las solicitudes correspondientes a Doppler.

Despues de este estado empieza el conteo para generar reporteria.

### `error`

El envio fallo.

Puede deberse a:

- CSV con columnas incorrectas.
- Correos invalidos.
- Variables faltantes.
- Problemas de configuracion.
- Error temporal de Doppler.

Revisa el detalle lateral y la auditoria para ver mas informacion.

## Flujo Completo De Un Envio Inmediato

Cuando marcas `Encolar envio inmediatamente`, el flujo esperado es:

```text
Crear envio
↓
en cola
↓
enviando
↓
done
↓
not_started en reporte
↓
reporte automatico despues de 15 min
↓
ready
↓
descargar reporte
```

Si el envio es muy pequeno, los estados `en cola` y `enviando` pueden durar poco.

## Flujo Completo De Un Envio Programado

Cuando seleccionas una fecha/hora futura:

```text
Crear envio programado
↓
programado
↓
llega la hora
↓
en cola
↓
enviando
↓
done
↓
reporte automatico despues de 15 min
```

El envio programado no se ejecuta antes de la fecha elegida.

Si llega la hora y queda unos minutos en `pending`, puede ser normal mientras el programador interno lo toma en su siguiente ciclo.

## Panel Lateral De Detalle

Al seleccionar un envio, a la derecha aparece el detalle.

Muestra:

- ID del BulkSend.
- Plantilla.
- Estado del envio.
- Estado del reporte.
- Hace cuanto fue creado.
- Filas de reporte.
- Programacion, si aplica.
- Botones de accion.
- Reportes disponibles.
- Trabajos recientes.
- Log del envio.

Este panel se actualiza cuando seleccionas un envio o cuando presionas:

```text
Refrescar detalle
```

## Boton Encolar Envio

Este boton sirve para enviar un BulkSend que quedo pendiente.

Solo debe usarse cuando el estado esta `pending`.

El sistema protege contra duplicados:

- Si ya hay un trabajo en cola o ejecutandose para ese envio, no permite crear otro.
- Si el envio ya esta `done`, no se debe volver a encolar.

No uses este boton si el envio esta:

- `en cola`
- `enviando`
- `done`
- `error`, salvo que un administrador haya revisado el caso.

## Auditoria

La seccion `Auditoria` muestra trabajos del sistema.

Los trabajos pueden ser:

- Envio masivo.
- Reporte.

Estados de auditoria:

- `queued`: en cola.
- `running`: ejecutandose.
- `done`: terminado.
- `error`: fallo.

La auditoria sirve para saber si el sistema esta trabajando o si algo quedo detenido.

## Reporteria

La reporteria no aparece instantaneamente al terminar el envio.

Esto es normal.

Doppler necesita tiempo para procesar los estados reales de los correos: enviados, en cola, abiertos, clics, errores, etc.

## Estados Del Reporte

La columna `Reporte` muestra el estado de la reporteria.

### `not_started`

Todavia no se ha generado reporte para ese envio.

Puede pasar porque:

- El envio todavia esta `programado`, `pending`, `en cola` o `enviando`.
- El envio ya esta `done`, pero aun no han pasado los 15 minutos para el intento automatico.
- El proceso automatico todavia no ha corrido.

### `pending`

El reporte fue solicitado y esta pendiente de procesar.

### `processing`

El reporte esta siendo procesado.

### `ready`

El reporte esta listo.

Cuando esta `ready`, se puede descargar desde `Reportes disponibles`.

### `error`

Hubo un problema generando o cargando el reporte.

Puede ser temporal. Un administrador puede revisar la auditoria y los logs.

### `zero_rows`

El reporte fue procesado, pero no trajo filas utiles.

Esto puede pasar si Doppler todavia no tiene informacion consolidada o si no hubo actividad reportable.

## Tiempos De Reporteria

Los tiempos definidos son:

### Reporte Automatico

El sistema intenta generar el reporte automaticamente:

```text
15 minutos despues de que el envio queda done
```

Esto permite obtener una primera version sin que el operador tenga que hacer nada.

### Actualizar Reporte Manualmente

El boton `Actualizar reporte` se habilita:

```text
30 minutos despues de que el envio queda done
```

La razon es evitar que el operador actualice demasiado pronto, cuando Doppler aun puede tener correos en cola o datos incompletos.

## Boton Actualizar Reporte

Este boton no reenvia correos.

Sirve para pedir una actualizacion del reporte.

Cuando lo presionas:

1. El sistema crea un trabajo de reporte.
2. El trabajo consulta/genera el reporte con Doppler.
3. Se actualiza la informacion local.
4. Si el archivo queda listo, aparece para descarga.

No tiene sentido presionarlo antes de tiempo, por eso se habilita despues de 30 minutos.

Mientras el reporte esta en cola o procesandose, el boton queda bloqueado.

## Descargar Reporte

La descarga aparece en el detalle lateral bajo:

```text
Reportes disponibles
```

Solo aparece si el reporte esta listo.

El boton `Descargar` descarga el archivo ya generado.

Importante:

- Descargar no consulta Doppler en vivo.
- Descargar no reenvia correos.
- Descargar no modifica el envio.

## Que Hacer Si No Veo El Reporte

Revisa:

1. El envio debe estar `done`.
2. Deben haber pasado al menos 15 minutos para el primer reporte automatico.
3. Si quieres forzar una actualizacion manual, deben haber pasado 30 minutos.
4. Presiona `Refrescar detalle`.
5. Revisa `Auditoria` para ver si hay un trabajo de reporte en `queued`, `running`, `done` o `error`.

## Que Hacer Si El Envio Se Queda En Pending

Puede ser normal si:

- Fue creado sin encolar inmediatamente.
- Fue programado y aun no llega la hora.

No es normal si:

- Ya paso la hora programada hace varios minutos.
- No hay trabajos recientes.
- No cambia a `en cola` o `enviando`.

En ese caso, avisa al administrador para revisar el programador de envios.

## Que Hacer Si El Envio Se Queda En Enviando

Si el envio queda mucho tiempo en `enviando`, no presiones nuevamente `Encolar envio`.

Primero se debe revisar auditoria.

Puede ocurrir si:

- El envio es grande.
- Doppler responde lento.
- El servicio fue reiniciado mientras enviaba.

Si se reinicia el servicio durante un envio, puede quedar un trabajo marcado como ejecutandose. En ese caso debe revisarlo un administrador antes de reenviar, para evitar duplicados.

## Recomendaciones Operativas

- Antes de enviar, usa vista previa de plantilla.
- Revisa que el CSV tenga columna `email`.
- No hagas doble clic en `Crear envio`.
- No reenvíes un BulkSend que esta `en cola` o `enviando`.
- Espera el reporte automatico antes de usar `Actualizar reporte`.
- Usa `Actualizar reporte` solo si necesitas datos mas recientes.
- Si hay duda de si un envio salio o no, revisa auditoria antes de volver a encolar.

## Resumen Rapido De Estados

Estados del envio:

```text
programado -> en cola -> enviando -> done
```

Otros posibles:

```text
pending
error
```

Estados del reporte:

```text
not_started -> pending -> processing -> ready
```

Otros posibles:

```text
error
zero_rows
```

Tiempos:

```text
Reporte automatico: 15 min despues de done
Actualizar reporte: 30 min despues de done
```

