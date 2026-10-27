# -*- coding: utf-8 -*-
"""Secretos fuera de queue_job (decisión 2026-10-27).

Hasta acá, los jobs de montaje/instalación/staging guardaban en claro la
contraseña PG transitoria y la del admin del odoo.conf, y los destructivos su
token de un solo uso (en args, kwargs y el func_string que se ve en la UI).

Esta migración limpia los jobs ya guardados:
- los que todavía se pueden reencolar (pendientes, encolados, en curso,
  fallidos): el secreto pasa al depósito cifrado y el job queda con la
  referencia — un reintento sigue funcionando;
- los terminados (done/cancelados): el secreto se reemplaza por
  «[redactado]».
En los dos casos se reescribe el func_string y se tapa el valor si apareció
en el resultado o en el error guardado.
"""
import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.primate_cloud_manager.models.primate_cloud_job_secret import (
    SECRET_KEYS, find_plaintext,
)

_logger = logging.getLogger(__name__)

REENCOLABLES = {"wait_dependencies", "pending", "enqueued", "started", "failed"}
REDACTADO = "[redactado]"


def _valores(value, out):
    """Junta los valores en claro de las claves secretas."""
    if isinstance(value, dict):
        for key, val in value.items():
            if key in SECRET_KEYS and val and isinstance(val, str):
                out.add(val)
            else:
                _valores(val, out)
    elif isinstance(value, (list, tuple)):
        for val in value:
            _valores(val, out)
    return out


def _redactar(value):
    if isinstance(value, dict):
        return {k: (REDACTADO if k in SECRET_KEYS and v and isinstance(v, str)
                    else _redactar(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redactar(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redactar(v) for v in value)
    return value


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Job = env["queue.job"]
    Job = Job.with_context(_job_edit_sentinel=Job.EDIT_SENTINEL)
    Secret = env["primate.cloud.job.secret"]
    limpios = 0
    for job in Job.search([]):
        args, kwargs = list(job.args or []), dict(job.kwargs or {})
        if not (find_plaintext(args) or find_plaintext(kwargs)):
            continue
        claros = _valores(args, set()) | _valores(kwargs, set())
        if job.state in REENCOLABLES:
            args, kwargs = Secret.stash(args), Secret.stash(kwargs)
        else:
            args, kwargs = _redactar(args), _redactar(kwargs)
        todos = ", ".join([repr(a) for a in args]
                          + ["%s=%r" % (k, v) for k, v in kwargs.items()])
        vals = {"args": args, "kwargs": kwargs,
                "func_string": "%r.%s(%s)" % (job.records, job.method_name,
                                              todos)}
        for campo in ("result", "exc_info", "exc_message", "exc_name"):
            if campo in job._fields and job[campo]:
                texto = str(job[campo])
                for claro in claros:
                    texto = texto.replace(claro, REDACTADO)
                if texto != str(job[campo]):
                    vals[campo] = texto
        job.write(vals)
        limpios += 1
    _logger.info("primate_cloud_manager: secretos limpiados de %s job(s).",
                 limpios)
