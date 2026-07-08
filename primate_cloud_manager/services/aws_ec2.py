# -*- coding: utf-8 -*-
"""Adaptador EC2: inventario (Fase 2), acciones (Fase 3) y creación (Fase 4).

Servicio puro: recibe un :class:`AwsBaseService` ya autenticado y devuelve
datos normalizados. No depende del ORM ni escribe en la base.
"""
import logging
import time

from . import aws_discovery

_logger = logging.getLogger(__name__)

# Estados terminales de una instancia EC2 al esperar tras crearla.
EC2_RUNNING_STATE = "running"
EC2_FAILED_STATES = {"shutting-down", "terminated", "stopping", "stopped"}

# Dispositivo raíz por defecto en las AMI de Ubuntu / Amazon Linux.
ROOT_DEVICE_NAME = "/dev/sda1"

# Resolución de AMI Ubuntu 24.04 (Noble) amd64.
# Opción A: parámetro público de SSM publicado por Canonical (1 sola llamada).
UBUNTU_2404_SSM_PARAM = (
    "/aws/service/canonical/ubuntu/server/24.04/stable/current"
    "/amd64/hvm/ebs-gp3/ami-id"
)
# Opción B (fallback): describe_images filtrando por nombre, owner Canonical.
CANONICAL_OWNER_ID = "099720109477"
UBUNTU_2404_NAME_PATTERN = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"


def normalize_ami_id(raw):
    """Devuelve el AMI id pelado, tolerando corchetes/comillas/espacios pegados.

    El valor correcto es ``ami-xxxxxxxx``; si alguien pega ``[ami-xxxx]`` o
    ``'ami-xxxx'`` (como apareció en errores reales de RunInstances), se limpia.

    Args:
        raw (str|None): valor crudo del campo AMI.

    Returns:
        str|None: el id limpio, o el valor original si era falsy.
    """
    if not raw:
        return raw
    # Un solo strip con todos los caracteres a pelar de los extremos: así no
    # importa el orden (ej.: '[ami-123]' con comillas envolviendo corchetes).
    return raw.strip(" \t\r\n[]'\"")


class AwsEc2Service:
    """Operaciones sobre EC2: lectura, ciclo de vida y creación."""

    def __init__(self, base):
        """Args:
            base (AwsBaseService): servicio base autenticado (inyección).
        """
        self._base = base

    def list_instances(self, region=None):
        """Lista todas las instancias EC2 de la región.

        Args:
            region (str, optional): región AWS; usa la de la sesión si falta.

        Returns:
            list[dict]: instancias normalizadas (ver :meth:`_normalize_instance`).
        """
        client = self._base.get_client("ec2", region=region)
        paginator = client.get_paginator("describe_instances")
        instances = []
        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    instances.append(self._normalize_instance(inst, region))
        return instances

    def get_instance(self, instance_id, region=None):
        """Devuelve una instancia puntual normalizada, o None si no existe."""
        client = self._base.get_client("ec2", region=region)
        response = client.describe_instances(InstanceIds=[instance_id])
        for reservation in response.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                return self._normalize_instance(inst, region)
        return None

    # ------------------------------------------------------------------
    # Acciones de ciclo de vida (Fase 3). Devuelven el AWS Request ID.
    # ------------------------------------------------------------------
    def start_instance(self, instance_id, region=None):
        """Arranca una instancia detenida. Devuelve el AWS Request ID."""
        client = self._base.get_client("ec2", region=region)
        response = client.start_instances(InstanceIds=[instance_id])
        return self._request_id(response)

    def stop_instance(self, instance_id, region=None):
        """Detiene una instancia en ejecución. Devuelve el AWS Request ID."""
        client = self._base.get_client("ec2", region=region)
        response = client.stop_instances(InstanceIds=[instance_id])
        return self._request_id(response)

    def reboot_instance(self, instance_id, region=None):
        """Reinicia la instancia in situ (reboot). Devuelve el AWS Request ID.

        Se usa ``reboot_instances`` (reinicio en el mismo host, sin cambio de IP)
        como interpretación de "reiniciar"; es más simple y seguro que un
        stop+start, que reasigna host e IP pública.
        """
        client = self._base.get_client("ec2", region=region)
        response = client.reboot_instances(InstanceIds=[instance_id])
        return self._request_id(response)

    def terminate_instance(self, instance_id, region=None):
        """Termina (elimina) permanentemente la instancia. Devuelve el Request ID."""
        client = self._base.get_client("ec2", region=region)
        response = client.terminate_instances(InstanceIds=[instance_id])
        return self._request_id(response)

    # ------------------------------------------------------------------
    # Creación de instancias (Fase 4). Devuelve la instancia normalizada.
    # ------------------------------------------------------------------
    def create_instance(
        self,
        image_id,
        instance_type,
        tags,
        region=None,
        disk_size_gb=None,
        key_name=None,
        security_group_ids=None,
        subnet_id=None,
        instance_profile=None,
        user_data=None,
        client_token=None,
        wait=True,
        timeout=300,
        poll_interval=10,
        _sleep=time.sleep,
    ):
        """Crea una instancia EC2 y, por defecto, espera a que esté ``running``.

        Pensado para correr dentro de un ``queue_job`` (la espera no bloquea la UI).
        Todo recurso se etiqueta con los tags obligatorios (D4): el ``tags`` que
        llega debe construirse con ``aws_base.build_resource_tags``.

        Args:
            image_id (str): AMI base (``ami-...``).
            instance_type (str): tipo de instancia (``t3.medium``, ...).
            tags (list[dict]): tags boto3 ``[{"Key":..,"Value":..}, ...]`` (incluye Name).
            region (str, optional): región; usa la de la sesión si falta.
            disk_size_gb (int, optional): tamaño del volumen raíz; None deja el de la AMI.
            key_name (str, optional): par de claves SSH.
            security_group_ids (list[str], optional): grupos de seguridad.
            subnet_id (str, optional): subred.
            instance_profile (str, optional): nombre del instance profile (perfil SSM).
            user_data (str, optional): script de arranque (cloud-init).
            client_token (str, optional): token de idempotencia. Con el mismo token,
                AWS NO crea una instancia nueva: devuelve la ya creada. Evita
                duplicados si el job se re-ejecuta (p. ej. tras reiniciar el server).
            wait (bool): si espera el estado ``running`` (True) o vuelve enseguida.
            timeout (int): segundos máximos de espera.
            poll_interval (int): segundos entre consultas de estado.
            _sleep (callable): inyectable para testear sin esperas reales.

        Returns:
            dict: la instancia normalizada (ver :meth:`_normalize_instance`).
        """
        client = self._base.get_client("ec2", region=region)
        params = {
            # Defensa: siempre string limpio (sin corchetes/comillas/espacios).
            "ImageId": normalize_ami_id(image_id),
            "InstanceType": instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            "TagSpecifications": [
                {"ResourceType": "instance", "Tags": tags},
                {"ResourceType": "volume", "Tags": tags},
            ],
        }
        if client_token:
            params["ClientToken"] = client_token
        if disk_size_gb:
            params["BlockDeviceMappings"] = [
                {
                    "DeviceName": ROOT_DEVICE_NAME,
                    "Ebs": {"VolumeSize": disk_size_gb, "DeleteOnTermination": True},
                }
            ]
        if key_name:
            params["KeyName"] = key_name
        if security_group_ids:
            params["SecurityGroupIds"] = security_group_ids
        if subnet_id:
            params["SubnetId"] = subnet_id
        if instance_profile:
            params["IamInstanceProfile"] = {"Name": instance_profile}
        if user_data:
            params["UserData"] = user_data

        response = client.run_instances(**params)
        instance_id = response["Instances"][0]["InstanceId"]
        if not wait:
            return self.get_instance(instance_id, region=region)
        return self._wait_running(
            instance_id, region, timeout, poll_interval, _sleep
        )

    def _wait_running(self, instance_id, region, timeout, poll_interval, _sleep):
        """Espera (polling) a que la instancia llegue a ``running`` y la devuelve.

        Returns:
            dict: instancia normalizada en su último estado conocido.

        Raises:
            RuntimeError: si la instancia entra en un estado terminal inesperado
                o si se agota el tiempo de espera.
        """
        waited = 0
        data = self.get_instance(instance_id, region=region)
        while waited < timeout:
            state = (data or {}).get("instance_state")
            if state == EC2_RUNNING_STATE:
                return data
            if state in EC2_FAILED_STATES:
                raise RuntimeError(
                    "La instancia %s entró en estado '%s' antes de ejecutarse."
                    % (instance_id, state)
                )
            _sleep(poll_interval)
            waited += poll_interval
            data = self.get_instance(instance_id, region=region)
        raise RuntimeError(
            "Tiempo de espera agotado esperando 'running' para %s." % instance_id
        )

    def resolve_ami(self, name_pattern, region=None, owners=None):
        """Resuelve la AMI más reciente que matchea un patrón de nombre (best-effort).

        Conveniencia para prefijar el wizard; el ``image_id`` explícito sigue
        siendo la fuente de verdad. Devuelve None si no encuentra nada.

        Args:
            name_pattern (str): patrón de ``Name`` (ej.: ``ubuntu/images/*24.04*``).
            region (str, optional): región.
            owners (list[str], optional): cuentas propietarias de la AMI.

        Returns:
            str|None: AMI id de la imagen más reciente, o None.
        """
        client = self._base.get_client("ec2", region=region)
        response = client.describe_images(
            Owners=owners or ["amazon"],
            Filters=[
                {"Name": "name", "Values": [name_pattern]},
                {"Name": "state", "Values": ["available"]},
            ],
        )
        images = sorted(
            response.get("Images", []),
            key=lambda img: img.get("CreationDate", ""),
            reverse=True,
        )
        return images[0]["ImageId"] if images else None

    def resolve_ubuntu_ami(self, region=None):
        """Resuelve el último Ubuntu 24.04 (Noble) amd64 para la región dada.

        Opción A (preferida): parámetro público de SSM publicado por Canonical
        (1 llamada). Requiere ``ssm:GetParameter``; si falla (permisos u otro
        error de SSM), NO rompe: cae a la Opción B (``describe_images``, que
        funciona con ``ec2:Describe*``).

        Args:
            region (str, optional): región AWS donde resolver el AMI.

        Returns:
            str|None: el AMI id limpio, o None si ningún método encontró imagen.
        """
        # --- Opción A: parámetro público de SSM ---
        try:
            ssm = self._base.get_client("ssm", region=region)
            value = ssm.get_parameter(Name=UBUNTU_2404_SSM_PARAM)["Parameter"]["Value"]
            ami = normalize_ami_id(value)
            if ami:
                _logger.info("AMI Ubuntu 24.04 resuelta vía SSM: %s (región %s).", ami, region)
                return ami
        except Exception as error:  # noqa: BLE001 - SSM puede no tener permiso
            _logger.warning(
                "No se pudo resolver el AMI vía SSM (%s). Se usa DescribeImages.", error
            )

        # --- Opción B (fallback): describe_images ---
        client = self._base.get_client("ec2", region=region)
        images = client.describe_images(
            Owners=[CANONICAL_OWNER_ID],
            Filters=[
                {"Name": "name", "Values": [UBUNTU_2404_NAME_PATTERN]},
                {"Name": "architecture", "Values": ["x86_64"]},
                {"Name": "state", "Values": ["available"]},
                {"Name": "root-device-type", "Values": ["ebs"]},
            ],
        ).get("Images", [])
        if not images:
            return None
        images.sort(key=lambda img: img.get("CreationDate", ""), reverse=True)
        ami = normalize_ami_id(images[0]["ImageId"])
        _logger.info(
            "AMI Ubuntu 24.04 resuelta vía DescribeImages: %s (región %s).", ami, region
        )
        return ami

    @staticmethod
    def _request_id(response):
        """Extrae el AWS Request ID de la metadata de una respuesta boto3."""
        return (response or {}).get("ResponseMetadata", {}).get("RequestId")

    def create_tags(self, resource_ids, tags, region=None):
        """Aplica (o sobrescribe) tags en recursos EC2 existentes (re-tagging).

        ``ec2:CreateTags`` es idempotente a nivel AWS: fijar una clave con el
        mismo valor es un no-op y no duplica. Acepta varios recursos en un
        request (instancia + volúmenes) → se taggean juntos.

        Args:
            resource_ids (list[str]): ids de recursos EC2 (instancia, volúmenes).
            tags (list[dict]): tags boto3 ``[{"Key":..,"Value":..}, ...]``.
            region (str, optional): región de los recursos.

        Returns:
            str: el AWS Request ID.
        """
        client = self._base.get_client("ec2", region=region)
        response = client.create_tags(Resources=list(resource_ids), Tags=tags)
        return self._request_id(response)

    @staticmethod
    def _error_code(error):
        """Código de error boto3 (``''`` si no aplica)."""
        if hasattr(error, "response"):
            return (error.response or {}).get("Error", {}).get("Code", "")
        return ""

    def ensure_security_group(self, region, vpc_id, tags, description=None):
        """Descubre o CREA el SG gestionado (``pcm-managed``) y completa sus reglas.

        Idempotente: si ya existe (nombre + tag en la VPC) lo reusa; si dos jobs
        lo crean a la vez, el segundo captura ``InvalidGroup.Duplicate`` y reusa
        (nombre único por VPC). Solo toca el SG de PCM. Deja ingress 80/443 desde
        0.0.0.0/0 (idempotente) y el egress abierto por default del SG nuevo.

        Args:
            region (str): región.
            vpc_id (str): VPC donde vive/creará el SG.
            tags (list[dict]): tags boto3 (deben incluir ``primate:managed_by=pcm``).
            description (str, optional): descripción del SG.

        Returns:
            str: GroupId del SG gestionado.
        """
        client = self._base.get_client("ec2", region=region)

        def _find():
            found = client.describe_security_groups(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "group-name", "Values": [aws_discovery.MANAGED_SG_NAME]},
            ]).get("SecurityGroups", [])
            return found[0]["GroupId"] if found else None

        sg_id = _find()
        if not sg_id:
            try:
                sg_id = client.create_security_group(
                    GroupName=aws_discovery.MANAGED_SG_NAME,
                    Description=description or "Primate Cloud Manager managed SG",
                    VpcId=vpc_id,
                    TagSpecifications=[
                        {"ResourceType": "security-group", "Tags": tags}],
                )["GroupId"]
            except Exception as error:  # noqa: BLE001
                if self._error_code(error) != "InvalidGroup.Duplicate":
                    raise
                sg_id = _find()  # otro job lo creó → reusar
        self._authorize_web_ingress(client, sg_id)
        return sg_id

    def _authorize_web_ingress(self, client, sg_id):
        """Abre 80/443 desde 0.0.0.0/0 en el SG (idempotente: ignora duplicados)."""
        for port in aws_discovery.REQUIRED_INGRESS_PORTS:
            try:
                client.authorize_security_group_ingress(
                    GroupId=sg_id,
                    IpPermissions=[{
                        "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                        "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])
            except Exception as error:  # noqa: BLE001
                if self._error_code(error) != "InvalidPermission.Duplicate":
                    raise

    @staticmethod
    def _normalize_instance(inst, region):
        """Convierte la respuesta cruda de AWS a un dict estable para el modelo."""
        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
        # Ids de volúmenes EBS adjuntos (para re-taggearlos junto a la instancia).
        volume_ids = [
            bdm["Ebs"]["VolumeId"]
            for bdm in inst.get("BlockDeviceMappings", [])
            if (bdm.get("Ebs") or {}).get("VolumeId")
        ]
        # Tamaño del volumen raíz: no viene en describe_instances; se deja en 0
        # y se completa en fases posteriores con describe_volumes si hace falta.
        return {
            "aws_instance_id": inst["InstanceId"],
            "name": tags.get("Name") or inst["InstanceId"],
            "instance_state": inst.get("State", {}).get("Name"),
            "instance_type": inst.get("InstanceType"),
            "public_ip": inst.get("PublicIpAddress"),
            "private_ip": inst.get("PrivateIpAddress"),
            "region": region,
            "tags": tags,
            "volume_ids": volume_ids,
            "created_at": inst.get("LaunchTime"),
        }
