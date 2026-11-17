# Entrega — Alta na Pipa en producción

Dejé montado el sitio de Alta na Pipa en su propio servidor, administrado
con Primate Cloud Manager.

| | |
|---|---|
| Sitio | https://altanapipa.primatecloudmanager.com |
| Backend | https://altanapipa.primatecloudmanager.com/web/login |
| Servidor | AWS EC2 `i-0363eeeca143781ea` · t3.medium · 30 GB · us-east-1 |
| IP fija | 32.195.171.222 (Elastic IP: reiniciar el servidor no la cambia) |
| Odoo | 19 Community · 59 módulos · português (pt_BR) |
| Monitoreo | agente CloudWatch activo |

La instalación quedó verificada contra la lista de referencia del repo
(`docs/MODULES_PROD.txt`, 59 módulos exactos), con los PDF generados por el
wkhtmltopdf correcto (0.12.6.1 con Qt parcheado), HTTPS con certificado
válido y renovación automática, y el gestor de bases de datos oculto.

## Lo que falta configurar a mano (Diego)

1. **Compañía**: en Ajustes → Compañías, renombrar «My Company» a
   **Alta na Pipa** y poner país **Brasil**.
2. **WhatsApp**: cargar el número de la escuela en Ajustes del sitio.
   Mientras esté vacío, `/agendar` redirige a `/contato` — es el
   comportamiento previsto, no un error.
3. **Correo saliente (SMTP)**: configurar un servidor para que salgan los
   avisos (Ajustes técnicos → Servidores de correo saliente).
4. **Padrón de alumnos**: Contactos → «Importar cadastro do Tecnofit», con
   el export `.xlsx` de Tecnofit (columnas `ref`, `cliente`, `nascimento`).
   ⚠️ **La fecha de nacimiento es la credencial de acceso de los alumnos
   en `/entrar`: el archivo del padrón NO se sube al repositorio, no se
   manda por canales abiertos y no se copia a ambientes de prueba.**

## Mis pendientes

- **Token de GitHub de los repos**: el PAT que usan los 4 repos privados
  vence a los ~90 días (≈ **15 de febrero de 2027**). Antes de esa fecha:
  rotarlo, o mejor, reemplazarlo por **deploy keys SSH** por repositorio
  (no vencen y tienen alcance por repo). Queda recordatorio en la ficha
  del cliente.
- Primer respaldo automático: asignar la política de respaldo diaria al
  entorno (hoy el entorno quedó sin política; el respaldo manual está
  disponible desde el panel en cualquier momento).
- Ambiente de staging para pruebas (segunda instancia en el mismo
  servidor, con copia neutralizada de producción) — planificado.

## Operación diaria

Todo se administra desde Primate Cloud Manager
(https://primatecloudmanager.com): estado del servidor, reinicios,
despliegues de los repos, respaldos y restauración, registros DNS, HTTPS,
métricas y bitácora de operaciones.
