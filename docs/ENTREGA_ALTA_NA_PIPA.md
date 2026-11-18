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
- ~~Primer respaldo automático~~ **HECHO**: política «Alta na Pipa diario»
  asignada (diaria 03:00 UTC, retención 30 días, bucket
  `pcm-backups-512060111009`), con el primer respaldo manual ya verificado
  en S3. **Dónde se asigna/cambia**: Entornos → (el entorno) → sección
  Respaldos → botón «Asignar política…». Desde ahí mismo, «Respaldar
  ahora» dispara uno manual.
- Ambiente de staging para pruebas (segunda instancia en el mismo
  servidor, con copia neutralizada de producción) — planificado.
- Nota técnica del log de instalación: los avisos «_sql_constraints is no
  longer supported» que aparecieron NO venían de los módulos del cliente
  (revisados los cuatro repos: cero usos de la API vieja — bien ahí),
  sino de una herramienta interna de administración, ya corregida. Nada
  que migrar del lado de Alta na Pipa.

## Operación diaria

Todo se administra desde Primate Cloud Manager
(https://primatecloudmanager.com): estado del servidor, reinicios,
despliegues de los repos, respaldos y restauración, registros DNS, HTTPS,
métricas y bitácora de operaciones.

## Staging (2026-11-18)

Montado `https://altanapipa-staging.primatecloudmanager.com`: una segunda
instancia Odoo en el mismo servidor, con recursos acotados por systemd
(1 GB de memoria máx., prioridad baja de CPU/disco) para que producción
siempre mande. Su base es una copia de producción **neutralizada**: correo
saliente bloqueado (queda solo el centinela oficial de Odoo apuntando a un
host inválido), crons apagados, URL propia, y el padrón del portal
anonimizado — las fechas de nacimiento se borran y los usuarios portal
reales quedan desactivados. Para probar el ingreso de alumnos hay dos
fichas de prueba: `STAGING-TEST-1` y `STAGING-TEST-2`, fecha 01/01/2000.

Los repos del staging son clones propios: `alta_na_pipa` sigue la rama
`19.0_staging`; los demás espejan la rama de producción. Diego puede
probar en staging y, cuando esté conforme, mergear a `19.0` y desplegar
en producción desde PCM.

Para recargar el staging con datos frescos de producción: pantalla de la
instancia staging → «Refrescar desde producción» (vuelve a copiar y
neutralizar; jamás escribe sobre producción).
