# -*- coding: utf-8 -*-
"""Wizard del dominio definitivo de una instancia (decisión 2026-10-27).

Los montajes sin dominio quedan en sslip.io con HTTPS. El dominio se suma
después por uno de dos caminos, los dos NO destructivos (si algo falla, la
instancia sigue como estaba):

- ``route53`` («Crear DNS ahora»): PCM crea el registro A en Route 53, espera
  INSYNC y la propagación, y cambia la instancia al dominio nuevo.
- ``manual`` («Asociar dominio existente»): el registro lo cargó el usuario
  (en la consola de Route 53 o en cualquier otro DNS). PCM NO toca Route 53:
  verifica que el dominio apunte a la IP del servidor y cambia.
"""
import logging
import re
import socket

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
# Subdominio por defecto de «Crear DNS ahora» (2026-10-28): el permiso de
# Route 53 del stack (NombresDns) solo alcanza a *.<sufijo>.
SUFFIX_PARAM = "pcm.route53_default_domain_suffix"
RESERVED_SUFFIXES = (".local", ".invalid", ".localhost", ".test", ".example",
                     ".internal", ".lan")


class PrimateCloudDomainWizard(models.TransientModel):
    _name = "primate.cloud.domain.wizard"
    _description = "Dominio definitivo de una instancia"

    instance_id = fields.Many2one(
        "primate.cloud.instance", string="Instancia", required=True,
        ondelete="cascade")
    mode = fields.Selection(
        [("route53", "Crear DNS ahora (Route 53, lo crea PCM)"),
         ("manual", "Asociar dominio existente (registro cargado a mano)")],
        string="Camino", required=True, default="manual")
    domain = fields.Char(string="Dominio nuevo", required=True)
    hosted_zone_id = fields.Char(
        string="Hosted Zone ID",
        default=lambda self: self.env["ir.config_parameter"].sudo().get_param(
            "pcm.route53_default_zone_id", ""))
    ttl = fields.Integer(string="TTL", default=300)
    current_domain = fields.Char(
        string="Dominio actual", related="instance_id.main_url")
    server_ip = fields.Char(string="IP del servidor",
                            compute="_compute_server_ip")

    @api.model
    def default_get(self, fields_list):
        """Con «Crear DNS ahora» propone ``<slug>.<sufijo por defecto>``."""
        res = super().default_get(fields_list)
        suffix = self._default_suffix()
        if (res.get("mode") == "route53" and suffix and not res.get("domain")
                and res.get("instance_id")):
            inst = self.env["primate.cloud.instance"].browse(res["instance_id"])
            if inst.slug:
                res["domain"] = "%s.%s" % (inst.slug, suffix)
        return res

    def _default_suffix(self):
        return (self.env["ir.config_parameter"].sudo().get_param(SUFFIX_PARAM)
                or "").strip().lower().strip(".")

    @api.depends("instance_id")
    def _compute_server_ip(self):
        for wiz in self:
            machine = wiz.instance_id._machine() if wiz.instance_id else False
            wiz.server_ip = machine.public_ip if machine else False

    # ------------------------------------------------------------------
    def _clean_domain(self):
        domain = (self.domain or "").strip().lower().rstrip(".")
        domain = re.sub(r"^https?://", "", domain).split("/")[0]
        if not DOMAIN_RE.match(domain):
            raise UserError(_("«%s» no es un dominio válido.") % self.domain)
        if domain.endswith(RESERVED_SUFFIXES):
            raise UserError(_(
                "«%s» es un dominio reservado: nunca va a resolver en "
                "internet.") % domain)
        if domain == (self.instance_id.main_url or "").strip().lower():
            raise UserError(_("La instancia ya está en %s.") % domain)
        return domain

    @staticmethod
    def _resolve_ipv4(domain):
        """IPs (A) del dominio según el resolver de PCM, o [] si no resuelve.
        Chequeo rápido para avisar enseguida; el job vuelve a verificar
        desde el servidor (el que decide)."""
        try:
            infos = socket.getaddrinfo(domain, None, socket.AF_INET,
                                       socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError):
            return []
        return sorted({info[4][0] for info in infos})

    def action_confirm(self):
        self.ensure_one()
        instance = self.instance_id
        instance._pcm_assert_not_imported(_("cambiar el dominio"))
        if instance.state != "active":
            raise UserError(_("La instancia tiene que estar activa."))
        machine = instance._machine()
        if not machine or not machine.public_ip:
            raise UserError(_(
                "El servidor no tiene máquina activa con IP pública."))
        domain = self._clean_domain()

        if self.mode == "route53":
            zone = (self.hosted_zone_id or "").strip()
            if not zone:
                raise UserError(_("Falta el Hosted Zone ID."))
            suffix = self._default_suffix()
            if suffix and not domain.endswith("." + suffix):
                raise UserError(_(
                    "PCM solo puede crear registros dentro de %(s)s (el "
                    "permiso de Route 53 del stack alcanza a *.%(s)s): "
                    "«%(d)s» queda afuera. No se cambió nada.",
                    s=suffix, d=domain))
            instance.environment_id._check_hosted_zone(
                instance.account_id, zone)
            instance._domain_switch_start(domain)
            instance.with_delay(
                description=_("Crear DNS y cambiar dominio: %s")
                % instance.display_name
            ).job_create_dns_and_switch(domain, zone, self.ttl or 300)
            message = _(
                "Creando %(d)s → %(ip)s en Route 53. PCM espera la "
                "propagación, emite el certificado y recién ahí retira "
                "%(old)s.", d=domain, ip=machine.public_ip,
                old=instance.main_url or "—")
        else:
            ips = self._resolve_ipv4(domain)
            if machine.public_ip not in ips:
                if ips:
                    motivo = _(
                        "%(d)s apunta a %(a)s, no a la IP del servidor "
                        "(%(ip)s).", d=domain, a=", ".join(ips),
                        ip=machine.public_ip)
                else:
                    motivo = _(
                        "%(d)s todavía no resuelve: tiene que apuntar a la IP "
                        "del servidor (%(ip)s).", d=domain, ip=machine.public_ip)
                raise UserError(_(
                    "%s Creá o corregí el registro A y volvé a intentar. No se "
                    "cambió nada.") % motivo)
            instance._domain_switch_start(domain)
            instance.with_delay(
                description=_("Asociar dominio: %s") % instance.display_name
            ).job_attach_domain(domain)
            message = _(
                "%(d)s apunta al servidor. PCM monta el dominio en paralelo, "
                "emite el certificado y recién ahí retira %(old)s.",
                d=domain, old=instance.main_url or "—")
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"type": "info", "sticky": False, "message": message,
                       "next": {"type": "ir.actions.act_window_close"}},
        }
