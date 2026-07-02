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

    def list_objects(self, bucket, prefix=None, region=None):
        """Lista objetos del bucket (evidencia de backups en S3, Fase 8).

        Args:
            bucket (str): nombre del bucket.
            prefix (str, optional): filtra por prefijo de key.
            region (str, optional): región.

        Returns:
            list[dict]: ``{"key", "size", "last_modified"}`` (``last_modified``
            tz-aware, como lo entrega boto3).
        """
        client = self._base.get_client("s3", region=region)
        paginator = client.get_paginator("list_objects_v2")
        params = {"Bucket": bucket}
        if prefix:
            params["Prefix"] = prefix
        result = []
        for page in paginator.paginate(**params):
            for obj in page.get("Contents", []):
                result.append({
                    "key": obj["Key"],
                    "size": obj.get("Size"),
                    "last_modified": obj.get("LastModified"),
                })
        return result

    def head_object(self, bucket, key, region=None):
        """Verifica que un objeto exista. Devuelve sus metadatos o None (404).

        Usado por el validador para confirmar que el dump registrado sigue en S3.
        """
        client = self._base.get_client("s3", region=region)
        try:
            response = client.head_object(Bucket=bucket, Key=key)
        except client.exceptions.ClientError as error:
            code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code == 404:
                return None
            raise
        return {
            "key": key,
            "size": response.get("ContentLength"),
            "last_modified": response.get("LastModified"),
        }
