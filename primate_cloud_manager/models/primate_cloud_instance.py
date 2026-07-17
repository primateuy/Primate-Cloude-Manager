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

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools import crypto

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

    def write(self, vals):
        """Estampa ``hosted_until`` al archivar (choke point único del dato).

        Sin importar QUÉ flujo archiva la instancia (barrido, teardown, acción
        manual), el paso a ``archived`` deja registrado CUÁNDO dejó de estar
        hospedada — lo necesita el prorrateo por días del reparto (R5). Solo se
        estampa si está vacío (idempotente: re-archivar NO pisa la fecha real);
        si vuelve a un estado vivo se limpia (edge de reactivación). Un
        ``hosted_until`` explícito en el mismo write manda (no se toca).
        """
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
            return res
        if (vals.get("state") and vals["state"] != "archived"
                and "hosted_until" not in vals and self.filtered("hosted_until")):
            vals = dict(vals, hosted_until=False)   # reactivación: limpia el sello
        return super().write(vals)

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
        if not self.dns_pending:
            raise UserError(_("Esta instancia no tiene un DNS pendiente."))
        self.with_delay(
            description=_("Reintentar DNS: %s") % self.display_name
        ).job_retry_dns()
        return True

    def job_retry_dns(self):
        """Job: crea el registro DNS pendiente reusando el flujo de provisioning."""
        self.ensure_one()
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
        """URL base de ESTE Odoo (su dominio https, o la IP pública http)."""
        self.ensure_one()
        if self.main_url:
            return "https://%s" % self.main_url.rstrip("/")
        machine = self._machine()
        return "http://%s" % (machine.public_ip or "")

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
