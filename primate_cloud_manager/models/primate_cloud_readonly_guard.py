# -*- coding: utf-8 -*-
"""Cerrojo de frontera para recursos IMPORTADOS (regla dura 2026-10-22).

Tercera capa (después de la UI y de las guardas de modelo/job): se engancha
en ``AwsBaseService.get_client`` y ve TODA llamada a AWS antes de que salga.
Las lecturas pasan; cualquier otra operación que toque un recurso importado
—una EC2 (por id), un nombre DNS, una RDS— se aborta SIN llamar al
proveedor, con log. La única escritura admitida sobre una EC2 importada es
``ssm:SendCommand`` del documento propio de solo lectura, sin parámetros.

La cuarta capa es IAM (policy pcm-prod-readonly): aunque este código tuviera
un bug, la credencial productiva no puede modificar nada.
"""
import logging

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DISCOVERY_DOCUMENT_PARAM = "pcm.readonly_discovery_document"
DISCOVERY_DOCUMENT_DEFAULT = "pcm-readonly-discovery"
# Llave de propiedad (minúsculas: IAM compara claves de tag sin distinguir).
OWNERSHIP_TAG = "primate:managed_by"


def _norm_dns(name):
    return (name or "").strip().rstrip(".").lower()


class ReadonlyGuard:
    """Conjuntos protegidos + decisión por operación. Se construye por
    llamada a ``_get_aws_service`` (siempre fresco: nada cacheado entre
    transacciones)."""

    def __init__(self, instance_ids, dns_names, rds_ids, readonly_document,
                 on_block=None):
        self.instance_ids = set(instance_ids)
        self.dns_names = {_norm_dns(n) for n in dns_names}
        self.rds_ids = set(rds_ids)
        self.readonly_document = readonly_document
        self.on_block = on_block

    @classmethod
    def from_env(cls, env):
        machines = env["primate.cloud.ec2.instance"].sudo().with_context(
            active_test=False).search([
                ("management_state", "=", "adopted_readonly")])
        instance_ids = [m.aws_instance_id for m in machines
                        if m.aws_instance_id]
        environments = machines.mapped("environment_id") | env[
            "primate.cloud.environment"].sudo().with_context(
                active_test=False).search([("pcm_origin", "!=", "pcm")])
        dns = env["primate.cloud.dns.record"].sudo().with_context(
            active_test=False).search([
                "|", "|", ("pcm_origin", "=", "imported"),
                ("environment_id", "in", environments.ids),
                ("instance_id.environment_id", "in", environments.ids)])
        rds = env["primate.cloud.database"].sudo().with_context(
            active_test=False).search([
                ("db_type", "=", "rds"),
                "|", ("pcm_origin", "=", "imported"),
                ("environment_id", "in", environments.ids)])
        document = env["ir.config_parameter"].sudo().get_param(
            DISCOVERY_DOCUMENT_PARAM, DISCOVERY_DOCUMENT_DEFAULT)

        def on_block(service, operation, detail):
            _logger.warning(
                "PCM readonly guard: BLOQUEADO %s:%s — %s",
                service, operation, detail)
            from .primate_cloud_destruction import durable_log
            durable_log(env, {
                "action_type": "readonly_block", "result": "failed",
                "name": "Frontera AWS: %s:%s bloqueado (importado)" % (
                    service, operation),
                "user_id": env.uid,
                "error_message": "Recurso importado: %s. No se llamó al "
                                 "proveedor." % detail,
            })

        return cls(instance_ids, dns.mapped("name"),
                   [r.rds_identifier or r.name for r in rds if r.rds_identifier or r.name], document, on_block)

    # ------------------------------------------------------------------
    @staticmethod
    def _ids_in(params):
        ids = set()
        for key in ("InstanceIds", "Resources"):
            for value in params.get(key) or []:
                ids.add(value)
        if params.get("InstanceId"):
            ids.add(params["InstanceId"])
        for target in params.get("Targets") or []:
            if target.get("Key") in ("InstanceIds", "InstanceId"):
                ids.update(target.get("Values") or [])
        return ids

    def _block(self, service, operation, detail):
        if self.on_block:
            self.on_block(service, operation, detail)
        raise UserError(_(
            "Operación bloqueada: %(svc)s:%(op)s toca un recurso IMPORTADO "
            "(%(det)s). Los ambientes importados son solo lectura, sin "
            "excepción. No se llamó al proveedor.",
            svc=service, op=operation, det=detail))

    @staticmethod
    def _touches_ownership_key(operation, params):
        """CreateTags/DeleteTags sobre la llave de propiedad, o un DeleteTags
        sin claves (que borra TODAS, la llave incluida)."""
        if operation not in ("create_tags", "delete_tags"):
            return False
        tags = params.get("Tags")
        if operation == "delete_tags" and not tags:
            return True
        return any((t.get("Key") or "").lower() == OWNERSHIP_TAG
                   for t in tags or [])

    def check(self, service, operation, params):
        """Levanta si la operación (NO de lectura) toca algo importado."""
        params = params or {}
        # La llave primate:managed_by solo nace con el recurso (TagSpecifications
        # al crear, que no pasa por acá). Ponerla o sacarla DESPUÉS sería ganar
        # o perder la propiedad de algo existente: bloqueado sobre cualquier
        # recurso, importado o no.
        if service == "ec2" and self._touches_ownership_key(operation, params):
            self._block(service, operation, _(
                "la etiqueta %s solo se pone al crear el recurso")
                % OWNERSHIP_TAG)
        if service in ("ec2", "ssm"):
            touched = self._ids_in(params) & self.instance_ids
            if touched:
                if (service == "ssm" and operation == "send_command"
                        and params.get("DocumentName") == self.readonly_document
                        and not params.get("Parameters")):
                    return  # el discovery de solo lectura: la única excepción
                self._block(service, operation,
                            ", ".join(sorted(touched)))
        elif service == "route53" and operation == "change_resource_record_sets":
            for change in (params.get("ChangeBatch") or {}).get("Changes") or []:
                name = _norm_dns(
                    (change.get("ResourceRecordSet") or {}).get("Name"))
                if name in self.dns_names:
                    self._block(service, operation, name)
        elif service == "rds":
            ident = params.get("DBInstanceIdentifier")
            if ident and ident in self.rds_ids:
                self._block(service, operation, ident)
