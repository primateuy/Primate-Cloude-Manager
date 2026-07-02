# -*- coding: utf-8 -*-
"""Adaptador Route 53: inventario de zonas y registros DNS (global).

Route 53 es un servicio global: no se pasa región.
"""


class AwsRoute53Service:
    """Operaciones sobre Route 53. En Fase 2 solo lectura (zonas y registros)."""

    def __init__(self, base):
        """Args:
            base (AwsBaseService): servicio base autenticado (inyección).
        """
        self._base = base

    def list_zones(self):
        """Lista las zonas alojadas (hosted zones).

        Returns:
            list[dict]: ``[{"id": str, "name": str}, ...]`` (id sin el prefijo).
        """
        client = self._base.get_client("route53")
        paginator = client.get_paginator("list_hosted_zones")
        zones = []
        for page in paginator.paginate():
            for zone in page.get("HostedZones", []):
                zones.append(
                    {
                        "id": zone["Id"].split("/")[-1],
                        "name": zone["Name"].rstrip("."),
                    }
                )
        return zones

    def list_records(self, hosted_zone_id):
        """Lista los registros de una zona.

        Args:
            hosted_zone_id (str): ID de la zona (sin prefijo ``/hostedzone/``).

        Returns:
            list[dict]: registros normalizados (ver :meth:`_normalize_record`).
        """
        client = self._base.get_client("route53")
        paginator = client.get_paginator("list_resource_record_sets")
        records = []
        for page in paginator.paginate(HostedZoneId=hosted_zone_id):
            for record_set in page.get("ResourceRecordSets", []):
                records.append(self._normalize_record(record_set, hosted_zone_id))
        return records

    # ------------------------------------------------------------------
    # Escritura de registros (Fase 4). UPSERT idempotente.
    # ------------------------------------------------------------------
    def create_record(
        self, hosted_zone_id, name, record_type, value, ttl=300, comment=None
    ):
        """Crea o actualiza un registro DNS (acción ``UPSERT``, idempotente).

        Route 53 es global: no se pasa región. ``UPSERT`` evita fallar si el
        registro ya existe (lo deja en el valor indicado).

        Args:
            hosted_zone_id (str): ID de la zona (sin prefijo ``/hostedzone/``).
            name (str): nombre del registro (ej.: ``forum.primate.cloud``).
            record_type (str): tipo (``A``, ``CNAME``, ``TXT``, ``MX``).
            value (str|list[str]): valor(es) del registro.
            ttl (int): TTL en segundos.
            comment (str, optional): comentario del change batch.

        Returns:
            str: el Change Id devuelto por AWS.
        """
        values = [value] if isinstance(value, str) else list(value)
        client = self._base.get_client("route53")
        response = client.change_resource_record_sets(
            HostedZoneId=hosted_zone_id,
            ChangeBatch={
                "Comment": (comment or "primate_cloud_manager")[:256],
                "Changes": [
                    {
                        "Action": "UPSERT",
                        "ResourceRecordSet": {
                            "Name": name,
                            "Type": record_type,
                            "TTL": ttl,
                            "ResourceRecords": [{"Value": v} for v in values],
                        },
                    }
                ],
            },
        )
        return response.get("ChangeInfo", {}).get("Id")

    def delete_record(
        self, hosted_zone_id, name, record_type, value, ttl=300, comment=None
    ):
        """Borra un registro DNS (acción ``DELETE``).

        Route 53 exige el ``ResourceRecordSet`` EXACTO (nombre, tipo, TTL y
        valores) para borrar; se toma del registro ya conocido por PCM. Falla
        con ``InvalidChangeBatch`` si el RRSet no coincide con el real.

        Args:
            hosted_zone_id (str): ID de la zona (sin prefijo ``/hostedzone/``).
            name (str): nombre del registro.
            record_type (str): tipo (``A``, ``CNAME``, ``TXT``, ``MX``).
            value (str|list[str]): valor(es) exactos del registro a borrar.
            ttl (int): TTL exacto del registro.
            comment (str, optional): comentario del change batch.

        Returns:
            str: el Change Id devuelto por AWS.
        """
        values = [value] if isinstance(value, str) else list(value)
        client = self._base.get_client("route53")
        response = client.change_resource_record_sets(
            HostedZoneId=hosted_zone_id,
            ChangeBatch={
                "Comment": (comment or "primate_cloud_manager")[:256],
                "Changes": [
                    {
                        "Action": "DELETE",
                        "ResourceRecordSet": {
                            "Name": name,
                            "Type": record_type,
                            "TTL": ttl,
                            "ResourceRecords": [{"Value": v} for v in values],
                        },
                    }
                ],
            },
        )
        return response.get("ChangeInfo", {}).get("Id")

    def get_change_status(self, change_id):
        """Estado de propagación de un cambio: ``INSYNC`` o ``PENDING``.

        Route 53 confirma un cambio de forma asíncrona: el ``ChangeId`` que
        devuelve ``change_resource_record_sets`` nace ``PENDING`` y pasa a
        ``INSYNC`` al propagar. Se consulta para no mostrar "sincronizado"
        antes de tiempo.

        Args:
            change_id (str): el ``ChangeId`` (ej.: ``/change/C0123...``).

        Returns:
            str: ``"INSYNC"`` / ``"PENDING"`` (o ``None`` si no se pudo leer).
        """
        client = self._base.get_client("route53")
        response = client.get_change(Id=change_id)
        return response.get("ChangeInfo", {}).get("Status")

    @staticmethod
    def _normalize_record(record_set, hosted_zone_id):
        """Normaliza un ResourceRecordSet (incluye registros de tipo alias)."""
        values = [r.get("Value") for r in record_set.get("ResourceRecords", [])]
        is_alias = bool(record_set.get("AliasTarget"))
        if not values and is_alias:
            # Registros alias: el valor está en AliasTarget.DNSName.
            values = [record_set["AliasTarget"].get("DNSName", "").rstrip(".")]
        return {
            "name": record_set["Name"].rstrip("."),
            "record_type": record_set.get("Type"),
            "ttl": record_set.get("TTL", 300),
            "record_value": ", ".join(v for v in values if v),
            "hosted_zone_id": hosted_zone_id,
            # Señal REAL de alias de Route 53 (AliasTarget presente / sin
            # ResourceRecords). NO se infiere del TTL: un alias no trae TTL y un
            # registro simple con TTL 0 es válido — el proxy los confundiría.
            "is_alias": is_alias,
        }
