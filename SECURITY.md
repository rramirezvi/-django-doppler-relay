SECURITY.md

# Política de Seguridad

## Reportando Vulnerabilidades

Si descubres una vulnerabilidad de seguridad dentro de Django Doppler Relay, por favor envía un correo electrónico a desarrollo@ramirezvi.com. Todas las vulnerabilidades de seguridad serán atendidas con prontitud.

## Prácticas Recomendadas

1. **Variables de Entorno**
   - Nunca commits credenciales en el código
   - Usa archivos .env para las credenciales
   - Mantén las API keys seguras

2. **Dependencias**
   - Actualiza regularmente las dependencias
   - Revisa las vulnerabilidades conocidas
   - Usa herramientas de escaneo de seguridad

3. **Autenticación**
   - Implementa autenticación fuerte
   - Usa tokens de acceso seguros
   - Rota las credenciales regularmente

4. **Datos Sensibles**
   - Cifra los datos sensibles
   - No almacenes información innecesaria
   - Implementa políticas de retención de datos

## Versiones Soportadas

| Versión | Soporte          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |

## Proceso de Actualización

1. Mantén tu sistema actualizado
2. Sigue las notas de la versión
3. Prueba en ambiente de desarrollo primero
4. Haz backups antes de actualizar