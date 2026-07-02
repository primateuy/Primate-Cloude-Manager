# -*- coding: utf-8 -*-
"""Helpers de fechas para normalizar respuestas de AWS al formato de Odoo."""
from datetime import datetime, timezone


def to_naive_utc(value):
    """Convierte un datetime (tz-aware o no) a naive UTC, como guarda Odoo.

    boto3 devuelve datetimes tz-aware; Odoo almacena Datetime naive en UTC.

    Args:
        value: ``datetime`` o cualquier cosa (se ignora si no es datetime).

    Returns:
        datetime|bool: datetime naive en UTC, o False si no era un datetime.
    """
    if not isinstance(value, datetime):
        return False
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value
