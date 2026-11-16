# -*- coding: utf-8 -*-
"""Helpers de fechas para normalizar respuestas de APIs externas al formato de Odoo.

Los campos ``Datetime`` de Odoo guardan datetimes **naive en UTC**. Las APIs
externas devuelven otra cosa: boto3 (AWS) y PyGithub entregan datetimes
tz-aware; otros endpoints entregan strings ISO-8601 con zona
(``2026-11-16 19:30:06+00:00``). Escribir cualquiera de esos crudo revienta
con «Datetime field expects a naive datetime». Toda fecha que venga de
GitHub, AWS, certificados o cualquier servicio externo pasa por acá antes
de tocar un campo ``Datetime``.
"""
from datetime import datetime, timezone


def to_naive_utc(value):
    """Convierte una fecha externa a datetime naive en UTC, como guarda Odoo.

    Acepta ``datetime`` (tz-aware o naive) o string ISO-8601 (con o sin
    zona horaria; cualquier offset se convierte a UTC antes de descartarlo).

    Args:
        value: ``datetime``, string ISO-8601, o cualquier cosa.

    Returns:
        datetime|bool: datetime naive en UTC, o False si no se pudo
        interpretar como fecha.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
    if not isinstance(value, datetime):
        return False
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value
