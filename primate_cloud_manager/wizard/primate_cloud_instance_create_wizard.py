# -*- coding: utf-8 -*-
"""Wizard «Agregar instancia» (R3-B3): otro Odoo en un servidor EXISTENTE.

Acá se materializa el servidor compartido de D6: el segundo Odoo puede ser
de OTRO cliente (``project_id`` requerido y libre). El wizard NUNCA lanza
una EC2: crea la instancia con su slot de puertos asignado atómicamente
(:meth:`environment._create_instance_with_ports`) y encola
:meth:`environment.job_add_instance` (lock por servidor + health snapshot
antes/después + rollback clean-slate).
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.primate_cloud_environment import DB_NAME_RE
from ..models.primate_cloud_instance import slugify

# RAM aproximada (GB) por tipo de máquina, para la advertencia ADVISORIA del
# wizard (diseño R3 §8: no bloquea en v1; el límite duro llega cuando haya
# medición de consumo por instancia). Tipos fuera del mapa: sin advertencia.
RAM_GB_BY_TYPE = {
    "t2.nano": 0.5, "t3.nano": 0.5, "t3a.nano": 0.5,
    "t2.micro": 1, "t3.micro": 1, "t3a.micro": 1,
    "t2.small": 2, "t3.small": 2, "t3a.small": 2,
    "t2.medium": 4, "t3.medium": 4, "t3a.medium": 4,
    "t2.large": 8, "t3.large": 8, "t3a.large": 8,
    "m5.large": 8, "m6i.large": 8, "m7i.large": 8,
    "t2.xlarge": 16, "t3.xlarge": 16, "m5.xlarge": 16, "m6i.xlarge": 16,
}


class PrimateCloudInstanceCreateWizard(models.TransientModel):
    """Parámetros del Odoo nuevo a montar dentro de un servidor activo."""

    _name = "primate.cloud.instance.create.wizard"
    _description = "Agregar instancia (otro Odoo en un servidor existente)"

    environment_id = fields.Many2one(
        "primate.cloud.environment", string="Servidor", required=True,
        ondelete="cascade",
    )
    project_id = fields.Many2one(
        "primate.cloud.project", string="Proyecto (cliente)", required=True,
        help="A quién pertenece este Odoo. Puede ser OTRO cliente: acá se "
             "materializa el servidor compartido (D6).",
    )
    name = fields.Char(
        string="Nombre de la instancia", required=True,
        help="El slug técnico (dirs/unit/sitio nginx) sale de este nombre.",
    )
    odoo_version = fields.Selection(
        [("17", "17"), ("18", "18"), ("19", "19")],
        string="Versión Odoo", required=True, default="19",
    )
    odoo_edition = fields.Selection(
        [("community", "Community"), ("enterprise", "Enterprise")],
        string="Edición Odoo", required=True, default="community",
    )
    domain = fields.Char(
        string="Dominio", required=True,
        help="server_name del sitio nginx propio (ej.: cliente.primate.cloud). "
             "Por IP cruda responde la instancia default del servidor.",
    )
    db_name = fields.Char(
        string="Nombre de la base", required=True,
        help="Única en el servidor (PostgreSQL local compartido).",
    )
    admin_password = fields.Char(
        string="Contraseña admin (odoo.conf)", password="True", required=True,
        help="admin_passwd del conf propio. No se almacena en PCM.",
    )
    workers = fields.Integer(
        string="Workers", default=0,
        help="0 = threaded (default multi-Odoo, D-R3.6). Subir solo con RAM "
             "de sobra.",
    )
    create_dns = fields.Boolean(string="Crear registro DNS")
    hosted_zone_id = fields.Char(string="Hosted Zone ID")
    ttl = fields.Integer(string="TTL", default=300)

    # --- Informativos ---
    http_port_preview = fields.Integer(
        string="Puerto HTTP (se asignará)", compute="_compute_ports_preview",
        help="Slot probable; la asignación real (atómica) ocurre al confirmar.",
    )
    gevent_port_preview = fields.Integer(
        string="Puerto gevent (se asignará)", compute="_compute_ports_preview",
    )
    ram_warning = fields.Text(
        string="Advertencia de RAM", compute="_compute_ram_warning",
    )

    @api.depends("environment_id")
    def _compute_ports_preview(self):
        for wizard in self:
            if wizard.environment_id:
                # Sin lock: es un preview (la asignación real, atómica bajo
                # xact_lock, ocurre al confirmar en _create_instance_with_ports).
                http_port, gevent_port = wizard.environment_id._allocate_ports(
                    lock=False)
            else:
                http_port = gevent_port = 0
            wizard.http_port_preview = http_port
            wizard.gevent_port_preview = gevent_port

    @api.depends("environment_id")
    def _compute_ram_warning(self):
        """Advertencia ADVISORIA: nº de Odoo vivos vs RAM del tipo de máquina.

        Regla práctica v1: ~1 instancia por GB de RAM (workers=0 + swap del
        bootstrap). No bloquea: informa para decidir con datos.
        """
        for wizard in self:
            wizard.ram_warning = False
            server = wizard.environment_id
            if not server:
                continue
            machine = server._active_machine()
            ram_gb = RAM_GB_BY_TYPE.get(machine.instance_type or "")
            if not ram_gb:
                continue
            vivas = len(server.instance_ids.filtered(
                lambda i: i.state in ("active", "installing")))
            if vivas + 1 > ram_gb:
                wizard.ram_warning = _(
                    "El servidor (%(tipo)s, %(ram)s GB de RAM) quedaría con "
                    "%(total)d Odoo corriendo. Regla práctica: ~1 instancia "
                    "por GB (workers=0 + swap). Va a funcionar pero puede "
                    "ponerse lento; considerá otro servidor o subir el tipo "
                    "de máquina.",
                    tipo=machine.instance_type, ram=ram_gb, total=vivas + 1)

    def _validate(self):
        """Valida ANTES de crear nada (el server-side no confía en la vista)."""
        self.ensure_one()
        server = self.environment_id
        if server.state != "active":
            raise UserError(_("Solo se agregan instancias a un servidor activo."))
        if server._is_legacy_layout():
            raise UserError(_(
                "Este servidor tiene layout legacy (pre-multi-Odoo): la "
                "adopción in-place es una mini-fase pendiente del recableo."))
        db_name = (self.db_name or "").strip()
        if not DB_NAME_RE.match(db_name):
            raise UserError(_(
                "Nombre de base inválido: %r (letras/números/guiones bajos, "
                "sin espacios ni símbolos).") % db_name)
        if self.env["primate.cloud.database"].search_count(
                [("environment_id", "=", server.id), ("name", "=", db_name)]):
            raise UserError(_(
                "Ya hay una base «%s» registrada en este servidor: el "
                "PostgreSQL local es compartido, elegí otro nombre.") % db_name)
        slug = slugify(self.name)
        if self.env["primate.cloud.instance"].with_context(
                active_test=False).search_count(
                [("environment_id", "=", server.id), ("slug", "=", slug)]):
            raise UserError(_(
                "Ya existe una instancia con el nombre técnico «%s» en este "
                "servidor (incluye archivadas): cambiá el nombre.") % slug)
        if self.create_dns and not (self.hosted_zone_id or "").strip():
            raise UserError(_("Para crear DNS, indicá el Hosted Zone ID."))

    def action_add_instance(self):
        """Crea la instancia (slot atómico) y encola el install multi-Odoo."""
        self.ensure_one()
        self._validate()
        server = self.environment_id
        instance = server._create_instance_with_ports({
            "name": self.name,
            "project_id": self.project_id.id,
            "slug": slugify(self.name),
            "odoo_version": self.odoo_version,
            "odoo_edition": self.odoo_edition,
            "main_url": self.domain,
        })
        server._enqueue_add_instance(instance, {
            "db_mode": "local_pg",
            "db_name": (self.db_name or "").strip(),
            "domain": self.domain,
            "admin_password": self.admin_password,
            "workers": self.workers,
            "create_dns": self.create_dns,
            "hosted_zone_id": self.hosted_zone_id,
            "ttl": self.ttl,
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info", "title": _("Agregando instancia"),
                "message": _(
                    "Instalación encolada en %(server)s (puertos %(http)d/"
                    "%(gevent)d). Los Odoo vivos del servidor no se tocan.",
                    server=server.name, http=instance.http_port,
                    gevent=instance.gevent_port),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
