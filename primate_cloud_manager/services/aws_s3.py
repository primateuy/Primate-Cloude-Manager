# -*- coding: utf-8 -*-
"""Adaptador S3: bucket de transferencia para la copia de base (Fase 7).

Servicio puro: recibe un :class:`AwsBaseService` ya autenticado. En el flujo de
staging, el dump viaja origen → S3 → destino mediante ``aws s3 cp`` corrido en
las instancias (vía SSM); este adaptador solo garantiza que el bucket exista y
limpia el objeto al final.
"""


class AwsS3Service:
    """Operaciones mínimas sobre S3 para la transferencia de dumps."""

    def __init__(self, base):
        """Args:
            base (AwsBaseService): servicio base autenticado (inyección).
        """
        self._base = base

    def ensure_bucket(self, bucket, region=None):
        """Crea el bucket si no existe (idempotente). Devuelve el nombre.

        Maneja la particularidad de ``us-east-1`` (no admite
        ``LocationConstraint``).

        Args:
            bucket (str): nombre del bucket.
            region (str, optional): región donde crearlo.

        Returns:
            str: el nombre del bucket.
        """
        client = self._base.get_client("s3", region=region)
        existing = {b["Name"] for b in client.list_buckets().get("Buckets", [])}
        if bucket in existing:
            return bucket
        params = {"Bucket": bucket}
        location = region or getattr(self._base, "region", None)
        if location and location != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": location}
        client.create_bucket(**params)
        return bucket

    def delete_object(self, bucket, key, region=None):
        """Elimina un objeto del bucket (limpieza del dump). Best-effort."""
        client = self._base.get_client("s3", region=region)
        client.delete_object(Bucket=bucket, Key=key)
