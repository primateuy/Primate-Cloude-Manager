# -*- coding: utf-8 -*-
"""Adaptador base para autenticación y sesiones boto3.

Servicio Python **puro**: no depende del ORM de Odoo, no escribe en la base
de datos y no contiene lógica de negocio. Recibe credenciales ya descifradas
y devuelve clientes/datos normalizados. Los modelos lo consumen por inyección,
lo que habilita el testeo unitario con mocks y la futura extensión a otros
proveedores cloud.
"""
import boto3

# Etiquetas mínimas obligatorias en todo recurso AWS creado por el módulo (D4).
# Centralizar acá la construcción de tags sostiene la atribución de costos.
MANAGED_BY_TAG = "primate:managed_by"
MANAGED_BY_VALUE = "pcm"
CLIENT_TAG = "primate:client"
ENVIRONMENT_TAG = "primate:environment"

# Nombre de sesión usado al asumir un rol cruzado en la cuenta del cliente.
ASSUME_ROLE_SESSION_NAME = "primate-cloud-manager"


def build_resource_tags(client, environment, extra=None):
    """Construye la lista de tags obligatoria para un recurso AWS.

    Args:
        client (str): identificador del cliente/proyecto (``primate:client``).
        environment (str): identificador del entorno (``primate:environment``).
        extra (dict, optional): tags adicionales {clave: valor}.

    Returns:
        list[dict]: tags en formato boto3 ``[{"Key": ..., "Value": ...}, ...]``.
    """
    tags = {
        MANAGED_BY_TAG: MANAGED_BY_VALUE,
        CLIENT_TAG: client or "",
        ENVIRONMENT_TAG: environment or "",
    }
    if extra:
        tags.update(extra)
    return [{"Key": key, "Value": str(value)} for key, value in tags.items()]


class AwsBaseService:
    """Servicio base: construye una sesión boto3 y entrega clientes por servicio.

    Soporta los dos métodos de autenticación del módulo (D3):

    - ``access_key``: claves IAM de larga vida (default v1).
    - ``assume_role``: rol IAM cruzado en la cuenta destino vía ``sts:AssumeRole``
      (recomendado para cuentas de cliente; evita guardar secretos persistentes).

    Nunca persiste ni loguea las credenciales: la sesión vive en memoria.
    """

    def __init__(
        self,
        access_key_id=None,
        secret_access_key=None,
        region=None,
        role_arn=None,
        external_id=None,
    ):
        """Inicializa la sesión boto3.

        Args:
            access_key_id (str, optional): Access Key ID del IAM.
            secret_access_key (str, optional): Secret Access Key del IAM.
            region (str, optional): región AWS por defecto.
            role_arn (str, optional): ARN del rol a asumir (activa ``assume_role``).
            external_id (str, optional): External ID exigido por el rol cruzado.
        """
        self.region = region
        if role_arn:
            self._session = self._build_assumed_role_session(
                access_key_id, secret_access_key, region, role_arn, external_id
            )
        else:
            self._session = boto3.Session(
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name=region,
            )

    def _build_assumed_role_session(
        self, access_key_id, secret_access_key, region, role_arn, external_id
    ):
        """Asume un rol IAM y devuelve una sesión con credenciales temporales."""
        if access_key_id:
            base_session = boto3.Session(
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name=region,
            )
        else:
            # Sin claves explícitas: usa la cadena de credenciales del entorno
            # (instance profile de la EC2 que corre Odoo, variables de entorno, etc.).
            base_session = boto3.Session(region_name=region)
        sts = base_session.client("sts")
        params = {"RoleArn": role_arn, "RoleSessionName": ASSUME_ROLE_SESSION_NAME}
        if external_id:
            params["ExternalId"] = external_id
        creds = sts.assume_role(**params)["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )

    def get_client(self, service_name, region=None):
        """Retorna un cliente boto3 para el servicio indicado.

        Args:
            service_name (str): servicio AWS (``ec2``, ``rds``, ``route53``, ...).
            region (str, optional): sobrescribe la región de la sesión.

        Returns:
            botocore.client.BaseClient: cliente boto3 configurado.
        """
        kwargs = {}
        if region:
            kwargs["region_name"] = region
        return self._session.client(service_name, **kwargs)

    def test_connection(self):
        """Verifica que las credenciales sean válidas contra ``sts:GetCallerIdentity``.

        Returns:
            dict: ``{"success": bool, "account_id": str|None, "error": str|None}``.
        """
        try:
            sts = self.get_client("sts")
            identity = sts.get_caller_identity()
            return {"success": True, "account_id": identity["Account"], "error": None}
        except Exception as error:  # noqa: BLE001 - se normaliza el error al modelo
            return {"success": False, "account_id": None, "error": str(error)}
