# Templates Admin (Doppler Relay)

Módulo de administración para gestionar plantillas de Doppler Relay desde Django: listar, crear, editar (con vista previa) y eliminar.

## Funcionamiento

- Ubicación: sección `Templates` en el admin.
- Preview: el iframe renderiza el contenido del campo "HTML body" en vivo mientras editas.
- Cómo obtenemos el HTML al editar:
  1) Llamamos al endpoint principal `get_template(account_id, template_id)`.
  2) Buscamos campos de contenido en el JSON (y anidados): `html`, `htmlContent`, `body`, `content`, `textContent`.
  3) Si no vienen en el JSON, seguimos el enlace de `_links` cuyo `rel` es `/docs/rels/get-template-body` y hacemos `GET` al `href` (por ejemplo `/accounts/.../body`).
  4) Si tampoco está disponible, prellenamos desde caché local si existe.

> Nota: No se usan rutas alternativas como `/html` o `/source` porque no están disponibles en todas las cuentas.

## Caché local de HTML

- Ruta: `attachments/templates/<template_id>.html`.
- Se guarda automáticamente al actualizar la plantilla desde esta UI.
- Si la API no devuelve el cuerpo, se usa este caché para prellenar futuras ediciones.

## Requisitos de entorno

- Variables Doppler: `DOPPLER_RELAY_API_KEY`, `DOPPLER_RELAY_ACCOUNT_ID`, `DOPPLER_RELAY_BASE_URL`, `DOPPLER_RELAY_AUTH_SCHEME`.
- Permisos de escritura para la carpeta `attachments/templates/`.

## Consideraciones

- Este módulo no altera la lógica de envío; sólo facilita el CRUD y la edición/preview de plantillas.
- Si el proveedor no expone el cuerpo en el JSON ni por `_links` con `get-template-body`, puedes pegar el HTML manualmente; quedará guardado en caché local para próximas ediciones.

