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

    @staticmethod
    def _normalize_record(record_set, hosted_zone_id):
        """Normaliza un ResourceRecordSet (incluye registros de tipo alias)."""
        values = [r.get("Value") for r in record_set.get("ResourceRecords", [])]
        if not values and record_set.get("AliasTarget"):
            # Registros alias: el valor está en AliasTarget.DNSName.
            values = [record_set["AliasTarget"].get("DNSName", "").rstrip(".")]
        return {
            "name": record_set["Name"].rstrip("."),
            "record_type": record_set.get("Type"),
            "ttl": record_set.get("TTL", 300),
            "record_value": ", ".join(v for v in values if v),
            "hosted_zone_id": hosted_zone_id,
        }
