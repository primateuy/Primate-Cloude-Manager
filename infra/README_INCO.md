# Permisos de PCM en la cuenta AWS — guía para revisión

**PCM (Primate Cloud Manager)** es el módulo de Odoo con el que Primate
gobierna sus servidores Odoo en AWS: inventario, montaje de servidores nuevos,
restauración de bases, HTTPS y costos. Este paquete trae todo lo que PCM
necesita en la cuenta, para revisarlo y desplegarlo sin el resto del código:

| Archivo | Qué es |
|---|---|
| `pcm-prerequisitos.yaml` | Plantilla de CloudFormation (us-east-1): crea todos los recursos de abajo. |
| `pcm-ssm-readonly.yaml` | Plantilla mínima: sólo el documento SSM de lectura, para otras regiones con servidores a inventariar. |
| `verificar.sh` | Verificación de solo lectura para CloudShell, después del despliegue. |
| `README_INCO.md` | Este documento. |

Ninguno contiene secretos, números de cuenta, IPs ni nombres de clientes:
todo sale de los pseudo-parámetros de la cuenta donde se despliega.

**En corto:**

- **Probada desplegada.** Esta misma versión de la plantilla se desplegó en
  una **cuenta de pruebas** (con el parámetro `Sufijo`) y se usó para un
  **montaje y una importación reales** con los dos usuarios que crea (§0).
- **La escritura en Route 53 es opcional y viene apagada.** Con los
  parámetros `ZonaDns`/`NombresDns` vacíos (el default) PCM no escribe en
  Route 53. Si se completan, queda acotada a **una zona** y a los nombres de
  **un subdominio** (ej. `*.erp.ejemplo.com`), sólo registros A (§2).
- **La plantilla pesa ~81 KB**: se sube **por consola o desde S3**, no con
  `--template-body` (tope 51.200 bytes; §9).

---

## 0. Cómo se probó

En una cuenta de pruebas, con el stack desplegado (`Sufijo` de prueba) y las
access keys de los dos usuarios cargadas en PCM:

| Qué | Usuario | Resultado |
|---|---|---|
| Montaje de un servidor nuevo: red, EC2 con la llave, **Elastic IP**, instalación de Odoo | `pcm-prod-prov` | OK, sin AccessDenied |
| HTTPS (Let's Encrypt) sobre `<nombre>.<ip>.sslip.io` | `pcm-prod-prov` | OK |
| Subida y restore de un dump desde `pcm-dumps-<cuenta>` (el dump se borra al terminar) | `pcm-prod-prov` | OK |
| Importación de solo lectura en tres regiones: el servidor de PCM no se importa; los detenidos y las regiones sin documento quedan con su motivo | `pcm-prod-ro` | OK, sin AccessDenied |
| Documento de lectura (3 pasos) sobre el servidor nuevo | `pcm-prod-ro` | OK, gemelo completo |
| Registro A en Route 53 (zona de prueba, nombres acotados) | `pcm-prod-prov` | OK |
| Terminación del servidor y liberación de su Elastic IP | `pcm-prod-prov` | OK |

`verificar.sh` en esa cuenta: todo OK salvo la prueba 2 («hay recursos con la
llave»), esperable porque la cuenta de pruebas ya tenía servidores de PCM. En
una cuenta sin servidores de PCM tiene que dar **TODO OK**.

Durante la prueba hizo falta **un** ajuste de permisos
(`ec2:DescribeInternetGateways`, para revisar la red antes de montar), ya
incluido en esta versión.

---

## 1. El modelo de seguridad

**La etiqueta es la llave.** Todo lo que PCM crea nace con la etiqueta
`primate:managed_by = pcm`, puesta **en el mismo llamado** que crea el recurso
(instancia y su volumen, security group, Elastic IP). Todos los permisos de
escritura de PCM exigen esa etiqueta, en el pedido o en el recurso. Nadie
puede ponérsela o sacársela a algo que ya existe: hay un **Deny explícito**
(con cualquier combinación de mayúsculas, porque IAM compara así las claves).
Resultado: lo que ya estaba en la cuenta queda fuera del alcance de PCM.

**Dos usuarios, a propósito:**

| Usuario | Policy | Para qué |
|---|---|---|
| `pcm-prod-ro` | `pcm-prod-readonly` | Leer el inventario (EC2, RDS, Route 53, CloudWatch, costos) y correr **sólo** el documento SSM de lectura en los servidores existentes. |
| `pcm-prod-prov` | `pcm-prod-provision` | Montar servidores **nuevos** (siempre con la llave) y operar **sólo** esos. |

No se juntan en un usuario porque IAM evalúa `ssm:SendCommand` **por
recurso** (el documento y la instancia por separado). Juntas, una policy
autorizaría el documento de shell y la otra la instancia ajena: el resultado
sería un shell arbitrario sobre un servidor que PCM no montó. Separados, cada
usuario es seguro solo.

**Los Deny son la segunda red.** En la policy de lectura, los Deny dejan
intocable lo que no tiene la llave aunque alguien le sumara después otra
policy al usuario. Esas acciones ya estarían prohibidas igual, porque no hay
ningún Allow de escritura.

**Tercera red, en PCM.** Además de IAM, PCM bloquea en código (antes de
llamar a AWS) cualquier escritura sobre un recurso importado, y los
servidores importados no muestran ningún botón que los modifique.

Los usuarios se crean **sin acceso a la consola y sin access keys**. Las keys
las crea un administrador en la consola y las carga en PCM (cifradas).

---

## 2. Qué crea la plantilla

| Recurso | Nombre | Para qué |
|---|---|---|
| Documento SSM | `pcm-readonly-discovery` | El **único** comando que PCM puede correr en un servidor que no montó. Script fijo, sin parámetros, de solo lectura (ver §5). |
| Rol IAM + instance profile | `pcm-ssm-role` | El rol que llevan las EC2 que **monta** PCM: agente SSM (`AmazonSSMManagedInstanceCore`; PCM no usa SSH) y lectura/escritura de dumps y backups en los buckets de PCM de esta cuenta. Los servidores existentes no lo usan. |
| Bucket S3 | `pcm-dumps-<cuenta>` | Dumps a restaurar. Privado, Block Public Access completo, cifrado SSE-S3, **sin versionado**, sólo TLS. Lo que está en `incoming/` se borra solo a los 7 días; los multipart incompletos, a 1 día. Política **Retain**: borrar el stack no borra el bucket. |
| Managed policy | `pcm-prod-readonly` | Ver §3. |
| Managed policy | `pcm-prod-provision` | Ver §4. |
| Usuario IAM | `pcm-prod-ro` | Sólo `pcm-prod-readonly`. |
| Usuario IAM | `pcm-prod-prov` | Sólo `pcm-prod-provision`. |

**Convención de buckets:** todos los buckets de PCM se llaman
`pcm-<uso>-<cuenta>` (`pcm-dumps-<cuenta>` para dumps, `pcm-backups-<cuenta>`
para backups y pre-backups). Los permisos de S3 se limitan a ese patrón **y** a
buckets de esta cuenta (`aws:ResourceAccount`). Lo segundo evita que otra
cuenta registre primero un bucket con ese nombre y reciba nuestros backups.
PCM valida el nombre antes de usarlo.

**Parámetro `Sufijo`** (opcional, vacío en producción): se agrega a todos los
nombres (por ejemplo `-cfn`). Sirve para probar el stack en una cuenta que ya
tiene recursos con estos nombres, sin tocarlos.

**Parámetros `ZonaDns` y `NombresDns`** (opcionales, van juntos):

- **Vacíos** (recomendado mientras no se decida otra cosa): PCM **no toca
  Route 53**. Los servidores se publican con `<nombre>.<ip>.sslip.io` y el
  dominio definitivo lo crea un administrador a mano en la consola; PCM sólo
  verifica que resuelva a la IP del servidor antes de usarlo.
- **Completos**: `ZonaDns` es el Hosted Zone ID donde PCM puede crear
  registros (ej. `Z0123456789ABC`) y `NombresDns` la lista de nombres que
  puede crear, actualizar o borrar ahí (minúsculas, separados por coma, con
  comodín: ej. `*.erp.ejemplo.com`, es decir, los nombres de un subdominio).
  Sólo registros **A**, sólo esos nombres, sólo esa zona.
- En los dos casos, pasar un servidor de `sslip.io` a su dominio definitivo
  **no corta el servicio**: PCM monta el dominio nuevo en paralelo (nginx +
  certificado + verificación) y recién con todo verificado retira el de
  sslip.io. Si algo falla, el servidor sigue en sslip.io con su HTTPS.
- Dos reglas de la plantilla rechazan el despliegue si se completa uno solo de
  los dos.

---

## 3. `pcm-prod-readonly` — cada permiso y por qué

| Statement | Efecto | Qué permite o prohíbe | Por qué |
|---|---|---|---|
| `LecturaInventario` | Allow | `Describe*`/`List*`/`Get*` de EC2, RDS, Route 53, CloudWatch; `ce:GetCostAndUsage` y `ce:GetCostForecast`; lectura de comandos SSM y documentos | El inventario, las métricas y los costos. Nada escribe. |
| `DiscoverySoloConElDocumentoPropio` | Allow | `ssm:SendCommand` con el documento `pcm-readonly-discovery` | El inventario de adentro de los servidores. **Ningún otro documento** está permitido. |
| `MaquinasNoCreadasPorPCMIntocables` | Deny | Ciclo de vida, metadata, perfil IAM, discos, snapshots, Elastic IP, reglas de security group y `DeleteTags` sobre lo que no tiene la llave | Segunda red. |
| `EtiquetasDeLoAjenoIntocables` | Deny | Etiquetar recursos ajenos (salvo dentro de una creación) | Segunda red. |
| `BasesRdsNoCreadasPorPCMIntocables` | Deny | Modificar, borrar, reiniciar, parar, restaurar o etiquetar RDS ajenas | Segunda red. |
| `SinSesionesNiAutomatizacionesNiDocumentos` | Deny | Sesiones SSM, automatizaciones, crear/editar documentos, parámetros, asociaciones; crear o borrar zonas de Route 53; Route 53 Domains, Organizations, Account; toda escritura de IAM | Nada de esto lo necesita PCM. |
| `NadieTocaLaLlaveDePropiedadEnRecursosExistentes` | Deny | `CreateTags`/`DeleteTags` con la clave `primate:managed_by` fuera de una creación | La llave no se fabrica. |
| `NuncaBorrarEtiquetas` | Deny | Todo `DeleteTags` | Un `DeleteTags` sin claves borraría la llave sin nombrarla. |

---

## 4. `pcm-prod-provision` — cada permiso y por qué

| Statement | Efecto | Qué permite | Por qué |
|---|---|---|---|
| `LecturasParaOperarLoPropio` | Allow | `Describe*` de EC2, lectura de Route 53, CloudWatch y de comandos SSM, `iam:GetInstanceProfile` | Montar y seguir el estado de lo propio. Nada escribe. Sin costos (los trae la cuenta de lectura). |
| `AmiPublicaDeCanonical` | Allow | `ssm:GetParameter` **sólo** sobre `/aws/service/canonical/ubuntu/*` | Resolver la AMI oficial de Ubuntu. No lee ningún parámetro ni secreto de la cuenta. |
| `CrearInstanciaSoloEtiquetadaPorPCM` | Allow | `RunInstances` si la instancia y su volumen nacen con la llave | Montar servidores nuevos. |
| `RecursosAuxiliaresDeRunInstances` | Allow | Lo que `RunInstances` usa sin crear: AMI, subred, interfaz, security group, key pair | Requisito de `RunInstances`. |
| `EtiquetarSoloAlCrear` | Allow | `CreateTags` sólo dentro de una creación | Las etiquetas viajan en el mismo llamado. |
| `CicloDeVidaSoloDeLoDePCM` | Allow | Arrancar, parar, reiniciar, terminar y modificar **sólo** instancias con la llave | Operar lo propio. Terminar exige en PCM una confirmación en 3 pasos. |
| `SecurityGroupPropioEtiquetado` / `SecurityGroupEnLaVpc` | Allow | Crear un security group con la llave, dentro de una VPC existente | El SG `pcm-managed` (80/443). |
| `ReglasSoloEnElSecurityGroupDePCM` | Allow | Abrir puertos sólo en security groups con la llave | Nunca en uno ajeno. |
| `ElasticIpPropia` / `AsociarEipSoloEntreRecursosDePCM` | Allow | Reservar una Elastic IP **con** la llave y asociarla sólo entre recursos con la llave | IP fija de cada servidor nuevo: PCM la reserva por defecto en el montaje, para que un stop/start no cambie la IP (el dominio y el certificado dependen de ella). |
| `AsociarEipEnLaInterfazDeLaMaquina` | Allow | `AssociateAddress` sobre la interfaz de red de la máquina | AWS también evalúa la interfaz de red al asociar por instancia. Solo no alcanza para nada: la Elastic IP y la máquina tienen que tener la llave (statement anterior). |
| `LiberarEipSoloDePCM` | Allow | `ReleaseAddress` sólo de Elastic IPs con la llave | Al terminar un servidor de PCM se libera su IP fija (una EIP sin usar se sigue cobrando). Nunca una ajena. |
| `PasarSoloElRolDeSSMDePCM` | Allow | `iam:PassRole` sólo de `pcm-ssm-role` y sólo hacia EC2 | La EC2 nueva lleva ese rol y ningún otro. |
| `ShellDeInstalacionDocumento` + `ShellDeInstalacionSoloEnMaquinasDePCM` | Allow | `ssm:SendCommand` con `AWS-RunShellScript` **sólo** sobre instancias con la llave | Instalar y operar Odoo en los servidores nuevos. Nunca en uno ajeno. |
| `DnsSoloNombresDePCMEnLaZonaReal` | Allow | **Sólo si se completan `ZonaDns`/`NombresDns`**: `ChangeResourceRecordSets` en esa zona, sólo registros A, sólo esos nombres | Crear el registro del dominio de un servidor nuevo. Con los parámetros vacíos este statement no existe. |
| `CrearBucketsDePCM` | Allow | Crear buckets `pcm-<uso>-<cuenta>` | Bucket de backups. |
| `BucketsDePCMDeEstaCuenta` / `ObjetosDeLosBucketsDePCM` | Allow | Listar, lifecycle, leer y borrar objetos en esos buckets, sólo de esta cuenta | Backups, retención y limpieza del dump ya restaurado. |
| `SubirDumpsSoloAlBucketDePCM` | Allow | `PutObject` sólo en `incoming/` de `pcm-dumps-<cuenta>`, de esta cuenta | Subir el dump a restaurar con este mismo usuario. |
| `NadieTocaLaLlaveDePropiedadEnRecursosExistentes` / `NuncaBorrarEtiquetas` | Deny | Ídem readonly | La llave no se fabrica ni se borra. |

---

## 5. Qué NO puede hacer PCM con estos permisos

- **Nada sobre lo que ya existe en la cuenta**: ni arrancar, parar, terminar,
  modificar, etiquetar ni correr comandos (salvo el documento de lectura).
- **Ninguna escritura en Route 53 con `ZonaDns` vacío** (el montaje usa
  `<nombre>.<ip>.sslip.io` y el dominio real lo crea un administrador). Con
  `ZonaDns`/`NombresDns` completos, sólo registros A de esos nombres en esa
  zona: ni otros tipos, ni otras zonas, ni crear o borrar zonas.
- **No libera ni mueve Elastic IPs ajenas**: sólo las que nacieron con la llave.
- **No amplía discos.** No tiene `ec2:ModifyVolume`: lo hace un administrador
  (procedimiento: `modify-volume` + `growpart` + `resize2fs`, sin reinicio).
- **Nada de IAM** (salvo pasar `pcm-ssm-role` a una EC2 nueva), ni sesiones
  SSM, ni automatizaciones, ni documentos SSM nuevos.
- **No lee parámetros ni secretos de la cuenta** (sólo el parámetro público de
  la AMI de Ubuntu).
- **No borra etiquetas**, ni siquiera las propias.
- **No sale de los buckets `pcm-<uso>-<cuenta>` de esta cuenta.**

---

## 6. Limitaciones conocidas

- **Rol compartido entre servidores de PCM.** `pcm-ssm-role` es uno solo para
  todas las EC2 que monta PCM. Cualquiera de esos servidores puede leer los
  dumps y backups de los demás clientes que estén en los buckets de PCM. No
  afecta a los servidores existentes, que no lo usan. Opciones evaluadas para
  más adelante: un prefijo por instancia con condición en la policy, o URLs
  prefirmadas generadas por PCM para cada transferencia.
- **Inventario dentro de servidores grandes.** La salida de cada paso de un
  comando SSM se lee hasta 24.000 caracteres. Por eso el documento va en tres
  pasos (inventario, gemelo y paquetes de Python), cada uno con su tope, y el
  gemelo sale **un registro por instancia**: en un servidor con muchas
  instancias Odoo (medido: más de ~7 con 25 repos cada una) entra lo que
  cabe y PCM marca el resto como **«gemelo incompleto»**, visible y con el
  motivo. Lo que falta se completa por otra vía.
- **Elastic IP por servidor.** Cada servidor nuevo lleva su Elastic IP (con
  la llave), así la IP no cambia con un stop/start. Si un servidor se borra
  por fuera de PCM, su Elastic IP queda reservada (y cobrándose) hasta que un
  administrador la libere.

---

## 7. Costos que genera PCM en la cuenta

- **Cost Explorer.** `GetCostAndUsage` y `GetCostForecast` cuestan US$0,01 por
  llamada. PCM hace **3 llamadas por actualización**: mes actual, mes
  anterior y proyección. La actualización corre **una vez por día** (cron a
  las 04:45 UTC) y a pedido, con una guarda de 6 horas entre consultas. La
  hace **sólo la cuenta de lectura**. Del orden de **US$0,90 por mes**, más
  las consultas manuales.
- **CloudWatch.** `GetMetricData` de las métricas de los servidores: US$0,01
  cada 1.000 métricas consultadas. Centavos por mes.
- **IPv4 pública / Elastic IP.** AWS cobra US$0,005 por hora cada IPv4
  pública (unos US$3,60 por mes), sea Elastic IP o la IP automática de la
  instancia: la Elastic IP asociada no agrega costo frente a la automática.
  Una Elastic IP **sin asociar** también se cobra: PCM la libera al terminar
  el servidor.
- **S3.** Lo que ocupen los dumps (en `incoming/` se borran solos a los 7
  días) y los backups que se configuren.
- **SSM Run Command, IAM y CloudFormation:** sin costo.

---

## 8. El documento de lectura, en los servidores

`pcm-readonly-discovery` es un script fijo, en **tres pasos** (inventario,
gemelo y paquetes de Python de cada entorno virtual), que lista los Odoo del
servidor:
el conf con los valores sensibles tapados, el `addons_path`, la unit de
systemd, el commit del core, los repos git y su estado, el venv de Python
(`pip freeze`), la versión de PostgreSQL y el tamaño de la base, el tamaño del
filestore, nginx con sus certificados, y los crontabs con los secretos tapados.

- **No escribe nada** en el servidor: ni archivos, ni temporales, ni índices de
  git, ni `.pyc`. Todo sale por la salida estándar.
- Corre **`du`** sobre cada filestore y **`pip freeze`** por venv. En un
  servidor con filestores grandes eso lee mucho disco durante unos minutos:
  **conviene correrlo fuera de horario.**

---

## 9. Cómo desplegar

1. CloudShell (us-east-1), solo lectura, antes de desplegar:

   ```bash
   # Ninguno de estos nombres puede existir ya (si existe, el stack falla al crearlo):
   aws iam get-role --role-name pcm-ssm-role                  2>&1 | tail -1
   aws iam get-instance-profile --instance-profile-name pcm-ssm-role 2>&1 | tail -1
   aws iam get-user --user-name pcm-prod-ro                   2>&1 | tail -1
   aws iam get-user --user-name pcm-prod-prov                 2>&1 | tail -1
   aws ssm describe-document --name pcm-readonly-discovery    2>&1 | tail -1
   aws s3api head-bucket --bucket pcm-dumps-$(aws sts get-caller-identity --query Account --output text) 2>&1 | tail -1
   ```

   Esperado: `NoSuchEntity`, `InvalidDocument` o `Not Found` en todos. Si algo
   existe: no se pisa; se decide antes (o se usa el parámetro `Sufijo`).

   **Tamaño de la plantilla.** `pcm-prerequisitos.yaml` pesa **~81 KB** (el
   documento SSM lleva el script de lectura completo): se sube **por consola**
   (*Upload a template file*) **o desde S3** (*Amazon S3 URL*, o
   `--template-url` por CLI), **no con `--template-body`**, cuyo tope es
   51.200 bytes. **Por consola el tope no aplica**: al elegir *Upload a template
   file*, CloudFormation sube la plantilla a un bucket S3 propio
   (`cf-templates-<algo>-<región>`), con tope de 1 MB, y la valida ahí mismo
   (un error de sintaxis aparece antes de crear nada). **Ese bucket lo crea
   CloudFormation, no PCM**, y queda en la cuenta: se puede borrar después.
2. CloudFormation → **Create stack** → *Upload a template file* →
   `pcm-prerequisitos.yaml` → nombre `pcm-prerequisitos` → **Sufijo vacío** →
   **ZonaDns y NombresDns vacíos** (salvo que se haya decidido que PCM cree el
   DNS: ver §2) → tildar *I acknowledge that AWS CloudFormation might create IAM resources
   with custom names* → Create. La plantilla se niega a desplegarse fuera de
   us-east-1.
3. Otras regiones con servidores a inventariar: un stack con
   `pcm-ssm-readonly.yaml` en cada una. Pesa **~56 KB**: también se sube por
   consola o desde S3, no con `--template-body`.
4. CloudShell: `bash verificar.sh` → tiene que terminar en **TODO OK**. Prueba,
   con el simulador de IAM (sin ejecutar nada), que la cuenta de lectura no
   modifica nada; que la de montaje no toca lo que no tiene la llave, crea
   sólo con la llave, reserva y libera sólo Elastic IPs propias y sube dumps
   sólo a su bucket; y, si el stack tiene `ZonaDns`, que sólo crea registros A
   con los nombres permitidos en esa zona. Sin `ZonaDns` informa «Route 53:
   sin zona configurada (PCM no crea DNS; dominio a mano)», que no es un error.
5. Access keys de `pcm-prod-ro` y `pcm-prod-prov` (IAM → Users → Security
   credentials → Create access key → *Application running outside AWS*) para
   cargarlas en PCM.

## 10. Cómo deshacer

1. Borrar en la consola las **access keys** de `pcm-prod-ro` y `pcm-prod-prov`
   (CloudFormation no borra un usuario que tiene keys creadas a mano).
2. Terminar las EC2 montadas por PCM (o quitarles el instance profile
   `pcm-ssm-role`), si se quiere borrar también el rol. Si se terminan desde
   PCM, PCM libera sus Elastic IPs; si se terminan desde la consola, liberar
   a mano las Elastic IPs con la etiqueta `primate:managed_by = pcm`.
3. CloudFormation → el stack `pcm-prerequisitos` → **Delete**. Se borran el
   documento SSM, el rol, el instance profile, las dos policies y los dos
   usuarios.
4. El bucket `pcm-dumps-<cuenta>` **queda** (Retain): se vacía y se borra a
   mano si ya no se quiere. Los buckets de backups que haya creado PCM
   tampoco son del stack.
