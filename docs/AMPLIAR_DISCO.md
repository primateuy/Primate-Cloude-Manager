# Ampliar el disco de una instancia montada por PCM (procedimiento manual)

**PCM hoy NO amplía discos.** No tiene la acción (`modify-volume` + `growpart`
+ `resize2fs`) y la policy `pcm-prod-provision` no incluye `ec2:ModifyVolume`.
Está anotado como feature en el `BACKLOG.md`. Mientras tanto, se hace a mano
así. **Sin reinicio y sin parar Odoo**: EBS amplía en caliente.

Quién: la parte de AWS la hace **INCO o Daryl** con una identidad de
administración (no con los usuarios de PCM). La parte de adentro del servidor
se puede hacer desde PCM («Ejecutar comando»), porque el servidor es de PCM.

## Antes

- Un volumen EBS sólo **crece**, nunca se achica.
- Entre una modificación y la siguiente del mismo volumen hay que esperar
  **6 horas**.
- Recomendado: snapshot antes de tocar nada.

## 1. AWS (consola o CloudShell)

```bash
INSTANCIA=i-xxxxxxxxxxxxxxxxx      # «Instance ID» en PCM → Instancias EC2 → el servidor
REGION=us-east-1
NUEVO_GB=150                        # tamaño TOTAL nuevo, no el agregado

# Volumen raíz de la instancia
VOL=$(aws ec2 describe-instances --region $REGION --instance-ids $INSTANCIA \
  --query 'Reservations[0].Instances[0].BlockDeviceMappings[?DeviceName==`/dev/sda1`].Ebs.VolumeId' \
  --output text)
echo "$VOL"

# (recomendado) snapshot antes
aws ec2 create-snapshot --region $REGION --volume-id $VOL \
  --description "antes de ampliar a ${NUEVO_GB} GB"

# Ampliar (gp3 sigue gp3; IOPS y throughput no cambian si no se piden)
aws ec2 modify-volume --region $REGION --volume-id $VOL --size $NUEVO_GB

# Esperar a «optimizing» o «completed» (con «optimizing» ya se puede seguir)
aws ec2 describe-volumes-modifications --region $REGION --volume-ids $VOL \
  --query 'VolumesModifications[0].[ModificationState,Progress]' --output text
```

En consola: EC2 → Volumes → el volumen → **Actions → Modify volume** →
Size → Modify.

## 2. Adentro del servidor

Desde PCM → **Instancias EC2 → el servidor → «Ejecutar comando»** (o una
sesión de SSM, si la hace INCO). Ubuntu 24.04 en instancias Nitro (t3, m7i):
el disco raíz es `/dev/nvme0n1` y la partición `/dev/nvme0n1p1`, ext4.

```bash
lsblk                                    # confirmar: nvme0n1 ya muestra el tamaño nuevo
sudo growpart /dev/nvme0n1 1             # agranda la partición 1 al disco entero
sudo resize2fs /dev/nvme0n1p1            # agranda el filesystem ext4 en caliente
df -h /                                  # tiene que mostrar el tamaño nuevo
```

- Si `lsblk` muestra otro nombre de disco (p. ej. `xvda`), usar ese:
  `growpart /dev/xvda 1` y `resize2fs /dev/xvda1`.
- Si `growpart` dice `NOCHANGE`, el volumen todavía no terminó de crecer:
  esperar a «optimizing» y repetir.
- Si el filesystem fuera XFS (no es el default de Ubuntu): `sudo xfs_growfs /`.

## 3. PCM

Servidor → **«Sincronizar»** para que el inventario muestre el disco nuevo.
