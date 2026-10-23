# Checklist — enlace de ambientes productivos y montaje de Barraca Julia

Orden obligatorio. No se pasa al paso siguiente sin cerrar el anterior.
Todo lo importado es **solo lectura sin excepción** (UI, modelo, cerrojo de
frontera e IAM). Detalle de IAM y del simulador: [`docs/iam/README.md`](iam/README.md).

---

## 1. Enlace con la cuenta de SOLO LECTURA

1. En la cuenta productiva de AWS, crear el usuario IAM **`pcm-prod-ro`** con
   `docs/iam/pcm-prod-readonly.json` (placeholders reemplazados). Nada más
   adjunto (ver «Lo que NUNCA hay que adjuntar»).
2. Un admin crea el documento SSM `pcm-readonly-discovery` en **cada región**
   con servidores (comando en `docs/iam/README.md`).
3. Verificar en el Policy Simulator, con un ID de instancia productiva real:
   `TerminateInstances`/`StopInstances` → **explicitDeny**;
   `SendCommand` con `AWS-RunShellScript` → **implicitDeny**;
   `SendCommand` con `pcm-readonly-discovery` → **allowed**.
4. En PCM: Cuentas AWS → nueva cuenta «Prod — solo lectura» con las claves de
   `pcm-prod-ro`. **Validar conexión.** (*Solo aprovisionamiento*: apagado.)

## 2. Importación y revisión del gemelo

### AWS
1. Importar servidores (todas las regiones con servidores). Todo entra
   `adopted_readonly`, con entorno marcado «Importado de AWS».
2. En el reporte de la importación, mirar **«Sin acceso SSM»**: esos
   servidores no tienen agente o instance profile de SSM. **No se les
   agrega** (sería modificarlos): su gemelo se captura como el de Cloudpepper
   (archivo, abajo).
3. Por cada servidor con SSM: el gemelo se captura en la misma importación;
   después, «Capturar gemelo (solo lectura)» para re-capturar.

### Cloudpepper (PCM no se conecta: ni API ni SSH)
1. En cada servidor, con tu acceso habitual:
   `sudo bash pcm_readonly_discovery.sh > gemelo-<servidor>.txt`
   (el script está en `primate_cloud_manager/data/`; solo lee, no escribe nada).
2. PCM → Inventario → **Importar servidor de Cloudpepper** → nombre del
   servidor + archivo. Queda marcado «Importado de Cloudpepper».

### Revisión del gemelo (por instancia, pestaña «Gemelo»)
- [ ] Cada Odoo del servidor aparece como instancia propia y con su base.
- [ ] Repos en el orden del `addons_path`, con remote, rama y SHA.
- [ ] **ALERTAS de cambios locales** (archivos sin commitear / commits sin
      pushear): la mudanza por git LOS PERDERÍA. Resolver con el dueño del
      repo ANTES de mudar (commitear y pushear en origen, nunca desde PCM).
- [ ] Odoo core/enterprise con commit, Python y `pip freeze`.
- [ ] Base (versión de PostgreSQL y tamaño) y filestore (ruta y tamaño).
- [ ] nginx: dominios y vencimiento de certificados.
- [ ] Crons del sistema.
- [ ] Clasificar cada instancia en su proyecto (cliente).

## 3. Verificación de que lo importado no tiene acciones

- [ ] En entornos, servidores e instancias importados se ve el banner
      **IMPORTADO — SOLO LECTURA** y **ningún** botón que modifique (solo
      «Capturar gemelo»).
- [ ] Bitácora: cualquier intento queda como «Bloqueo de solo lectura».
- [ ] El grupo «PCM: operaciones destructivas» está **vacío** (lo asignás vos,
      a mano, recién cuando haga falta; ni así habilita nada sobre importados).

## 4. Montaje de Barraca Julia (recién ahora, con la cuenta de APROVISIONAMIENTO)

1. Crear el usuario IAM **`pcm-prod-prov`** con `docs/iam/pcm-prod-provision.json`
   (`<ZONE_ID>` = zona real del dominio; `<NOMBRES_DNS_DE_PCM>` = el dominio
   de Barraca Julia, ej. `barraca-julia.primate.cloud`). Verificar en el
   simulador: `StopInstances` sobre una instancia importada → **implicitDeny**;
   `ChangeResourceRecordSets` con un nombre que no es de PCM → **implicitDeny**.
2. En PCM: cuenta nueva «Prod — aprovisionamiento» con las claves de
   `pcm-prod-prov` y **Solo aprovisionamiento** marcado. Validar conexión.
3. **Zona real**: `Hosted Zone ID` copiado de Route 53 → Hosted zones. Al
   confirmar el wizard, PCM valida que la zona exista en la cuenta y, si no,
   corta con «La zona Route53 X no existe en la cuenta Y» **antes** de crear
   nada (guardia NoSuchHostedZone).
4. **Tamaño**, a partir del gemelo de la instancia de origen:
   - Disco (GB) ≥ `3 × base + 2 × filestore + 20` (dump + restaurado +
     filestore + sistema). Redondear para arriba.
   - Instancia: base < 5 GB → `t3.large` (8 GB RAM); más grande o con mucho
     tráfico → `t3.xlarge`. `workers` > 0 en producción.
5. Crear entorno con **Emitir HTTPS al terminar** y **Crear registro DNS**
   prendidos. Qué mirar en el progreso / chatter:
   - **DNS** se crea ANTES del install (la propagación corre en paralelo).
   - **INSYNC**: Route 53 confirma el cambio (unos segundos a ~2 min).
   - **Propagación**: `dig` contra los 4 NS autoritativos de la zona debe
     devolver la IP del servidor; si existe un **AAAA** ajeno, corta con
     mensaje claro (borrar ese AAAA y reintentar).
   - **certbot**: solo corre con la propagación confirmada (los límites de
     Let's Encrypt no se gastan esperando). Debe decir «HTTPS activo y
     verificado».
   - **Verificación real**: https responde con certificado válido y http
     redirige. `web.base.url` queda en https.
   - Asegurar `pcm.ssl_use_le_staging` = **False** (si quedó en True de un
     ensayo, el certificado sería de prueba y el navegador lo rechaza).
6. Restore de la base completa + filestore de Barraca Julia sobre la
   instancia nueva (la base recién creada; si pisara una base registrada que
   no es staging, PCM pide la triple confirmación).

### Plan B si SSL falla
La instancia queda **activa en HTTP** (nunca rota): badge «Sin HTTPS» con el
motivo. Corregir la causa (DNS sin propagar, AAAA ajeno, puerto 443) y
**«Activar HTTPS ahora»**. El reintento automático corre cada 10 min (solo
gasta intento de certbot con la propagación confirmada; tope 6 intentos y
24 h esperando propagación).

### Rollback limpio de lo nuevo (sin tocar nada importado)
- Falló el install con la EC2 creada → «Reintentar instalación» (reusa la
  EC2, no crea otra).
- Hay que deshacer todo → con el grupo destructivo asignado a vos: terminar
  el servidor nuevo (triple confirmación: impacto → nombre exacto → tu
  contraseña/2FA) y borrar su registro DNS (ídem). IAM impide igual tocar lo
  importado: `pcm-prod-prov` solo opera recursos con `primate:managed_by=pcm`
  y solo los nombres DNS de PCM.
- Al terminar, quitarte el grupo destructivo.
