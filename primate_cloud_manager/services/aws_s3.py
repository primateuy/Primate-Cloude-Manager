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

        Chequea con ``head_bucket``, NO con ``list_buckets``: hallazgo de la
        prueba real de Fase 8 — ListAllMyBuckets suele estar denegado en
        cuentas con permisos mínimos, y head_bucket es además más preciso
        (distingue "no existe" de "existe pero es de otra cuenta"). Maneja la
        particularidad de ``us-east-1`` (no admite ``LocationConstraint``).

        Args:
            bucket (str): nombre del bucket.
            region (str, optional): región donde crearlo.

        Returns:
            str: el nombre del bucket.

        Raises:
            RuntimeError: con mensaje accionable si el bucket es de otra
                cuenta o si falta y la cuenta no puede crear buckets.
        """
        client = self._base.get_client("s3", region=region)
        try:
            client.head_bucket(Bucket=bucket)
            return bucket
        except client.exceptions.ClientError as error:
            code = (error.response.get("ResponseMetadata") or {}).get(
                "HTTPStatusCode")
            if code == 403:
                # 403 es ambiguo en HeadBucket (hallazgo de la prueba real):
                # bucket de otra cuenta O falta s3:ListBucket en la política.
                raise RuntimeError(
                    "Sin acceso al bucket S3 '%s': o pertenece a otra cuenta "
                    "(los nombres son globales) o a la identidad le falta "
                    "s3:ListBucket sobre él (revisá la política IAM)." % bucket
                ) from error
            if code != 404:
                raise
        params = {"Bucket": bucket}
        location = region or getattr(self._base, "region", None)
        if location and location != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": location}
        try:
            client.create_bucket(**params)
        except client.exceptions.ClientError as error:
            if (error.response.get("Error") or {}).get("Code") == "AccessDenied":
                raise RuntimeError(
                    "El bucket S3 '%s' no existe y esta cuenta no puede crear "
                    "buckets (s3:CreateBucket denegado): crealo a mano o "
                    "ampliá la política IAM del usuario." % bucket
                ) from error
            raise
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

    def put_lifecycle_rule(self, bucket, prefix, expiration_days, rule_id=None,
                           region=None):
        """Asegura una regla de expiración por prefijo SIN pisar reglas ajenas.

        ``put_bucket_lifecycle_configuration`` REEMPLAZA la configuración entera
        del bucket: acá se lee la existente, se actualiza/inserta solo la regla
        propia (por su id) y se vuelve a subir todo junto. Con esta regla, AWS
        borra solo los objetos vencidos (retención de la política, Fase 8).

        Args:
            bucket (str): nombre del bucket.
            prefix (str): prefijo de keys que expira (ej.: ``pcm-backups/``).
            expiration_days (int): días de retención.
            rule_id (str, optional): id de la regla; default derivado del prefijo.
            region (str, optional): región.

        Returns:
            str: el id de la regla asegurada.
        """
        client = self._base.get_client("s3", region=region)
        rule_id = rule_id or "pcm-%s" % (prefix or "all").strip("/").replace("/", "-")
        try:
            existing = client.get_bucket_lifecycle_configuration(
                Bucket=bucket
            ).get("Rules", [])
        except client.exceptions.ClientError as error:
            code = (error.response.get("Error") or {}).get("Code")
            if code != "NoSuchLifecycleConfiguration":
                raise
            existing = []
        rules = [rule for rule in existing if rule.get("ID") != rule_id]
        rules.append({
            "ID": rule_id,
            "Status": "Enabled",
            "Filter": {"Prefix": prefix or ""},
            "Expiration": {"Days": int(expiration_days)},
        })
        client.put_bucket_lifecycle_configuration(
            Bucket=bucket, LifecycleConfiguration={"Rules": rules}
        )
        return rule_id

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
