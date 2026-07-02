# -*- coding: utf-8 -*-
"""Adaptador RDS: inventario (Fase 2) y creación de bases (Fase 4)."""
import time

# Estado terminal esperado tras crear una RDS.
RDS_AVAILABLE_STATE = "available"
RDS_FAILED_STATES = {"failed", "incompatible-parameters", "incompatible-restore"}

# Motor gestionado por el módulo (Odoo corre sobre PostgreSQL).
RDS_ENGINE = "postgres"


class AwsRdsService:
    """Operaciones sobre RDS: lectura y creación de instancias PostgreSQL."""

    def __init__(self, base):
        """Args:
            base (AwsBaseService): servicio base autenticado (inyección).
        """
        self._base = base

    def list_instances(self, region=None):
        """Lista las instancias RDS de la región.

        Returns:
            list[dict]: bases normalizadas (ver :meth:`_normalize_db`).
        """
        client = self._base.get_client("rds", region=region)
        paginator = client.get_paginator("describe_db_instances")
        result = []
        for page in paginator.paginate():
            for db in page.get("DBInstances", []):
                result.append(self._normalize_db(db, region))
        return result

    def get_instance(self, identifier, region=None):
        """Devuelve una RDS puntual normalizada, o None si no existe."""
        client = self._base.get_client("rds", region=region)
        response = client.describe_db_instances(DBInstanceIdentifier=identifier)
        for db in response.get("DBInstances", []):
            return self._normalize_db(db, region)
        return None

    # ------------------------------------------------------------------
    # Creación de instancias RDS (Fase 4). Devuelve la base normalizada.
    # ------------------------------------------------------------------
    def create_instance(
        self,
        identifier,
        instance_class,
        storage_gb,
        master_username,
        master_password,
        tags,
        engine_version=None,
        multi_az=False,
        backup_retention_days=7,
        region=None,
        wait=True,
        timeout=1200,
        poll_interval=20,
        _sleep=time.sleep,
    ):
        """Crea una RDS PostgreSQL y, por defecto, espera a que esté ``available``.

        El ``master_password`` se usa solo para la creación: no se persiste en la
        base de Odoo (lo maneja el job que invoca este servicio).

        Args:
            identifier (str): identificador de la instancia RDS.
            instance_class (str): clase (``db.t3.medium``, ...).
            storage_gb (int): almacenamiento inicial en GB.
            master_username (str): usuario maestro.
            master_password (str): contraseña maestra (no se persiste).
            tags (list[dict]): tags boto3 obligatorios (D4).
            engine_version (str, optional): versión PostgreSQL (ej.: ``16.3``).
            multi_az (bool): réplica en segunda zona.
            backup_retention_days (int): días de retención de snapshots (0-35).
            region (str, optional): región.
            wait (bool): si espera el estado ``available``.
            timeout (int): segundos máximos de espera.
            poll_interval (int): segundos entre consultas.
            _sleep (callable): inyectable para testear sin esperas reales.

        Returns:
            dict: la base normalizada (ver :meth:`_normalize_db`).
        """
        client = self._base.get_client("rds", region=region)
        params = {
            "DBInstanceIdentifier": identifier,
            "DBInstanceClass": instance_class,
            "Engine": RDS_ENGINE,
            "AllocatedStorage": storage_gb,
            "MasterUsername": master_username,
            "MasterUserPassword": master_password,
            "MultiAZ": multi_az,
            "BackupRetentionPeriod": backup_retention_days,
            "Tags": tags,
        }
        if engine_version:
            params["EngineVersion"] = engine_version
        client.create_db_instance(**params)
        if not wait:
            return self.get_instance(identifier, region=region)
        return self._wait_available(
            identifier, region, timeout, poll_interval, _sleep
        )

    def _wait_available(self, identifier, region, timeout, poll_interval, _sleep):
        """Espera (polling) a que la RDS llegue a ``available`` y la devuelve.

        Raises:
            RuntimeError: ante un estado de falla o si se agota el tiempo.
        """
        waited = 0
        data = self.get_instance(identifier, region=region)
        while waited < timeout:
            status = (data or {}).get("status")
            if status == RDS_AVAILABLE_STATE:
                return data
            if status in RDS_FAILED_STATES:
                raise RuntimeError(
                    "La RDS %s entró en estado '%s'." % (identifier, status)
                )
            _sleep(poll_interval)
            waited += poll_interval
            data = self.get_instance(identifier, region=region)
        raise RuntimeError(
            "Tiempo de espera agotado esperando 'available' para %s." % identifier
        )

    @staticmethod
    def _normalize_db(db, region):
        """Normaliza la respuesta de DescribeDBInstances."""
        endpoint = db.get("Endpoint") or {}
        return {
            "rds_identifier": db["DBInstanceIdentifier"],
            "name": db["DBInstanceIdentifier"],
            "engine": db.get("Engine"),
            "engine_version": db.get("EngineVersion"),
            "rds_instance_class": db.get("DBInstanceClass"),
            "rds_storage_gb": db.get("AllocatedStorage"),
            "rds_multi_az": db.get("MultiAZ", False),
            "backup_retention_days": db.get("BackupRetentionPeriod"),
            "status": db.get("DBInstanceStatus"),
            "endpoint": endpoint.get("Address"),
            "region": region,
        }
