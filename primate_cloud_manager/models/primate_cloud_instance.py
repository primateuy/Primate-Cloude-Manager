# -*- coding: utf-8 -*-
"""Instancia Odoo (D6 / Recableo R1): un Odoo montado DENTRO de un servidor.

Punto de cruce de los dos ejes del modelo destino (``RECABLEO_PLAN.md`` §1):

- **eje CLIENTE**: la instancia pertenece a UN proyecto (``project_id``,
  a quién se factura/gestiona);
- **eje INFRAESTRUCTURA**: corre en UN entorno/servidor (``environment_id``).

Desde R1 la instancia es la **fuente de verdad** de la identidad del Odoo
(tipo/versión/edición/URL/respaldos). El entorno delega esos campos por
compat (related store) hasta que R2/R4 recableen flujos y UI; los modelos
satélite (repos/deploys/DNS/BD/backups) llevan ``instance_id`` real con
auto-resolución compat (mixin de abajo).
"""
import re
import uuid

from odoo import _, api, fields, models

# Layout LEGACY (single-Odoo, install_odoo.sh actual). Las instancias nuevas
# nacen con estas rutas hasta que R3 introduzca el layout multi-Odoo por slug;
# tener las rutas POR INSTANCIA es lo que permite convivir ambos layouts.
LEGACY_SERVICE = "odoo"
LEGACY_CONF_PATH = "/etc/odoo/odoo.conf"
LEGACY_DATA_DIR = "/opt/odoo/.local/share/Odoo"
LEGACY_ADDONS_DIR = "/opt/odoo/custom-addons"
LEGACY_HTTP_PORT = 8069
LEGACY_GEVENT_PORT = 8072
# Runtime legacy (R4-B1): en multi-Odoo el runtime es compartido por versión
# bajo /opt/pcm/runtime; estos son los equivalentes del layout viejo.
LEGACY_PYTHON_BIN = "/opt/odoo/venv/bin/python3"
LEGACY_ODOO_BIN = "/opt/odoo/odoo/odoo-bin"
LEGACY_LOG_PATH = "/var/log/odoo/odoo.log"


def slugify(name):
    """Slug técnico desde un nombre humano (minúsculas, [a-z0-9-])."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "instancia"


class PrimateCloudInstance(models.Model):
    """Un Odoo dentro de un servidor: dos padres obligatorios (D6)."""

    _name = "primate.cloud.instance"
    _description = "Instancia Odoo (un Odoo dentro de un servidor)"
    _inherit = ["mail.thread"]
    _order = "name"

    name = fields.Char(string="Nombre", required=True, tracking=True)
    project_id = fields.Many2one(
        "primate.cloud.project", string="Proyecto (cliente)",
        required=True, ondelete="restrict", index=True,
        help="Eje cliente: a quién pertenece/factura este Odoo.",
    )
    environment_id = fields.Many2one(
        "primate.cloud.environment", string="Entorno (servidor)",
        required=True, ondelete="restrict", index=True,
        help="Eje infraestructura: en qué servidor corre este Odoo.",
    )
    account_id = fields.Many2one(
        related="environment_id.account_id", string="Cuenta AWS",
        store=True, readonly=True,
    )
    slug = fields.Char(
        string="Slug", required=True, copy=False,
        help="Identificador técnico único por servidor (dirs/unit/BD en el "
             "layout multi-Odoo de R3).",
    )
    state = fields.Selection(
        [
            ("draft", "Borrador"),
            ("installing", "Instalando"),
            ("active", "Activa"),
            ("error", "Error"),
            ("archived", "Archivada"),
        ],
        string="Estado", default="draft", required=True, tracking=True,
        help="Estado del Odoo (no de la máquina: eso vive en el servidor). "
             "Hasta R2 el estado operativo sigue siendo el del entorno.",
    )
    env_type = fields.Selection(
        [
            ("production", "Producción"),
            ("staging", "Staging"),
            ("testing", "Testing"),
            ("development", "Desarrollo"),
        ],
        string="Tipo", required=True, default="production",
        help="Propósito del Odoo (era del entorno; D6 lo muda a la instancia).",
    )
    odoo_version = fields.Selection(
        [("17", "17"), ("18", "18"), ("19", "19")], string="Versión Odoo",
    )
    odoo_edition = fields.Selection(
        [("community", "Community"), ("enterprise", "Enterprise")],
        string="Edición Odoo",
    )
    main_url = fields.Char(string="URL principal", help="Ej.: forum.primate.cloud")

    # --- Runtime en el servidor (rutas POR instancia: conviven legacy y multi-Odoo) ---
    http_port = fields.Integer(string="Puerto HTTP", default=LEGACY_HTTP_PORT)
    gevent_port = fields.Integer(string="Puerto gevent", default=LEGACY_GEVENT_PORT)
    service_name = fields.Char(string="Servicio systemd", default=LEGACY_SERVICE)
    conf_path = fields.Char(string="Ruta odoo.conf", default=LEGACY_CONF_PATH)
    data_dir = fields.Char(string="Data dir (filestore)", default=LEGACY_DATA_DIR)
    addons_dir = fields.Char(string="Dir de addons custom", default=LEGACY_ADDONS_DIR)
    pg_user = fields.Char(string="Usuario PostgreSQL", default="odoo")
    # Runtime por instancia (R4-B1): con qué intérprete/odoo-bin se opera este
    # Odoo (shell, -u, -i) y dónde loguea. Materializados por R3 en multi;
    # defaults legacy para los servidores pre-R3 — un solo código para ambos.
    python_bin = fields.Char(string="Python del runtime", default=LEGACY_PYTHON_BIN)
    odoo_bin = fields.Char(string="odoo-bin", default=LEGACY_ODOO_BIN)
    log_path = fields.Char(string="Log de Odoo", default=LEGACY_LOG_PATH)
    database_id = fields.Many2one(
        "primate.cloud.database", string="Base de datos principal",
        ondelete="set null",
    )

    # --- Respaldos (asignación por instancia desde R1; el validador la lee
    #     vía la delegación del entorno hasta R4) ---
    backup_policy_id = fields.Many2one(
        "primate.cloud.backup.policy", string="Política de respaldo",
        ondelete="restrict", tracking=True,
    )
    # OJO: sin readonly=True a nivel campo — la delegación del entorno
    # (related readonly=False) NO propaga el write-through a un destino
    # readonly (verificado en Odoo 19: el entorno guardaba y la instancia
    # no, y el próximo recompute revertía). El "solo lectura" es intención
    # de UI → vive en las vistas (readonly="1"), como last_sync_date.
    backup_compliance = fields.Selection(
        [
            ("ok", "Cumple"),
            ("non_compliant", "No cumple"),
            ("unverifiable", "No verificable"),
            ("no_policy", "Sin política definida"),
        ],
        string="Cumplimiento de respaldo",
        default="no_policy", copy=False,
    )
    backup_compliance_detail = fields.Text(
        string="Detalle de cumplimiento", copy=False
    )
    last_backup_check = fields.Datetime(
        string="Última verificación de respaldo", copy=False
    )

    # --- Staging (el vínculo instancia→instancia; el flujo sigue en el
    #     entorno hasta R4) ---
    origin_instance_id = fields.Many2one(
        "primate.cloud.instance", string="Instancia origen",
        readonly=True, ondelete="set null", copy=False,
        help="Para staging: instancia de la que se clonó.",
    )
    staging_ids = fields.One2many(
        "primate.cloud.instance", "origin_instance_id", string="Stagings"
    )

    # --- Costos (reparto R5) ---
    pcm_ref = fields.Char(
        string="Ref estable", readonly=True, copy=False, index=True,
        help="Identificador inmutable de la instancia para las líneas de "
             "reparto de costo (cost.share, R5). No cambia al renombrar.",
    )
    cost_weight = fields.Float(
        string="Peso de costo", default=1.0,
        help="Peso para el método de reparto 'por peso' (R5).",
    )

    active = fields.Boolean(string="Activo", default=True)
    notes = fields.Text(string="Notas")

    _slug_uniq = models.Constraint(
        "UNIQUE(environment_id, slug)",
        "Ya existe una instancia con ese slug en el servidor.",
    )
    _http_port_uniq = models.Constraint(
        "UNIQUE(environment_id, http_port)",
        "Ya existe una instancia usando ese puerto HTTP en el servidor.",
    )
    _pcm_ref_uniq = models.Constraint(
        "UNIQUE(pcm_ref)",
        "El identificador estable (pcm_ref) de la instancia debe ser único.",
    )

    @staticmethod
    def _new_pcm_ref():
        """Ref estable de instancia (análogo al pcm_env_ del servidor)."""
        return "pcm_inst_" + uuid.uuid4().hex

    @api.model_create_multi
    def create(self, vals_list):
        """Asigna slug y pcm_ref por registro si no vienen."""
        for vals in vals_list:
            if not vals.get("slug"):
                vals["slug"] = slugify(vals.get("name"))
            if not vals.get("pcm_ref"):
                vals["pcm_ref"] = self._new_pcm_ref()
        return super().create(vals_list)

    def _compute_display_name(self):
        for rec in self:
            server = rec.environment_id.name
            rec.display_name = (
                "%s @ %s" % (rec.name, server) if server else rec.name or ""
            )

    def _machine(self):
        """La máquina AWS viva del servidor donde corre esta instancia.

        Cimiento de R4: las operaciones por-instancia (config/logs/addons/
        impersonación) resuelven acá su destino SSM. Recordset vacío si el
        servidor no tiene máquina activa.
        """
        self.ensure_one()
        return self.environment_id._active_machine()


class PrimateCloudInstanceLinked(models.AbstractModel):
    """Compat R1: los satélites ganan ``instance_id`` con auto-resolución.

    Los modelos que hoy cuelgan de ``environment_id`` (repos, deploys, DNS,
    BDs, backups) pasan a tener el vínculo REAL con la instancia. Mientras
    los flujos sigan creando con ``environment_id`` (hasta R2/R4), el create
    resuelve el faltante desde el otro: solo entorno → instancia primaria del
    entorno; solo instancia → su servidor. Nada queda huérfano y ningún flujo
    se rompe. El par ``environment_id`` de estos modelos se retira en R4.
    """

    _name = "primate.cloud.instance.linked"
    _description = "Vínculo a instancia con auto-resolución (compat R1)"

    instance_id = fields.Many2one(
        "primate.cloud.instance", string="Instancia", ondelete="set null",
        index=True,
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._autofill_instance_link(vals)
        return super().create(vals_list)

    @api.model
    def _autofill_instance_link(self, vals):
        """Completa el lado faltante del par instancia↔entorno (in place)."""
        has_env_field = "environment_id" in self._fields
        if vals.get("instance_id") and has_env_field and not vals.get("environment_id"):
            instance = self.env["primate.cloud.instance"].browse(vals["instance_id"])
            vals["environment_id"] = instance.environment_id.id
        elif vals.get("environment_id") and not vals.get("instance_id"):
            environment = self.env["primate.cloud.environment"].browse(
                vals["environment_id"])
            vals["instance_id"] = environment.primary_instance_id.id or False
        return vals
