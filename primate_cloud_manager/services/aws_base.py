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
# Claves ESTABLES de atribución (Fase 9, Opción A). Cost Explorer agrupa por
# estas (no por los tags por nombre, que son solo para lectura humana).
CLIENT_ID_TAG = "primate:client_id"
ENVIRONMENT_ID_TAG = "primate:environment_id"

# Nombre de sesión usado al asumir un rol cruzado en la cuenta del cliente.
ASSUME_ROLE_SESSION_NAME = "primate-cloud-manager"


def build_resource_tags(client, environment, client_ref=None,
                        environment_ref=None, extra=None):
    """Construye la lista de tags obligatoria para un recurso AWS.

    Emite los tags por nombre (lectura humana) y, si se pasan, los tags de
    identificador ESTABLE (clave de atribución de costos, Fase 9). Un ``_ref``
    vacío/None NO se emite: un tag de identificador ausente es preferible a uno
    vacío (el recurso queda "sin atribuir" limpio, sin ensuciar la agrupación).

    Args:
        client (str): nombre del cliente/proyecto (``primate:client``).
        environment (str): nombre del entorno (``primate:environment``).
        client_ref (str, optional): ref estable del cliente (``primate:client_id``).
        environment_ref (str, optional): ref estable del entorno
            (``primate:environment_id``).
        extra (dict, optional): tags adicionales {clave: valor}.

    Returns:
        list[dict]: tags en formato boto3 ``[{"Key": ..., "Value": ...}, ...]``.
    """
    tags = {
        MANAGED_BY_TAG: MANAGED_BY_VALUE,
        CLIENT_TAG: client or "",
        ENVIRONMENT_TAG: environment or "",
    }
    # Solo se emiten los tags de id si hay valor (cliente/entorno sin ref =
    # tag ausente, no vacío).
    if client_ref:
        tags[CLIENT_ID_TAG] = client_ref
    if environment_ref:
        tags[ENVIRONMENT_ID_TAG] = environment_ref
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

    # Guarda de solo lectura (regla dura 2026-10-22). La inyecta el lado Odoo
    # (cuenta) con los recursos IMPORTADOS; el servicio sigue puro: solo sabe
    # que, si hay guarda, toda operación NO de lectura pasa por
    # ``guard.check(servicio, operación, params)`` ANTES de salir a AWS.
    guard = None

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
        client = self._session.client(service_name, **kwargs)
        if self.guard is not None:
            return GuardedClient(client, service_name, self.guard)
        return client

    def test_connection(self):
        """Verifica que las credenciales sean válidas contra ``sts:GetCallerIdentity``.

        Returns:
            dict: ``{"success": bool, "account_id": str|None, "arn": str|None,
            "error": str|None}``; ``arn`` es quién respondió (usuario o rol).
        """
        try:
            sts = self.get_client("sts")
            identity = sts.get_caller_identity()
            return {"success": True, "account_id": identity["Account"],
                    "arn": identity.get("Arn"), "error": None}
        except Exception as error:  # noqa: BLE001 - se normaliza el error al modelo
            return {"success": False, "account_id": None, "arn": None,
                    "error": str(error)}

# Prefijos de operaciones boto3 que NO modifican nada (lectura pura).
READ_ONLY_PREFIXES = (
    "describe_", "list_", "get_", "head_", "check_", "lookup_", "search_",
    "test_", "generate_presigned", "can_paginate", "get_paginator",
    "get_waiter", "close",
)


class GuardedClient:
    """Envuelve un cliente boto3: lecturas libres, mutaciones validadas.

    ``guard.check(service, operation, params)`` levanta si la operación toca
    un recurso protegido; si no levanta, la llamada sigue normal.
    """

    def __init__(self, client, service_name, guard):
        self._client = client
        self._service_name = service_name
        self._guard = guard

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if (name.startswith("_") or not callable(attr)
                or name.startswith(READ_ONLY_PREFIXES)):
            return attr

        def guarded(*args, **kwargs):
            self._guard.check(self._service_name, name, kwargs)
            return attr(*args, **kwargs)

        return guarded
