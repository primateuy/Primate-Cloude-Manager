# -*- coding: utf-8 -*-
"""Caché de descubrimiento de red por (cuenta, región) — Bloque B2.

Guarda lo que ``aws_discovery`` descubrió/validó para no golpear la API en cada
creación. Política de invalidación HONESTA: un estado **OK** se cachea con TTL
(no re-descubrir si el semáforo está verde y es reciente); un estado **NO-OK**
(falta algo / error) se **re-verifica SIEMPRE** — si el admin resolvió lo que
faltaba en AWS, el caché no puede quedar bloqueando "falta VPC" para siempre.
"""
from datetime import timedelta

from odoo import _, api, fields, models

from ..services import aws_discovery

# Nombre convenido del instance profile/rol (runbook). El auto-path lo asume.
PCM_SSM_ROLE = "pcm-ssm-role"
# TTL del caché para estados OK (no hostigar la API cuando todo está verde).
CACHE_TTL_HOURS = 1


class PrimateCloudRegionSetup(models.Model):
    _name = "primate.cloud.region.setup"
    _description = "Descubrimiento de red por región (caché)"
    _order = "account_id, region"

    account_id = fields.Many2one(
        "primate.cloud.account", string="Cuenta", required=True,
        ondelete="cascade", index=True)
    region = fields.Char(string="Región", required=True, index=True)
    status = fields.Selection(
        [("draft", "Sin verificar"), ("ok", "Listo"),
         ("missing_structural", "Falta infraestructura"), ("error", "Error")],
        string="Estado", default="draft", index=True)
    # Recursos descubiertos.
    vpc_id = fields.Char(string="VPC", readonly=True)
    subnet_id = fields.Char(string="Subnet pública", readonly=True)
    security_group_id = fields.Char(string="Security Group", readonly=True)
    profile_ok = fields.Boolean(string="Rol pcm-ssm-role", readonly=True)
    has_igw = fields.Boolean(string="VPC con salida a internet", readonly=True)
    detail = fields.Text(string="Detalle accionable", readonly=True)
    last_discovered_at = fields.Datetime(string="Verificado al", readonly=True)
    discovered_by = fields.Many2one("res.users", string="Verificado por",
                                    readonly=True)

    _region_uniq = models.Constraint(
        "UNIQUE(account_id, region)",
        "Ya existe el descubrimiento de esa región para la cuenta.")

    def _compute_display_name(self):
        labels = dict(self._fields["status"].selection)
        for rec in self:
            rec.display_name = "%s / %s — %s" % (
                rec.account_id.display_name or "?", rec.region or "?",
                labels.get(rec.status, rec.status or ""))

    # ------------------------------------------------------------------
    def _discovery(self):
        self.ensure_one()
        return aws_discovery.AwsDiscoveryService(
            self.account_id._get_aws_service())

    def action_discover(self):
        """'Verificar región': corre el descubrimiento y refresca el caché.

        Read-only contra AWS (describe + dry-run), SÍNCRONO por la política de
        lecturas interactivas. Botón siempre disponible → re-verificar es 1 click,
        clave cuando el semáforo está en rojo y el admin ya arregló AWS.
        """
        for rec in self:
            try:
                result = rec._discovery().discover_region(rec.region, PCM_SSM_ROLE)
                rec._apply_discovery(result)
            except Exception as error:  # noqa: BLE001 - error legible, no rompe
                rec.write({
                    "status": "error",
                    "detail": _("No se pudo verificar la región: %s") % error,
                    "last_discovered_at": fields.Datetime.now(),
                    "discovered_by": self.env.user.id})
        return True

    def _apply_discovery(self, result):
        """Mapea el dict de discover_region a los campos + estado + detalle."""
        self.ensure_one()
        vpc = result.get("vpc") or {}
        subnet = result.get("subnet") or {}
        sg = result.get("security_group") or {}
        missing = result.get("structural_missing") or []
        status = "ok" if result.get("structural_ok") else "missing_structural"
        self.write({
            "status": status,
            "profile_ok": (result.get("profile") or {}).get("exists", False),
            "vpc_id": vpc.get("vpc_id") or False,
            "has_igw": vpc.get("has_igw", False),
            "subnet_id": subnet.get("subnet_id") or False,
            "security_group_id": (sg or {}).get("id") or False,
            "detail": self._actionable_detail(result, missing, vpc),
            "last_discovered_at": fields.Datetime.now(),
            "discovered_by": self.env.user.id,
        })

    @staticmethod
    def _actionable_detail(result, missing, vpc):
        """Mensaje accionable por recurso faltante (apunta al runbook, no traceback)."""
        lines = []
        if "profile" in missing:
            lines.append(_(
                "Falta el instance profile 'pcm-ssm-role' (o no es pasable) en "
                "esta cuenta. Corré el paso IAM del onboarding antes de "
                "aprovisionar."))
        if "vpc" in missing:
            if vpc.get("ambiguous"):
                lines.append(_(
                    "Hay varias VPC candidatas y ninguna default ni tagueada. "
                    "Tagueá la VPC que PCM debe usar con la clave "
                    "'primate:managed_by' = 'pcm' (EC2 → VPC → Tags) y re-verificá."))
            else:
                lines.append(_(
                    "No hay una VPC usable en esta región. PCM no crea VPC en v1: "
                    "prepará una VPC (o usá la default) según el runbook."))
        if "vpc_igw" in missing:
            lines.append(_(
                "La VPC no tiene Internet Gateway (sin salida a internet). "
                "Creá/attachá un IGW a la VPC según el runbook."))
        if "subnet" in missing:
            lines.append(_(
                "No hay una subnet pública (auto-assign public IP + ruta a "
                "Internet Gateway). Prepará una subnet pública según el runbook."))
        if not missing:
            sg = result.get("security_group")
            if not sg:
                lines.append(_(
                    "Todo listo. El security group 'pcm-managed' se creará al "
                    "aprovisionar (ingress 80/443)."))
            elif not sg.get("rules_ok"):
                lines.append(_(
                    "Todo listo. Se completarán reglas faltantes del security "
                    "group 'pcm-managed': %s.") % sg.get("missing_ingress"))
            else:
                lines.append(_("Todo listo. Región verificada."))
        return "\n".join(lines)

    @api.model
    def get_or_discover(self, account, region):
        """Devuelve el setup de (cuenta, región), re-descubriendo según política.

        OK reciente (< TTL) → se reusa el caché. NO-OK o vencido → re-descubre
        (un 'falta algo' NUNCA se confía viejo: el admin pudo haberlo resuelto).
        """
        setup = self.search([("account_id", "=", account.id),
                              ("region", "=", region)], limit=1)
        if not setup:
            setup = self.create({"account_id": account.id, "region": region})
        fresh_ok = (
            setup.status == "ok" and setup.last_discovered_at
            and setup.last_discovered_at > fields.Datetime.now()
            - timedelta(hours=CACHE_TTL_HOURS))
        if not fresh_ok:
            setup.action_discover()
        return setup
