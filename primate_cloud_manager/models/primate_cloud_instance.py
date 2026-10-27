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
import base64
import json
import re
import time
import uuid
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from .primate_cloud_destruction import durable_log

from ..services import aws_route53
from ..tools import crypto

# Layout LEGACY (single-Odoo, install_odoo.sh actual). Las instancias nuevas
# nacen con estas rutas hasta que R3 introduzca el layout multi-Odoo por slug;
# tener las rutas POR INSTANCIA es lo que permite convivir ambos layouts.
LEGACY_SERVICE = "odoo"
# Cuánto espera el cambio de dominio a que el registro propague (segundos).
# Route 53 suele estar INSYNC en ~1 minuto; afuera depende del DNS.
DOMAIN_PROPAGATION_WAIT = 300
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
    # Obligatorias EN EL MODELO (2026-09-19): sin versión la instalación falla
    # recién sobre la EC2 ya creada. Ningún camino —UI, wizard o programático—
    # crea una instancia sin ellas; las existentes en NULL toman el default al
    # volverse required (Odoo inicializa la columna antes del NOT NULL).
    odoo_version = fields.Selection(
        [("17", "17"), ("18", "18"), ("19", "19")], string="Versión Odoo",
        required=True, default="19",
    )
    odoo_edition = fields.Selection(
        [("community", "Community"), ("enterprise", "Enterprise")],
        string="Edición Odoo", required=True, default="community",
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
    # hosted_from = create_date (la instancia NO se mueve de servidor: dos
    # fechas alcanzan, no un ledger de intervalos). hosted_until se estampa al
    # archivar — dato GRATIS ahora e IRRECONSTRUIBLE después (lección pcm_ref):
    # el prorrateo por instancia-días del reparto (R5) lo necesita. Vacío =
    # sigue hospedada (cuenta hasta el fin del período).
    hosted_until = fields.Datetime(
        string="Hospedada hasta", readonly=True, copy=False,
        help="Momento en que la instancia dejó de estar hospedada (se archivó). "
             "Vacío = sigue viva. Alimenta el prorrateo del reparto de costos.",
    )

    # --- Impersonación (Login-as) POR INSTANCIA (R4-B3, D-R4.3) ---
    # El par Ed25519 es de ESTA instancia: la privada (cifrada con la Fernet
    # de la cuenta) firma; la pública se despliega a SU companion. Un token
    # firmado con la privada de A NO verifica contra la pública de B → el
    # caso cruzado es estructuralmente imposible, no una validación a agregar.
    # copy=False: un staging/duplicado JAMÁS hereda la clave de producción; y
    # NO se generan en create (D-R4.9): nacen en el primer deploy/enable.
    impersonate_privkey_encrypted = fields.Char(
        string="Clave privada de impersonación (cifrada)",
        copy=False, groups="primate_cloud_manager.group_cloud_admin",
    )
    impersonate_pubkey = fields.Char(
        string="Clave pública de impersonación",
        copy=False, groups="primate_cloud_manager.group_cloud_admin",
    )

    # --- DNS best-effort (R4-B6.4, §8.1) ---
    # Si Route53 falla al montar la instancia, el Odoo queda ACTIVO (nginx ya
    # responde por dominio/IP) y acá se guarda el motivo + los params para
    # reintentar el registro SIN re-correr el install (cero PCM_ERR_DIRTY_SLUG).
    # JSON: {"reason", "hosted_zone_id", "ttl", "domain", "record_type"}. Vacío
    # = sin DNS pendiente.
    dns_pending = fields.Char(
        string="DNS pendiente (JSON)", copy=False, readonly=True,
        help="Motivo + params del registro DNS que falló al aprovisionar, "
             "para reintentarlo sin re-instalar.",
    )

    # --- HTTPS (D-R3.9 endurecida, 2026-10-22) ---
    # El certificado es un PASO verificado del modelo (_provision_ssl), no la
    # cola best-effort del install. Estados: none (sin dominio / no aplica),
    # pending (esperando propagación o reintento del cron), active (emitido Y
    # verificado con un GET https real), failed (agotó reintentos o error duro
    # — p. ej. AAAA ajeno). failed NUNCA bloquea: la instancia sirve por HTTP
    # y el botón «Activar HTTPS ahora» rearma el ciclo.
    ssl_state = fields.Selection(
        [("none", "Sin HTTPS"), ("pending", "HTTPS pendiente"),
         ("active", "HTTPS activo"), ("failed", "HTTPS fallido")],
        string="Estado SSL", default="none", copy=False, tracking=True)
    ssl_cert_expiry = fields.Datetime(
        string="Vencimiento del certificado", copy=False, readonly=True)
    ssl_retry_count = fields.Integer(
        string="Intentos de certbot", copy=False, readonly=True,
        help="Solo cuentan ejecuciones REALES de certbot (rate limits de "
             "Let's Encrypt): esperar propagación no consume intento.")
    ssl_last_error = fields.Char(
        string="Último error SSL", copy=False, readonly=True)
    ssl_pending_since = fields.Datetime(
        string="SSL pendiente desde", copy=False, readonly=True)
    ssl_expiry_warned = fields.Boolean(
        string="Vencimiento avisado", copy=False,
        help="Evita repetir el aviso de certificado por vencer.")

    # --- Dominio definitivo (decisión 2026-10-27) ---
    # Cambio NO destructivo a un dominio nuevo («Crear DNS ahora» por Route 53
    # o «Asociar dominio existente» a mano). Mientras corre o si falló, la
    # instancia sigue sirviendo en su dominio actual con su HTTPS.
    domain_switch_target = fields.Char(
        string="Dominio en camino", copy=False, readonly=True)
    domain_switch_state = fields.Selection(
        [("running", "En curso"), ("failed", "No se pudo")],
        string="Cambio de dominio", copy=False, readonly=True)
    domain_switch_error = fields.Char(
        string="Motivo (cambio de dominio)", copy=False, readonly=True)

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
        records = super().create(vals_list)
        # Una instancia nueva puede volver COMPARTIDO un servidor dedicado (o
        # sumar el 1er cliente): re-taggear (por queue_job, no rompe el create).
        for env in records.environment_id:
            env._enqueue_client_tag_sync()
        return records

    def write(self, vals):
        """Estampa ``hosted_until`` al archivar (choke point único del dato).

        Sin importar QUÉ flujo archiva la instancia (barrido, teardown, acción
        manual), el paso a ``archived`` deja registrado CUÁNDO dejó de estar
        hospedada — lo necesita el prorrateo por días del reparto (R5). Solo se
        estampa si está vacío (idempotente: re-archivar NO pisa la fecha real);
        si vuelve a un estado vivo se limpia (edge de reactivación). Un
        ``hosted_until`` explícito en el mismo write manda (no se toca).
        """
        # ¿Cambia la condición dedicado/compartido? Pasar A o DESDE 'archived'
        # altera el set de instancias VIVAS del servidor (R5-B3). Se evalúa ANTES
        # de escribir (después, r.state ya cambió). Mismo choke point que
        # hosted_until: el re-tag por archivar/reactivar no se escapa.
        retag = "state" in vals and (
            vals["state"] == "archived"
            or any(rec.state == "archived" for rec in self))
        envs = self.environment_id
        # Cambio de dominio sobre una instancia VIVA (punto F de HTTPS): el
        # vhost y el certificado quedaron atados al dominio viejo → después
        # del write se reescribe el vhost, se borra el cert viejo y se reemite
        # para el nuevo. Solo instancias activas con slug (multi-Odoo); los
        # writes del propio provisioning no disparan (prev vacío o estado no
        # activo todavía).
        # Regla dura 2026-10-22: campos de infraestructura de una instancia
        # IMPORTADA no se escriben (ni por UI ni por código), salvo el sync
        # del gemelo que se identifica por contexto.
        if not self.env.context.get("pcm_import_sync"):
            infra = set(vals) & self._PCM_INFRA_FIELDS
            if infra:
                for rec in self:
                    if rec.pcm_is_imported():
                        rec._pcm_assert_not_imported(_(
                            "modificar %s") % ", ".join(sorted(infra)))
        domain_changed = {}
        if "main_url" in vals and not self.env.context.get("pcm_skip_ssl_hooks"):
            domain_changed = {
                rec.id: rec.main_url for rec in self
                if rec.main_url and rec.state == "active" and rec.slug
                and (vals["main_url"] or "") != rec.main_url}
        if vals.get("state") == "archived" and "hosted_until" not in vals:
            now = fields.Datetime.now()
            sin_sello = self.filtered(lambda r: not r.hosted_until)
            resto = self - sin_sello
            res = True
            if sin_sello:
                res = super(PrimateCloudInstance, sin_sello).write(
                    dict(vals, hosted_until=now))
            if resto:
                res = super(PrimateCloudInstance, resto).write(vals) and res
        else:
            if (vals.get("state") and vals["state"] != "archived"
                    and "hosted_until" not in vals
                    and self.filtered("hosted_until")):
                vals = dict(vals, hosted_until=False)  # reactivación: limpia sello
            res = super().write(vals)
        if retag:
            for env in envs:
                env._enqueue_client_tag_sync()
        for rec in self:
            if rec.id in domain_changed:
                rec._on_domain_changed(domain_changed[rec.id])
        return res

    def unlink(self):
        """Una instancia importada no se borra NUNCA (regla 2026-10-22):
        es el reflejo de un Odoo real funcionando."""
        self._pcm_assert_not_imported(_("eliminar la instancia"))
        return super().unlink()

    # --- Desmontar instancia (destructivo, triple confirmación) ---------
    def action_open_destroy_wizard(self):
        """Desmonta ESTE Odoo del servidor (unit, nginx, conf, filestore y su
        base): triple confirmación obligatoria (regla dura 2026-10-22)."""
        self.ensure_one()
        self._pcm_assert_not_imported(_("desmontar la instancia"))
        return {
            "type": "ir.actions.act_window",
            "name": _("Desmontar instancia — confirmación destructiva"),
            "res_model": "primate.cloud.destruction.wizard",
            "view_mode": "form", "target": "new",
            "context": {
                "dest_model": self._name, "dest_res_id": self.id,
                "dest_action": "instance_destroy",
                "dest_callback": "_destruction_enqueue_destroy",
            },
        }

    def _destruction_impact(self, action):
        self.ensure_one()
        db = self.database_id
        return {"resource_name": self.display_name, "text": "\n".join([
            _("Acción: DESMONTAR la instancia Odoo (systemd, nginx, conf, "
              "filestore) y BORRAR su base — irreversible."),
            _("Instancia: %(n)s (slug %(s)s)%(prod)s",
              n=self.display_name, s=self.slug or "?",
              prod=_(" ⚠ PRODUCCIÓN") if self.env_type == "production" else ""),
            _("Servidor: %(srv)s · Cuenta: %(acc)s",
              srv=self.environment_id.display_name,
              acc=self.account_id.display_name if hasattr(self, "account_id")
              and self.account_id else self.environment_id.account_id
              .display_name),
            _("Base: %(db)s · Dominio: %(d)s",
              db=db.display_name or "—", d=self.main_url or "—"),
        ])}

    def _destruction_enqueue_destroy(self):
        self.ensure_one()
        ctx = self.env.context
        job_token = self.env["primate.cloud.destruction.ticket"]\
            .consume_for_job(self, "instance_destroy",
                             ctx.get("destruction_ticket_id"),
                             ctx.get("destruction_token"))
        self.with_delay(
            description=_("Desmontar instancia: %s") % self.display_name
        ).job_destroy(destruction_ticket_id=ctx.get("destruction_ticket_id"),
                      destruction_job_token=job_token)
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"type": "info",
                       "message": _("Desmontaje confirmado y encolado."),
                       "next": {"type": "ir.actions.act_window_close"}},
        }

    def job_destroy(self, destruction_ticket_id=None,
                    destruction_job_token=None):
        """Job destructivo: corre el teardown por slug (D-R3.3) con drop de
        SU base, y archiva los registros. NO arranca sin el job_token."""
        self.ensure_one()
        self._pcm_assert_not_imported(_("desmontar la instancia"))
        self.env["primate.cloud.destruction.ticket"].burn_job_token(
            self, "instance_destroy",
            destruction_ticket_id, destruction_job_token)
        env_rec = self.environment_id
        machine = self._machine_required()
        ssm = machine._get_ssm_service()
        db_name = self.database_id.name if self.database_id else None
        script = env_rec._build_instance_teardown_script(
            self, db_name=db_name, drop_db=bool(db_name))
        if isinstance(script, (list, tuple)):
            script = "\n".join(script)
        output = ssm.run_script(
            machine.aws_instance_id, script,
            region=machine.region or env_rec.account_id.default_region,
            comment="pcm instance destroy: %s" % (self.slug or self.id),
            timeout=600, check=False)
        ok = "PCM_TEARDOWN_DONE" in (output.get("stdout") or "")
        env_rec._log("instance_teardown",
                     name=_("Desmontar instancia: %s") % self.display_name,
                     result="success" if ok else "failed",
                     error_message=None if ok else (
                         output.get("stderr") or output.get("status")),
                     record=self)
        if ok:
            if self.database_id:
                self.database_id.sudo().state = "deleted" \
                    if "deleted" in dict(self.database_id._fields["state"]
                                         .selection) else self.database_id.state
            self.write({"state": "archived"})
            self.message_post(body=_(
                "Instancia desmontada del servidor (teardown por slug) con "
                "triple confirmación."))
        else:
            self.message_post(body=_(
                "El desmontaje FALLÓ: %s") % (output.get("stderr")
                                              or output.get("status") or "?"))
        return ok

    def _hosted_days_in_period(self, period_start, period_end):
        """Días que la instancia estuvo hospedada dentro de ``[period_start,
        period_end)`` — el peso del prorrateo del reparto de costos (R5-B2).

        hosted_from = ``create_date`` (la instancia no se mueve de servidor);
        hosted_until = el campo, o ``period_end`` si sigue viva. El solapamiento
        es ``min(until, period_end) - max(from, period_start)``; 0 si no se
        solapa (creada después del período o archivada antes). ``period_*`` son
        ``date``; se comparan contra las fechas de las marcas de tiempo.
        """
        self.ensure_one()
        created = (self.create_date.date() if self.create_date else period_start)
        from_date = max(created, period_start)
        until = (self.hosted_until.date() if self.hosted_until else period_end)
        to_date = min(until, period_end)
        return max(0, (to_date - from_date).days)

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

    # --- DNS best-effort (R4-B6.4) ---
    def _set_dns_pending(self, reason, params):
        """Guarda el motivo + params del DNS que falló (para reintentar)."""
        self.ensure_one()
        self.sudo().dns_pending = json.dumps({
            "reason": (reason or "")[:400],
            "hosted_zone_id": params.get("hosted_zone_id") or "",
            "ttl": params.get("ttl") or 300,
            "domain": params.get("domain") or self.main_url or "",
            "record_type": "A",
        })

    def _dns_pending_data(self):
        """Dict del DNS pendiente (o False). Consumido por el serializer."""
        self.ensure_one()
        if not self.dns_pending:
            return False
        try:
            return json.loads(self.dns_pending)
        except (ValueError, TypeError):
            return False

    def action_retry_dns(self):
        """Reintenta SOLO el registro DNS pendiente (R4-B6.4, §8.1 cond 3).

        Reusa ``environment._provision_dns`` con los params guardados, SIN
        re-correr ningún install (cero riesgo de PCM_ERR_DIRTY_SLUG: la
        instancia ya existe). Éxito → limpia ``dns_pending`` + crea el
        registro real. Fallo → refresca el motivo (sigue reintentable). Va
        por queue_job (muta Route53).
        """
        self.ensure_one()
        self._pcm_assert_not_imported(_("crear DNS"))
        if not self.dns_pending:
            raise UserError(_("Esta instancia no tiene un DNS pendiente."))
        self.with_delay(
            description=_("Reintentar DNS: %s") % self.display_name
        ).job_retry_dns()
        return True

    def job_retry_dns(self):
        """Job: crea el registro DNS pendiente reusando el flujo de provisioning."""
        self.ensure_one()
        self._pcm_assert_not_imported(_("crear DNS"))
        data = self._dns_pending_data()
        if not data:
            return False
        env = self.environment_id
        machine = self._machine()
        if not machine:
            self.message_post(body=_(
                "No se pudo crear el DNS: el servidor no tiene máquina activa."))
            return False
        try:
            base = self.account_id._get_aws_service()
            env._provision_dns(
                base, self.account_id, machine,
                {"create_dns": True, "hosted_zone_id": data["hosted_zone_id"],
                 "ttl": data["ttl"], "domain": data["domain"]},
                data["domain"], instance=self)
            self.sudo().dns_pending = False   # éxito → limpia el flag
            self.message_post(body=_("Registro DNS creado (reintento)."))
            return True
        except Exception as error:  # noqa: BLE001 - se anota, sigue reintentable
            self._set_dns_pending(str(error), data)
            self.message_post(body=_(
                "El reintento de DNS falló otra vez: %s.") % error)
            return False

    # --- Solo lectura de importados (regla dura 2026-10-22) -------------
    # Campos de INFRAESTRUCTURA de una instancia importada: tocarlos cambiaría
    # el gemelo o dispararía side-effects (main_url → cambio de dominio real).
    # Lo anotable (project_id para clasificar, notes, cost_weight) sigue
    # permitido: vive solo en PCM y no toca el ambiente real.
    _PCM_INFRA_FIELDS = frozenset({
        "main_url", "slug", "http_port", "gevent_port", "conf_path",
        "service_name", "data_dir", "addons_dir", "pg_user", "python_bin",
        "odoo_bin", "log_path", "odoo_version", "odoo_edition",
        "database_id", "ssl_state", "ssl_cert_expiry",
    })

    def pcm_is_imported(self):
        """¿Esta instancia es importada? (máquina adoptada o entorno con
        marca de origen importada — Cloudpepper no tiene máquina AWS)."""
        self.ensure_one()
        if self.environment_id and self.environment_id.pcm_origin != "pcm":
            return True
        machine = self._machine()
        return bool(machine) and machine.management_state == "adopted_readonly"

    def _pcm_assert_not_imported(self, accion):
        """Corta (y audita) cualquier intento de modificar un importado."""
        for inst in self:
            if not inst.pcm_is_imported():
                continue
            durable_log(self.env, {
                "action_type": "readonly_block", "result": "failed",
                "name": _("Bloqueado (importado): %(a)s sobre %(n)s",
                          a=accion, n=inst.display_name),
                "user_id": self.env.uid,
                "resource_model": inst._name, "resource_id": inst.id,
                "error_message": _("Recurso importado: solo lectura."),
            })
            raise UserError(_(
                "«%(n)s» es una instancia IMPORTADA (solo lectura): no se "
                "puede %(a)s, sin excepción ni override — es un ambiente "
                "real funcionando.", n=inst.display_name, a=accion))

    # --- HTTPS (D-R3.9 endurecida) ------------------------------------
    def _public_url(self):
        """La URL real de ESTA instancia según su estado SSL (ajuste A del
        OK de HTTPS): https SOLO con certificado activo y verificado; con
        dominio sin cert, http por dominio; sin dominio, http por IP.

        OJO con el nombre: ``public_url`` (sin guion) ya existe en la API
        pública del bundle y delega en ``_impersonate_base_url`` — que ahora
        delega acá, así que TODOS los links salen de esta única verdad."""
        self.ensure_one()
        if self.main_url:
            scheme = "https" if self.ssl_state == "active" else "http"
            return "%s://%s" % (scheme, self.main_url.rstrip("/"))
        machine = self._machine()
        return "http://%s" % (machine.public_ip or "")

    def _set_ssl_error(self, state, reason):
        """Deja el estado + motivo SSL (recortado) sin romper nada más."""
        self.ensure_one()
        self.sudo().write({
            "ssl_state": state,
            "ssl_last_error": (reason or "")[:400],
        })

    def action_enable_ssl(self):
        """Botón «Activar HTTPS ahora»: rearma el ciclo completo (propagación
        → certbot → verificación real) vía queue_job. Sirve para montajes
        viejos que quedaron en HTTP y para reintentar un ``failed``."""
        self.ensure_one()
        self._pcm_assert_not_imported(_("emitir HTTPS (certbot)"))
        if not self.main_url:
            raise UserError(_(
                "La instancia no tiene dominio (URL principal): sin dominio "
                "no hay Let's Encrypt."))
        self._machine_required()
        self.sudo().write({
            "ssl_state": "pending",
            "ssl_retry_count": 0,
            "ssl_last_error": False,
            "ssl_pending_since": fields.Datetime.now(),
        })
        self.with_delay(
            description=_("Activar HTTPS: %s") % self.display_name
        ).job_enable_ssl()
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"type": "info", "sticky": False,
                       "message": _("Emisión de HTTPS encolada para %s: "
                                    "propagación, certbot y verificación.")
                       % self.main_url},
        }

    def job_enable_ssl(self):
        """Job: un ciclo de _provision_ssl para ESTA instancia."""
        self.ensure_one()
        self._pcm_assert_not_imported(_("emitir HTTPS (certbot)"))
        return self.environment_id._provision_ssl(self)

    def _on_domain_changed(self, old_domain):
        """Punto F: al cambiar el dominio, el cert viejo se borra, el vhost se
        reescribe con el server_name nuevo y se reemite para el nuevo."""
        self.ensure_one()
        self.message_post(body=_(
            "Dominio cambiado: %(old)s → %(new)s. Se reescribe el vhost, se "
            "borra el certificado viejo y se reemite HTTPS para el nuevo.",
            old=old_domain, new=self.main_url))
        self.sudo().write({
            "ssl_state": "pending" if self.main_url else "none",
            "ssl_retry_count": 0,
            "ssl_cert_expiry": False,
            "ssl_last_error": False,
            "ssl_expiry_warned": False,
            "ssl_pending_since": fields.Datetime.now(),
        })
        self.with_delay(
            description=_("Cambio de dominio: %s") % self.display_name
        ).job_apply_domain_change(old_domain)

    def job_apply_domain_change(self, old_domain):
        """Job: aplica el cambio de dominio en el servidor + reemite SSL."""
        self.ensure_one()
        self._pcm_assert_not_imported(_("cambiar el dominio"))
        return self.environment_id._apply_instance_domain_change(
            self, old_domain)

    # --- Dominio definitivo: los dos caminos (decisión 2026-10-27) -----
    def _open_domain_wizard(self, mode):
        self.ensure_one()
        self._pcm_assert_not_imported(_("cambiar el dominio"))
        return {
            "type": "ir.actions.act_window",
            "res_model": "primate.cloud.domain.wizard",
            "view_mode": "form",
            "target": "new",
            "name": (_("Crear DNS ahora (Route 53)") if mode == "route53"
                     else _("Asociar dominio existente")),
            "context": {"default_instance_id": self.id, "default_mode": mode},
        }

    def action_open_create_dns_wizard(self):
        """Botón «Crear DNS ahora»: PCM crea el registro A en Route 53 y
        cambia la instancia al dominio nuevo."""
        return self._open_domain_wizard("route53")

    def action_open_attach_domain_wizard(self):
        """Botón «Asociar dominio existente»: el registro lo cargó el usuario
        (Route 53 a mano u otro DNS); PCM solo verifica y cambia."""
        return self._open_domain_wizard("manual")

    def _domain_switch_failed(self, reason, code):
        """Deja el motivo a la vista (la instancia sigue como estaba)."""
        self.ensure_one()
        self.sudo().write({"domain_switch_state": "failed",
                           "domain_switch_error": (reason or "")[:400]})
        self.message_post(body=reason)
        return code

    def _domain_switch_start(self, domain):
        self.ensure_one()
        if self.domain_switch_state == "running":
            raise UserError(_(
                "Ya hay un cambio de dominio en curso para «%(i)s» (hacia "
                "%(d)s): esperá a que termine.", i=self.display_name,
                d=self.domain_switch_target))
        self.sudo().write({"domain_switch_target": domain,
                           "domain_switch_state": "running",
                           "domain_switch_error": False})

    def job_create_dns_and_switch(self, domain, hosted_zone_id, ttl=300):
        """Job (camino PCM): registro A en Route 53 → INSYNC → propagación
        contra los NS de la zona → cambio no destructivo."""
        self.ensure_one()
        self._pcm_assert_not_imported(_("crear DNS"))
        env_rec = self.environment_id
        machine = self._machine()
        self.sudo().write({"domain_switch_target": domain,
                           "domain_switch_state": "running",
                           "domain_switch_error": False})
        try:
            if not machine or not machine.public_ip:
                raise UserError(_(
                    "El servidor no tiene máquina activa con IP pública."))
            machine._exigir_gestion_habilitada("dns_write")
            base = self.account_id._get_aws_service()
            change_id = env_rec._provision_dns(
                base, self.account_id, machine,
                {"create_dns": True, "hosted_zone_id": hosted_zone_id,
                 "ttl": ttl or 300}, domain, instance=self)
            nameservers = aws_route53.AwsRoute53Service(
                base).get_zone_nameservers(hosted_zone_id)
        except Exception as error:  # noqa: BLE001 - se anota, no se traga
            env_rec._log("domain_switch", result="failed",
                         name=_("Crear DNS: %s") % domain,
                         error_message=str(error), record=self)
            return self._domain_switch_failed(_(
                "No se pudo crear el registro DNS de %(d)s: %(e)s. No se "
                "cambió nada.", d=domain, e=error), "dns_failed")
        try:
            return env_rec._switch_domain(
                self, domain, change_id=change_id, nameservers=nameservers,
                wait_propagation=DOMAIN_PROPAGATION_WAIT)
        except Exception as error:  # noqa: BLE001 - nunca queda «en curso»
            return self._domain_switch_failed(_(
                "El cambio a %(d)s se cortó por un error: %(e)s. Revisá el "
                "servidor antes de reintentar.", d=domain, e=error),
                "error")

    def job_attach_domain(self, domain):
        """Job (camino manual o reintento): el registro ya existe; verifica
        que apunte al servidor y cambia sin destruir. Si el registro es de
        Route 53 y PCM lo conoce, consulta sus NS autoritativos."""
        self.ensure_one()
        self._pcm_assert_not_imported(_("cambiar el dominio"))
        env_rec = self.environment_id
        nameservers = None
        self.sudo().write({"domain_switch_target": domain,
                           "domain_switch_state": "running",
                           "domain_switch_error": False})
        record = self.env["primate.cloud.dns.record"].sudo().search(
            [("name", "=", domain), ("hosted_zone_id", "!=", False),
             ("instance_id", "=", self.id)], limit=1)
        if record:
            try:
                nameservers = aws_route53.AwsRoute53Service(
                    self.account_id._get_aws_service()).get_zone_nameservers(
                        record.hosted_zone_id)
            except Exception:  # noqa: BLE001 - cae a resolvers públicos
                nameservers = None
        try:
            return env_rec._switch_domain(
                self, domain, nameservers=nameservers,
                wait_propagation=DOMAIN_PROPAGATION_WAIT if record else 0)
        except Exception as error:  # noqa: BLE001 - nunca queda «en curso»
            return self._domain_switch_failed(_(
                "El cambio a %(d)s se cortó por un error: %(e)s. Revisá el "
                "servidor antes de reintentar.", d=domain, e=error),
                "error")

    def action_retry_domain_switch(self):
        """Reintenta el cambio al dominio que quedó en camino (el registro DNS
        ya existe: no se vuelve a crear)."""
        self.ensure_one()
        self._pcm_assert_not_imported(_("cambiar el dominio"))
        domain = self.domain_switch_target
        if not domain:
            raise UserError(_("No hay un cambio de dominio para reintentar."))
        self._domain_switch_start(domain)
        self.with_delay(
            description=_("Cambiar dominio: %s") % self.display_name
        ).job_attach_domain(domain)
        return True

    def action_dismiss_domain_switch(self):
        """Descarta el aviso de un cambio de dominio que no se pudo hacer."""
        self.ensure_one()
        if self.domain_switch_state == "running":
            raise UserError(_("El cambio de dominio sigue en curso."))
        self.sudo().write({"domain_switch_target": False,
                           "domain_switch_state": False,
                           "domain_switch_error": False})
        return True

    @api.model
    def _cron_ssl(self):
        """Cron (cada 10 min): (1) trabaja los ``pending`` — PRIMERO re-chequea
        propagación y solo si está confirma corre certbot (ajuste C: esperar
        no consume intento); (2) vigila vencimientos de los ``active``
        (<%(d)s días → aviso una sola vez)."""
        Param = self.env["ir.config_parameter"].sudo()
        max_hours = int(Param.get_param("pcm.ssl_pending_max_hours", "24"))
        for inst in self.search([("ssl_state", "=", "pending"),
                                 ("state", "=", "active")]):
            if inst.pcm_is_imported():
                # Regla dura: NINGÚN cron toca un importado — ni certbot.
                continue
            try:
                result = inst.environment_id._provision_ssl(inst)
            except Exception as error:  # noqa: BLE001 — un fallo no corta el cron
                inst._set_ssl_error("pending", str(error))
                continue
            if (result == "not_propagated" and inst.ssl_pending_since
                    and fields.Datetime.now() - inst.ssl_pending_since
                    > timedelta(hours=max_hours)):
                inst._set_ssl_error("failed", _(
                    "El dominio no propagó en %(h)s horas: revisá el registro "
                    "DNS y reintentá con «Activar HTTPS ahora».", h=max_hours))
                inst.message_post(body=inst.ssl_last_error)
        self._cron_ssl_expiry_watch()

    @api.model
    def _cron_ssl_expiry_watch(self):
        """Certificados activos por vencer (<15 días): re-lee el vencimiento
        del servidor (la renovación pudo haber pasado) y, si sigue corto,
        avisa UNA vez — un cert que se acerca al vencimiento con el timer de
        certbot sano no debería existir."""
        limit = fields.Datetime.now() + timedelta(days=15)
        for inst in self.search([("ssl_state", "=", "active"),
                                 ("ssl_cert_expiry", "!=", False),
                                 ("ssl_cert_expiry", "<", limit)]):
            try:
                inst.environment_id._ssl_refresh_cert_expiry(inst)
            except Exception:  # noqa: BLE001 — sin lectura, vale el dato viejo
                pass
            if inst.ssl_cert_expiry and inst.ssl_cert_expiry < limit:
                if not inst.ssl_expiry_warned:
                    inst.message_post(body=_(
                        "⚠ El certificado HTTPS vence el %(d)s y certbot no lo "
                        "renovó todavía: revisar certbot.timer en el servidor.",
                        d=inst.ssl_cert_expiry))
                    inst.sudo().ssl_expiry_warned = True
            elif inst.ssl_expiry_warned:
                inst.sudo().ssl_expiry_warned = False

    def _machine_required(self):
        """Como :meth:`_machine`, pero exige que exista (operaciones SSM)."""
        machine = self._machine()
        if not machine:
            raise UserError(_(
                "El servidor de «%s» no tiene una máquina activa.")
                % self.display_name)
        return machine

    # ------------------------------------------------------------------
    # API canónica del panel POR INSTANCIA (R4-B2, D-R4.1). La mecánica
    # SSM vive en la máquina (ec2); estos wrappers fijan el contrato que
    # la pantalla de instancia (R4-B6) consume: siempre MIS rutas.
    # ------------------------------------------------------------------
    def fetch_logs(self, source="odoo", **kwargs):
        """Logs de ESTE Odoo (tail de su log_path; fallback su unit)."""
        return self._machine_required().fetch_logs(
            source, odoo_instance=self, **kwargs)

    def fetch_logs_stream(self, source="odoo", **kwargs):
        """Streaming incremental de logs de ESTE Odoo."""
        return self._machine_required().fetch_logs_stream(
            source, odoo_instance=self, **kwargs)

    def fetch_config(self):
        """Lee MI odoo.conf (allowlist + hash), read-only síncrono."""
        return self._machine_required().fetch_config(odoo_instance=self)

    def action_save_config(self, edits, expected_hash, typed_name=None):
        """Valida y encola el guardado de MI conf (reinicia MI unit)."""
        return self._machine_required().action_save_config(
            edits, expected_hash, typed_name=typed_name, odoo_instance=self)

    # ------------------------------------------------------------------
    # Impersonación POR INSTANCIA (R4-B3): claves + firma + token.
    # La mecánica remota (deploy/enable/list/nginx) vive en la máquina
    # (ec2) parametrizada por esta instancia; la IDENTIDAD criptográfica
    # (par de claves, firma, claim instance_ref) vive acá.
    # ------------------------------------------------------------------
    def _ensure_impersonate_keys(self):
        """Genera el par Ed25519 de ESTA instancia si falta (D-R4.9: al primer
        deploy/enable, NUNCA en create). Devuelve (priv_enc, pub_b64)."""
        self.ensure_one()
        if self.impersonate_privkey_encrypted and self.impersonate_pubkey:
            return self.impersonate_privkey_encrypted, self.impersonate_pubkey
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey)
        priv = Ed25519PrivateKey.generate()
        priv_raw_b64 = base64.b64encode(
            priv.private_bytes_raw()).decode("ascii")
        pub_b64 = base64.b64encode(
            priv.public_key().public_bytes_raw()).decode("ascii")
        priv_enc = crypto.encrypt(
            self.account_id._get_encryption_key(), priv_raw_b64)
        # sudo: los campos de clave están restringidos a group_cloud_admin;
        # el job/flujo puede correr con otro usuario técnico.
        self.sudo().write({
            "impersonate_privkey_encrypted": priv_enc,
            "impersonate_pubkey": pub_b64,
        })
        return priv_enc, pub_b64

    def impersonate_public_key(self):
        """Clave pública (base64) de esta instancia (para su companion)."""
        self.ensure_one()
        return self._ensure_impersonate_keys()[1]

    def _sign_impersonate(self, payload_bytes):
        """Firma el payload con la privada Ed25519 de ESTA instancia."""
        self.ensure_one()
        priv_enc, _pub = self._ensure_impersonate_keys()
        priv_raw = base64.b64decode(
            crypto.decrypt(self.account_id._get_encryption_key(), priv_enc))
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey)
        priv = Ed25519PrivateKey.from_private_bytes(priv_raw)
        return priv.sign(payload_bytes)

    def _make_impersonate_token(self, db, uid, login, admin_ack=False):
        """Arma y FIRMA (Ed25519) el token de un solo uso, corto (60 s).

        Lleva el claim ``instance_ref`` (el pcm_ref inmutable de ESTA
        instancia): el companion lo valida contra su sys-param local además
        de la firma → defensa en profundidad del aislamiento cruzado.
        """
        self.ensure_one()
        payload = {
            "db": db, "uid": int(uid), "login": login,
            "instance_ref": self.pcm_ref,
            "env_id": self.environment_id.id,
            "exp": time.time() + 60,
            "nonce": uuid.uuid4().hex,
            "admin": self.env.user.login,
        }
        if admin_ack:
            payload["admin_ack"] = True
        payload_bytes = json.dumps(
            payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signature = self._sign_impersonate(payload_bytes)

        def b64url(raw):
            return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return "%s.%s" % (b64url(payload_bytes), b64url(signature))

    def _impersonate_base_url(self):
        """URL base de ESTE Odoo. https SOLO con ssl_state=active (ajuste A):
        antes asumía https por tener dominio y el link nacía roto sin cert."""
        return self._public_url()

    # --- Refresh de staging POR INSTANCIA (R4-B6.4 / D-B6.6) ---
    def _resolve_refresh_origin(self):
        """(instancia_origen, base_origen) para refrescar ESTE staging.

        D-B6.6 — fallback explícito, NUNCA la primaria a ciegas (ese era el
        ``[:1]``):
        1. Instance-based (B5): ``self.origin_instance_id`` seteado → esa
           instancia y su ``database_id`` (verdad nueva, inequívoca).
        2. Env-based (Fase 8): sin ``origin_instance_id`` pero el entorno del
           staging tiene ``staging_origin_database_id`` → derivar la instancia
           origen de esa BD (``.instance_id``, por el mixin R1), no la primaria.
        3. Ninguno → fail claro (indicá la instancia de origen).
        """
        self.ensure_one()
        if self.origin_instance_id:
            origin = self.origin_instance_id
            return origin, origin.database_id
        env = self.environment_id
        if env.env_type == "staging" and env.staging_origin_database_id:
            db = env.staging_origin_database_id
            if db.instance_id:
                return db.instance_id, db
        raise UserError(_(
            "Este staging no tiene un origen registrado de forma inequívoca: "
            "indicá la instancia de origen (no se adivina la primaria)."))

    def action_refresh_staging(self, use_last_backup=False, neutralize=True):
        """Refresca la base de ESTE staging desde su instancia de origen (B6.4).

        Reusa el pipeline backup/restore por-instancia de B4: backup del
        origen → restore sobre la BD de este staging (con neutralización).
        Solo aplica a instancias ``env_type=staging``.
        """
        self.ensure_one()
        if self.env_type != "staging":
            raise UserError(_("Refrescar solo aplica a instancias de staging."))
        # Valida el origen ANTES de encolar (fail rápido si no resuelve).
        self._resolve_refresh_origin()
        self.with_delay(
            description=_("Refrescar staging: %s") % self.display_name
        ).job_refresh_staging_instance(
            {"use_last_backup": use_last_backup, "neutralize": neutralize})
        return True

    def job_refresh_staging_instance(self, params):
        """Job: copia la base del origen sobre este staging (pipeline B4)."""
        self.ensure_one()
        env = self.environment_id
        machine = self._machine()
        if not machine or not self.database_id:
            self.message_post(body=_(
                "No se puede refrescar: falta máquina activa o base del staging."))
            return False
        origin_inst, origin_db = self._resolve_refresh_origin()
        origin_env = origin_inst.environment_id
        origin_machine = origin_inst._machine()
        region = self.account_id.default_region
        source = origin_env._staging_source_backup(
            origin_env, origin_machine, origin_db, region,
            use_last_backup=params.get("use_last_backup"))
        ok = env.job_restore_backup({
            "backup_id": source.id, "db_name": self.database_id.name,
            "instance_id": machine.id, "odoo_instance_id": self.id,
            "pre_backup": False,
            "neutralize": params.get("neutralize", True)})
        if ok:
            self.message_post(body=_("Staging refrescado desde %s.")
                              % origin_inst.display_name)
        return ok

    # --- Wrappers canónicos remotos (la mecánica SSM vive en la máquina) ---
    def deploy_impersonate(self, dbs, pcm_ip=None):
        """Despliega el companion de ESTA instancia (su pubkey + su ref)."""
        return self._machine_required().deploy_impersonate(
            dbs, pcm_ip=pcm_ip, odoo_instance=self)

    def set_impersonate_enabled(self, dbs, enabled):
        """Habilita/deshabilita (kill-switch) la impersonación de ESTA instancia."""
        return self._machine_required().set_impersonate_enabled(
            dbs, enabled, odoo_instance=self)

    def list_db_users(self, db):
        """Usuarios internos activos de una BD de ESTA instancia (SSM psql)."""
        return self._machine_required().list_db_users(db, odoo_instance=self)

    def action_login_as(self, db, uid, login, is_admin_target=False,
                        admin_ack=False, typed_name=None):
        """Genera el enlace de impersonación de ESTA instancia (auditado)."""
        return self._machine_required().action_login_as(
            db, uid, login, is_admin_target=is_admin_target,
            admin_ack=admin_ack, typed_name=typed_name, odoo_instance=self)


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
