# -*- coding: utf-8 -*-
"""Entorno: instalación específica de Odoo (objeto central del módulo).

En Fase 2 es un esqueleto que agrupa la infraestructura sincronizada (EC2,
bases, DNS). En Fase 4 gana el flujo de aprovisionamiento completo
(EC2 → DB → install_odoo.sh por SSM → nginx → SSL → DNS), todo async vía
queue_job. La trazabilidad (Fase 5) y el staging (Fase 7) se agregan luego.
"""
import json
import logging
import re
import secrets
import shlex
import uuid
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import file_open

from ..services import aws_base, aws_ec2, aws_rds, aws_route53, aws_s3, aws_ssm, github_api
from ..tools import bus, crypto, dates
from .primate_cloud_ec2_instance import CUSTOM_ADDONS_DIR

_logger = logging.getLogger(__name__)

# Rutas (relativas al addons-path) de las plantillas corridas por SSM.
INSTALL_SCRIPT_PATH = "primate_cloud_manager/data/install_odoo.sh"
NEUTRALIZATION_SQL_PATH = "primate_cloud_manager/data/neutralization.sql"

# Ventana máxima (horas) para dar por cumplida cada frecuencia esperada.
# Todo se interpreta y compara en UTC (decisión de Fase 8: los Datetime de Odoo,
# el cron y los timestamps de AWS son UTC). El margen sobre la frecuencia
# nominal absorbe corrimientos del cron y duración del propio backup.
BACKUP_FREQUENCY_WINDOW_HOURS = {"daily": 26, "twice_daily": 14, "hourly": 2}

# Umbrales de monitoreo (Fase 9). CPU en %; status check >= 1 = falla real.
MONITOR_CPU_WARN = 80.0
MONITOR_CPU_CRIT = 95.0

# Edad máxima (horas) de la evidencia para poder evaluarla. Evidencia más vieja
# (o sin marca temporal) => "No verificable": NUNCA "Cumple" sobre datos viejos,
# es el peor falso positivo posible en respaldos. En el flujo normal la evidencia
# se recolecta en vivo en el mismo job, así que esto es una guarda de contrato
# para cualquier llamada futura con datos cacheados.
BACKUP_EVIDENCE_MAX_AGE_HOURS = 24

# Ruta del filestore en las instancias aprovisionadas por PCM (install_odoo.sh
# crea el usuario 'odoo' con home /opt/odoo y data_dir default de Odoo).
BACKUP_FILESTORE_BASE = "/opt/odoo/.local/share/Odoo/filestore"

# Config de Odoo en las instancias aprovisionadas (para leer credenciales de
# BD in-situ cuando el origen es RDS: nunca viajan por SSM ni por logs).
ODOO_CONF_PATH = "/etc/odoo/odoo.conf"

# Nombre de base PostgreSQL admitido en flujos de backup/restore/staging
# (viaja dentro de SQL y de comandos shell: nada de comillas ni metacaracteres).
DB_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")


class PrimateCloudEnvironment(models.Model):
    """Combinación de infraestructura, base de datos y configuración de un Odoo."""

    _name = "primate.cloud.environment"
    _description = "Entorno Cloud"
    _inherit = ["mail.thread"]
    _order = "name"

    name = fields.Char(string="Nombre", required=True, tracking=True)
    # D6/R1: el entorno es el SERVIDOR y puede ser COMPARTIDO entre clientes
    # → deja de pertenecer obligatoriamente a un proyecto. Este campo queda
    # como "dueño" informativo para servidores dedicados; los clientes reales
    # del servidor son los proyectos de sus instancias (project_ids).
    project_id = fields.Many2one(
        "primate.cloud.project",
        string="Proyecto (dueño, dedicados)",
        required=False,
        ondelete="set null",
        help="Dueño informativo para servidores dedicados. Los clientes que "
             "efectivamente usan el servidor salen de sus instancias.",
    )
    project_ids = fields.Many2many(
        "primate.cloud.project", string="Proyectos hospedados",
        compute="_compute_project_ids",
        help="Proyectos (clientes) con instancias en este servidor.",
    )
    account_id = fields.Many2one(
        "primate.cloud.account",
        string="Cuenta AWS",
        compute="_compute_account_id",
        store=True,
        readonly=False,
        ondelete="restrict",
        help="Heredada del proyecto; puede sobrescribirse.",
    )
    # --- Delegación compat R1 (D6): la IDENTIDAD del Odoo vive en la
    # instancia primaria; el entorno la delega para que flujos/vistas/
    # serializers sigan funcionando hasta que R2/R4 los recableen.
    # related store=True → buscable y escribible (write-through).
    env_type = fields.Selection(
        related="primary_instance_id.env_type", string="Tipo",
        store=True, readonly=False,
    )
    state = fields.Selection(
        [
            ("draft", "Borrador"),
            ("provisioning", "Aprovisionando"),
            ("active", "Activo"),
            ("error", "Error"),
            ("archived", "Archivado"),
        ],
        string="Estado",
        default="draft",
        required=True,
        tracking=True,
    )
    odoo_version = fields.Selection(
        related="primary_instance_id.odoo_version", string="Versión Odoo",
        store=True, readonly=False,
    )
    odoo_edition = fields.Selection(
        related="primary_instance_id.odoo_edition", string="Edición Odoo",
        store=True, readonly=False,
    )
    main_url = fields.Char(
        related="primary_instance_id.main_url", string="URL principal",
        store=True, readonly=False, help="Ej.: forum.primate.cloud",
    )

    # --- Instancias (D6/R1): los Odoo montados en este servidor ---
    instance_ids = fields.One2many(
        "primate.cloud.instance", "environment_id", string="Instancias"
    )
    primary_instance_id = fields.Many2one(
        "primate.cloud.instance", string="Instancia primaria",
        compute="_compute_primary_instance_id", store=True,
        help="La primera instancia no archivada del servidor. Sostiene la "
             "delegación compat de R1; deja de ser especial en R2/R4.",
    )
    # La MÁQUINA del servidor (1:1 cuando PCM la gestiona). El One2many
    # ec2_instance_ids se conserva para historial (máquinas terminadas).
    ec2_instance_id = fields.Many2one(
        "primate.cloud.ec2.instance", string="Máquina AWS",
        ondelete="set null", copy=False,
    )
    ec2_instance_ids = fields.One2many(
        "primate.cloud.ec2.instance", "environment_id", string="Máquinas AWS"
    )
    database_ids = fields.One2many(
        "primate.cloud.database", "environment_id", string="Bases de datos"
    )
    dns_record_ids = fields.One2many(
        "primate.cloud.dns.record", "environment_id", string="Registros DNS"
    )
    repository_ids = fields.One2many(
        "primate.cloud.repository", "environment_id", string="Repositorios"
    )
    deployment_ids = fields.One2many(
        "primate.cloud.deployment", "environment_id", string="Despliegues"
    )

    creation_date = fields.Datetime(
        string="Fecha de creación", default=fields.Datetime.now, readonly=True
    )
    active = fields.Boolean(string="Activo", default=True)
    notes = fields.Text(string="Notas")

    # Identificador ESTABLE de atribución de costos (Fase 9, Opción A). Se
    # genera una vez y no cambia al renombrar: es la clave del tag AWS
    # `primate:environment_id`, con la que Cost Explorer agrupa. copy=False
    # para que un duplicado NO herede el ref (huérfanaría su atribución).
    pcm_ref = fields.Char(
        string="Ref estable", readonly=True, copy=False, index=True,
        help="Identificador inmutable para atribución de costos (tag "
             "primate:environment_id). No cambia al renombrar el entorno.",
    )

    _pcm_ref_uniq = models.Constraint(
        "UNIQUE(pcm_ref)", "El identificador estable (pcm_ref) debe ser único."
    )

    # --- Respaldos (Fase 8) — delegados a la instancia primaria desde R1;
    #     los jobs/crons siguen leyendo/escribiendo por acá hasta R4 ---
    backup_policy_id = fields.Many2one(
        related="primary_instance_id.backup_policy_id",
        string="Política de respaldo", store=True, readonly=False,
        help="Política esperada. El validador la compara contra lo detectado en AWS "
             "y los backups ejecutados por PCM.",
    )
    backup_ids = fields.One2many(
        "primate.cloud.backup", "environment_id", string="Backups"
    )
    backup_compliance = fields.Selection(
        related="primary_instance_id.backup_compliance",
        string="Cumplimiento de respaldo", store=True, readonly=False,
    )
    backup_compliance_detail = fields.Text(
        related="primary_instance_id.backup_compliance_detail",
        string="Detalle de cumplimiento", store=True, readonly=False,
    )
    last_backup_check = fields.Datetime(
        related="primary_instance_id.last_backup_check",
        string="Última verificación de respaldo", store=True, readonly=False,
    )

    # --- Monitoreo (Fase 9): estado general del entorno por umbrales ---
    monitor_state = fields.Selection(
        [
            ("unknown", "Sin datos"),
            ("ok", "Operativo"),
            ("warn", "Advertencia"),
            ("critical", "Crítico"),
        ],
        string="Estado de monitoreo", compute="_compute_monitor_state",
        help="Se calcula de la última métrica de las instancias: status check "
             "fallido o CPU > 95% = Crítico; CPU > 80% = Advertencia.",
    )

    # --- Staging (Fase 7) ---
    origin_environment_id = fields.Many2one(
        "primate.cloud.environment", string="Entorno origen", readonly=True,
        ondelete="set null",
        help="Para staging: entorno del que se clonó (normalmente producción).",
    )
    staging_ids = fields.One2many(
        "primate.cloud.environment", "origin_environment_id", string="Stagings"
    )
    staging_creation_date = fields.Datetime(string="Creación del staging", readonly=True)
    staging_created_by = fields.Many2one(
        "res.users", string="Staging creado por", readonly=True
    )
    staging_origin_backup = fields.Char(
        string="Backup de origen", readonly=True,
        help="Referencia (key S3) del dump usado para crear/refrescar el staging.",
    )
    # Origen EXPLÍCITO del staging (Fase 8, Bloque 5): de qué instancia y BD
    # del entorno origen salió. Reemplaza la convención [:1]; el refresh los
    # reusa para resolver determinísticamente.
    staging_origin_instance_id = fields.Many2one(
        "primate.cloud.ec2.instance", string="Instancia de origen",
        readonly=True, ondelete="set null",
    )
    staging_origin_database_id = fields.Many2one(
        "primate.cloud.database", string="BD de origen",
        readonly=True, ondelete="set null",
    )
    staging_neutralization_log = fields.Text(
        string="Log de neutralización", readonly=True
    )

    # Última configuración de los wizards (cifrada), para pre-cargarlos al
    # reintentar tras un error sin re-tipear todo. provision_config: del wizard
    # de aprovisionar (sobre este entorno). staging_config: del wizard de crear
    # staging (este entorno como ORIGEN).
    provision_config_encrypted = fields.Char(
        string="Config de aprovisionamiento (cifrada)", copy=False,
        groups="primate_cloud_manager.group_cloud_admin",
    )
    staging_config_encrypted = fields.Char(
        string="Config de staging (cifrada)", copy=False,
        groups="primate_cloud_manager.group_cloud_admin",
    )

    @staticmethod
    def _new_pcm_ref():
        """Genera un identificador estable único (Fase 9, Opción A)."""
        return "pcm_env_" + uuid.uuid4().hex

    def _cost_attribution_refs(self):
        """Devuelve ``(environment_ref, client_ref)`` para los tags estables.

        Materializa el ref del partner (lazy) AL taggear. **Degrada limpio** si
        falta proyecto o partner: devuelve ``client_ref = False`` (el tag de
        cliente no se emite) — NUNCA rompe el aprovisionamiento. Un recurso sin
        tag de cliente es un problema de reporte (tolerable); un provision que
        aborta por un ref de cliente ausente, NO.
        """
        self.ensure_one()
        partner = self.project_id.partner_id
        client_ref = False
        if partner:
            try:
                client_ref = partner._ensure_pcm_ref()
            except Exception:  # noqa: BLE001 - un ref ausente no aborta el provision
                _logger.warning(
                    "No se pudo materializar el pcm_ref del partner %s; el "
                    "recurso queda sin tag de cliente.", partner.id)
                client_ref = False
        return self.pcm_ref, client_ref

    # Campos de identidad del Odoo que desde R1 viven en la instancia. Si
    # vienen en el create del entorno (flujos/tests previos a D6), se
    # extraen y se crean EN la instancia primaria que nace con el entorno.
    INSTANCE_DELEGATED_FIELDS = [
        "env_type", "odoo_version", "odoo_edition", "main_url",
        "backup_policy_id", "backup_compliance", "backup_compliance_detail",
        "last_backup_check",
    ]

    @api.model_create_multi
    def create(self, vals_list):
        """Asigna pcm_ref y hace nacer el entorno CON su instancia primaria.

        pcm_ref: en create, no default lambda, para garantizar un valor
        DISTINTO por registro en un create en lote.

        D6/R1: "crear un entorno" = servidor + su primer Odoo. Los campos
        delegados que vengan en vals se mueven a la instancia (la verdad vive
        ahí; el entorno los expone por related). Si no hay proyecto no se
        puede crear la instancia (project_id es required en ella): con campos
        delegados presentes se corta con error claro, no se pierden en
        silencio.
        """
        delegated_list = []
        for vals in vals_list:
            if not vals.get("pcm_ref"):
                vals["pcm_ref"] = self._new_pcm_ref()
            delegated_list.append({
                key: vals.pop(key)
                for key in list(vals) if key in self.INSTANCE_DELEGATED_FIELDS
            })
        records = super().create(vals_list)
        Instance = self.env["primate.cloud.instance"]
        for record, delegated in zip(records, delegated_list):
            if record.project_id:
                Instance.create(dict(delegated, name=record.name,
                                     project_id=record.project_id.id,
                                     environment_id=record.id))
            elif delegated:
                raise UserError(_(
                    "El entorno «%s» trae datos de instancia (%s) pero no "
                    "tiene proyecto: asigná el proyecto (cliente) o creá la "
                    "instancia explícitamente."
                ) % (record.name, ", ".join(delegated)))
        return records

    @api.depends("instance_ids", "instance_ids.state", "instance_ids.active")
    def _compute_primary_instance_id(self):
        """Primera instancia no archivada (por id); sostiene la delegación R1."""
        for environment in self:
            instances = environment.instance_ids.filtered(
                lambda i: i.state != "archived") or environment.instance_ids
            environment.primary_instance_id = instances[:1]

    @api.depends("instance_ids.project_id")
    def _compute_project_ids(self):
        for environment in self:
            environment.project_ids = environment.instance_ids.project_id

    def _save_provision_config(self, values, field="provision_config_encrypted"):
        """Guarda (cifrada) la última config de un wizard en el campo indicado."""
        self.ensure_one()
        key = self.env["primate.cloud.account"]._get_encryption_key()
        self.sudo()[field] = crypto.encrypt(key, json.dumps(values))

    def _load_provision_config(self, field="provision_config_encrypted"):
        """Devuelve la última config guardada del campo indicado (o {} si no hay)."""
        self.ensure_one()
        blob = self.sudo()[field]
        if not blob:
            return {}
        key = self.env["primate.cloud.account"]._get_encryption_key()
        try:
            return json.loads(crypto.decrypt(key, blob) or "{}")
        except Exception:  # noqa: BLE001 - config corrupta: se ignora
            return {}

    @api.depends("project_id")
    def _compute_account_id(self):
        """Propone la cuenta del proyecto, sin pisar una elección manual previa."""
        for environment in self:
            if not environment.account_id and environment.project_id:
                environment.account_id = environment.project_id.account_id

    @api.depends("ec2_instance_ids.last_cpu",
                 "ec2_instance_ids.last_status_check_failed",
                 "ec2_instance_ids.last_metric_date")
    def _compute_monitor_state(self):
        """Estado del entorno a partir del cache de métricas de sus instancias
        (spec §12.3). 'Sin datos' si ninguna instancia tiene métricas aún."""
        for environment in self:
            insts = environment.ec2_instance_ids.filtered("last_metric_date")
            if not insts:
                environment.monitor_state = "unknown"
                continue
            state = "ok"
            for inst in insts:
                if (inst.last_status_check_failed or 0) >= 1 \
                        or (inst.last_cpu or 0) >= MONITOR_CPU_CRIT:
                    state = "critical"
                    break
                if (inst.last_cpu or 0) >= MONITOR_CPU_WARN:
                    state = "warn"
            environment.monitor_state = state

    # ------------------------------------------------------------------
    # Aprovisionamiento (Fase 4): botón -> wizard -> job
    # ------------------------------------------------------------------
    def action_provision(self):
        """Abre el wizard de aprovisionamiento (captura EC2 + DB + DNS).

        El botón no toca AWS: solo presenta el wizard. La confirmación del wizard
        encola la cadena servidor → instancia (ver :meth:`job_provision_server`).
        """
        self.ensure_one()
        if self.state not in ("draft", "error"):
            raise UserError(
                _("Solo se puede aprovisionar un entorno en estado Borrador o Error.")
            )
        return {
            "type": "ir.actions.act_window",
            "name": _("Aprovisionar entorno"),
            "res_model": "primate.cloud.provision.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_environment_id": self.id},
        }

    def _fill_network_from_discovery(self, params, account, region):
        """Completa la red (SG/subnet/profile) desde el descubrimiento de la región.

        REGLA (auto-discovery): un valor EXPLÍCITO del wizard GANA; el discovery
        solo completa lo VACÍO. Si falta lo estructural (VPC/subnet/rol), levanta
        el mensaje accionable (no aprovisiona a ciegas). Asegura el SG y COMMITEA
        enseguida → el advisory lock se libera antes del provisioning largo (no
        serializa 15 min a dos usuarios de la misma región).
        """
        self.ensure_one()
        sg = params.get("security_group_ids")
        subnet = params.get("subnet_id")
        profile = params.get("instance_profile")
        if sg and subnet and profile:
            return params   # red totalmente explícita → override, no tocar
        setup = self.env["primate.cloud.region.setup"].get_or_discover(
            account, region)
        if setup.status != "ok":
            raise UserError(setup.detail or _("La región no está lista para "
                                              "aprovisionar."))
        from .primate_cloud_region_setup import PCM_SSM_ROLE
        filled = dict(params)
        if not profile:
            filled["instance_profile"] = PCM_SSM_ROLE
        if not subnet:
            filled["subnet_id"] = setup.subnet_id
        if not sg:
            # Solo asegura el SG si el caché no lo tiene ya (evita una llamada
            # AWS por provisión). El lock de sesión con unlock explícito adentro
            # no retiene el lock durante el provisioning largo (no serializa 15 min).
            if not setup.security_group_id:
                setup.job_ensure_security_group()
            filled["security_group_ids"] = (
                [setup.security_group_id] if setup.security_group_id else [])
        return filled

    def _enqueue_provision(self, params):
        """Valida, deja el entorno en 'provisioning' y encola el flujo completo.

        Args:
            params (dict): parámetros de aprovisionamiento del wizard.
        """
        self.ensure_one()
        if not self.account_id:
            raise UserError(_("El entorno necesita una cuenta AWS para aprovisionar."))
        self.state = "provisioning"
        # Token de idempotencia: viaja en los args del job (queue_job los persiste),
        # así un requeue tras reiniciar el server NO crea una EC2 duplicada.
        params = dict(params, client_token="pcm-%s" % uuid.uuid4().hex)
        params = self._ensure_transient_db_password(params)
        self.with_delay(
            description=_("Aprovisionar entorno: %s") % self.name
        ).job_provision_server(params)

    @api.model
    def _ensure_transient_db_password(self, params):
        """Genera la contraseña PG transitoria del camino cero-config si falta.

        Con PostgreSQL local y sin contraseña dada (el usuario final no la ve),
        el usuario PG quedaría con password vacía y Odoo no puede autenticarse
        por TCP (``fe_sendauth: no password supplied`` en loop). Se genera al
        ENCOLAR (queue_job persiste los args → un requeue reusa la MISMA, igual
        que el ``client_token``) y NO se guarda en Odoo: vive solo en el
        ``odoo.conf`` de la instancia.
        """
        if params.get("db_mode") == "local_pg" and not params.get("db_password"):
            params = dict(params, db_password=secrets.token_urlsafe(24))
        return params

    # ------------------------------------------------------------------
    # Helpers de servicios / script
    # ------------------------------------------------------------------
    def _log(self, action_type, result="success", error_message=None,
             aws_request_id=None, name=None, record=None):
        """Atajo para registrar en la bitácora (por defecto sobre el entorno)."""
        return self.env["primate.cloud.operation.log"].log_operation(
            action_type, name=name, record=record if record is not None else self,
            result=result, error_message=error_message, aws_request_id=aws_request_id,
        )

    @api.model
    def _render_install_script(self, tokens):
        """Lee la plantilla install_odoo.sh y reemplaza los tokens %%...%%.

        Args:
            tokens (dict): ``{"ODOO_VERSION": "19", ...}``.

        Returns:
            str: el script listo para correr por SSM.
        """
        with file_open(INSTALL_SCRIPT_PATH, "r") as script_file:
            script = script_file.read()
        for key, value in tokens.items():
            script = script.replace("%%%%%s%%%%" % key, str(value or ""))
        return script

    # ------------------------------------------------------------------
    # Jobs de aprovisionamiento (flujo 7.1, partido en R2: D6)
    # ------------------------------------------------------------------
    def job_provision_server(self, params):
        """Job R2 (1/2): aprovisiona el SERVIDOR (red + EC2) y encadena el install.

        Crear un entorno = servidor + su primera instancia (D6). Este job hace
        la parte de MÁQUINA (auto-discovery de red + EC2) y encola
        :meth:`job_install_instance` con los MISMOS params. La cadena queda
        persistida en queue_job: si el install falla, el servidor NO queda en
        limbo — el entorno pasa a 'error' con la máquina viva y
        :meth:`action_retry_install` reintenta SOLO la instalación (el
        client_token protege además contra re-crear la EC2 si ESTE job se
        re-ejecuta).

        Returns:
            bool: True si el servidor quedó creado y el install encolado.
        """
        self.ensure_one()
        account = self.account_id
        bus.provision_start(self.env, self, title=_("Aprovisionando: %s") % self.name)
        try:
            base = account._get_aws_service()
            region = params.get("region") or account.default_region
            domain = params.get("domain") or self.main_url or self.name

            # 0. Completar la red desde el descubrimiento (auto-discovery): SG/
            #    subnet/profile vacíos se resuelven de la región; override gana.
            params = self._fill_network_from_discovery(params, account, region)

            # 1. Crear la máquina del servidor --------------------------------
            bus.provision_step(self.env, self, _("Creando el servidor (EC2)…"))
            self._provision_ec2(base, account, params, region, domain)
        except Exception as error:  # noqa: BLE001 - se normaliza y se audita
            self.state = "error"
            if self.primary_instance_id:
                self.primary_instance_id.state = "error"
            self.message_post(body=_("Aprovisionamiento del servidor fallido: %s") % error)
            self._log("provision", name=_("Aprovisionar servidor: %s") % self.name,
                      result="failed", error_message=str(error))
            bus.provision_done(self.env, self, ok=False,
                               message=_("Aprovisionamiento fallido: %s") % error)
            return False

        # 2. Encadenar la instalación del primer Odoo (job separado: si falla,
        #    se reintenta sin tocar la EC2).
        self.with_delay(
            description=_("Instalar instancia: %s") % self.name
        ).job_install_instance(params)
        return True

    def job_install_instance(self, params, resume=False):
        """Job R2 (2/2): monta el Odoo (la instancia) en el servidor ya creado.

        Pasos: BD (RDS/local) → install_odoo.sh por SSM (nginx + SSL) → DNS →
        activar entorno e instancia. Ante error deja entorno E instancia en
        'error' con la máquina intacta: recuperable con
        :meth:`action_retry_install` (mismos args vía requeue — misma
        contraseña PG transitoria, sin EC2 nueva).

        Args:
            params (dict): parámetros del wizard (los mismos de la cadena).
            resume (bool): reanudación sobre infra existente — salta la
                creación de la BD (la RDS/local ya existe de un intento
                anterior; el paso local_pg es idempotente pero el RDS no).

        Returns:
            bool: True si la instancia quedó activa; False si falló.
        """
        self.ensure_one()
        account = self.account_id
        machine = self.ec2_instance_id or self.ec2_instance_ids.filtered(
            lambda m: m.instance_state != "terminated")[:1]
        instance_rec = self.primary_instance_id
        bus.provision_start(self.env, self,
                            title=_("Instalando Odoo: %s") % self.name)
        try:
            if not machine or machine.instance_state == "terminated":
                raise UserError(_(
                    "El servidor no tiene una máquina activa: reaprovisioná el "
                    "entorno (el wizard recuerda la configuración)."))
            base = account._get_aws_service()
            region = params.get("region") or machine.region or account.default_region
            domain = params.get("domain") or self.main_url or self.name
            if instance_rec:
                instance_rec.state = "installing"

            # 1. Base de datos (RDS / PostgreSQL local / ninguna) --------------
            if resume:
                db_host = "localhost" if params.get("db_mode") == "local_pg" \
                    else (params.get("db_host") or "localhost")
            else:
                bus.provision_step(self.env, self,
                                   _("Configurando la base de datos…"))
                db_host = self._provision_database(
                    base, account, machine, params, region)

            # 2. install_odoo.sh por SSM (instala Odoo, nginx, SSL) -----------
            bus.provision_step(self.env, self,
                               _("Instalando Odoo por SSM (puede tardar varios minutos)…"))
            self._provision_run_install(base, machine, params, region, db_host, domain)

            # 3. Registro DNS (A -> IP pública) -------------------------------
            bus.provision_step(self.env, self, _("Configurando DNS…"))
            self._provision_dns(base, account, machine, params, domain)
        except Exception as error:  # noqa: BLE001 - se normaliza y se audita
            self.state = "error"
            if instance_rec:
                instance_rec.state = "error"
            self.message_post(body=_("Instalación de la instancia fallida: %s") % error)
            self._log("provision", name=_("Instalar instancia: %s") % self.name,
                      result="failed", error_message=str(error))
            bus.provision_done(self.env, self, ok=False,
                               message=_("Instalación fallida: %s") % error)
            return False

        # 4. Activar entorno e instancia ---------------------------------------
        self.write({"state": "active", "main_url": domain})
        if instance_rec:
            vals = {"state": "active"}
            if not instance_rec.database_id and self.database_ids:
                vals["database_id"] = self.database_ids[:1].id
            instance_rec.write(vals)
        self.message_post(body=_("Entorno aprovisionado y activo en %s.") % domain)
        self._log("provision", name=_("Aprovisionar: %s") % self.name, result="success")
        bus.provision_done(self.env, self, ok=True,
                           message=_("Entorno activo en %s.") % domain)
        return True

    def action_retry_install(self):
        """Reintenta la instalación del Odoo sobre el servidor YA creado.

        Recuperación del estado "servidor sí, instancia no" (falló la 2ª mitad
        de la cadena): re-encola el job_install_instance FALLIDO con sus mismos
        args (queue_job los persiste → misma contraseña PG transitoria), sin
        crear otra EC2. Si no hay job fallido que reintentar, guía al wizard
        (que recuerda la configuración).
        """
        self.ensure_one()
        failed_job = self.env["queue.job"].search([
            ("model_name", "=", self._name),
            ("method_name", "=", "job_install_instance"),
            ("state", "=", "failed"),
        ], order="id desc").filtered(
            lambda j: self.id in j.records.ids)[:1]
        if not failed_job:
            raise UserError(_(
                "No hay una instalación fallida para reintentar acá: usá "
                "«Aprovisionar» (el wizard recuerda lo que ingresaste)."))
        self.state = "provisioning"
        failed_job.requeue()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info", "title": _("Reintentando"),
                "message": _("Se reintenta la instalación sobre el servidor "
                             "existente (no se crea otra EC2)."),
            },
        }

    def action_create_instance(self):
        """Agregar OTRO Odoo a este servidor — gate honesto hasta R3.

        La acción es visible para que el modelo destino se entienda desde ya,
        pero el install multi-Odoo (puertos/unit/nginx por slug sin tocar los
        Odoo vivos) es la fase R3 del recableo: hasta entonces se explica el
        porqué en lugar de ocultar el botón.
        """
        self.ensure_one()
        raise UserError(_(
            "Todavía no disponible: montar un segundo Odoo en un servidor "
            "existente requiere el layout multi-Odoo (fase R3 del recableo, "
            "en curso). Hoy cada servidor hospeda su instancia primaria; para "
            "un Odoo nuevo, creá un entorno."))

    def _provision_ec2(self, base, account, params, region, domain):
        """Crea la EC2 del entorno y registra el recurso. Devuelve la instancia."""
        ec2 = aws_ec2.AwsEc2Service(base)
        env_ref, client_ref = self._cost_attribution_refs()
        tags = aws_base.build_resource_tags(
            client=self.project_id.name,
            environment=self.name,
            client_ref=client_ref,
            environment_ref=env_ref,
            extra={"Name": params.get("instance_name") or domain},
        )
        # AMI: si el usuario lo dejó vacío, se resuelve para la región; si lo
        # cargó, se limpia (tolera corchetes/comillas/espacios pegados).
        image_id = aws_ec2.normalize_ami_id(params.get("image_id")) \
            or account.resolve_ubuntu_ami(region)
        data = ec2.create_instance(
            image_id=image_id,
            instance_type=params["instance_type"],
            tags=tags,
            region=region,
            disk_size_gb=params.get("disk_size_gb") or None,
            key_name=params.get("key_name") or None,
            security_group_ids=params.get("security_group_ids") or None,
            subnet_id=params.get("subnet_id") or None,
            instance_profile=params.get("instance_profile") or None,
            client_token=params.get("client_token") or None,
        )
        instance = self.env["primate.cloud.ec2.instance"]._register_provisioned(
            account, data, environment=self,
            os_type=params.get("os_type"), disk_size_gb=params.get("disk_size_gb"),
        )
        self.message_post(body=_("EC2 creada: %s (%s).")
                          % (instance.name, instance.aws_instance_id))
        self._log("ec2_create", name=_("Crear EC2: %s") % instance.name,
                  result="success", record=instance)
        return instance

    def _upsert_provisioned_database(self, vals):
        """Reusa el registro de BD del entorno por nombre, o lo crea (F5).

        El re-provision de un entorno NO debe duplicar el registro
        ``primate.cloud.database``: un registro viejo apuntando a una instancia
        ya terminada, sin backups, produce un falso "No cumple" en el validador.
        Se busca por (entorno, nombre) y se actualiza (reapuntando la instancia
        nueva); si no existe, se crea.

        Returns:
            recordset: el registro de BD (reusado o nuevo).
        """
        self.ensure_one()
        Database = self.env["primate.cloud.database"]
        existing = Database.search([
            ("environment_id", "=", self.id),
            ("name", "=", vals.get("name")),
        ], limit=1)
        if existing:
            existing.write(vals)
            return existing
        return Database.create(vals)

    def _provision_database(self, base, account, instance, params, region):
        """Crea la base (RDS o local) y devuelve el host de conexión para Odoo."""
        db_mode = params.get("db_mode") or "none"
        if db_mode == "rds":
            rds = aws_rds.AwsRdsService(base)
            env_ref, client_ref = self._cost_attribution_refs()
            tags = aws_base.build_resource_tags(
                client=self.project_id.name, environment=self.name,
                client_ref=client_ref, environment_ref=env_ref,
            )
            try:
                data = rds.create_instance(
                    identifier=params["rds_identifier"],
                    instance_class=params["rds_instance_class"],
                    storage_gb=params.get("rds_storage_gb") or 20,
                    master_username=params.get("db_user") or "odoo",
                    master_password=params["db_password"],
                    tags=tags,
                    engine_version=self._pg_engine_version(params.get("pg_version")),
                    multi_az=params.get("rds_multi_az") or False,
                    backup_retention_days=params.get("backup_retention_days") or 7,
                    region=region,
                )
            except Exception as error:  # noqa: BLE001 - solo AlreadyExists
                # Idempotencia del retry (R2): si la RDS quedó creada de un
                # intento anterior de la cadena, se REUSA (mismo patrón que el
                # InvalidGroup.Duplicate del SG). Cualquier otro error se
                # propaga.
                code = getattr(error, "response", {}).get(
                    "Error", {}).get("Code", "")
                if code != "DBInstanceAlreadyExists":
                    raise
                data = rds.get_instance(params["rds_identifier"], region=region)
                self.message_post(body=_(
                    "RDS ya existente (reintento): se reusa %s."
                ) % params["rds_identifier"])
            database = self._upsert_provisioned_database({
                "name": params["rds_identifier"],
                "account_id": account.id,
                "environment_id": self.id,
                "db_type": "rds",
                "rds_identifier": params["rds_identifier"],
                "rds_endpoint": data.get("endpoint") or False,
                "rds_instance_class": params["rds_instance_class"],
                "rds_storage_gb": params.get("rds_storage_gb") or 0,
                "rds_multi_az": params.get("rds_multi_az") or False,
                "backup_retention_days": params.get("backup_retention_days") or 0,
                "pg_version": params.get("pg_version") or False,
                "state": "available",
            })
            self.message_post(body=_("RDS creada: %s.") % database.name)
            self._log("rds_create", name=_("Crear RDS: %s") % database.name,
                      result="success", record=database)
            return data.get("endpoint") or "localhost"

        if db_mode == "local_pg":
            self._upsert_provisioned_database({
                "name": params.get("db_name") or self.name,
                "account_id": account.id,
                "environment_id": self.id,
                "db_type": "local_pg",
                "ec2_instance_id": instance.id,
                "pg_version": params.get("pg_version") or False,
                "state": "available",
            })
            return "localhost"

        # db_mode == "none": no se crea base; install_odoo.sh asume localhost.
        return "localhost"

    def _provision_run_install(self, base, instance, params, region, db_host, domain):
        """Renderiza y corre install_odoo.sh por SSM; deja la salida en el chatter."""
        ssm = aws_ssm.AwsSsmService(base)
        tokens = {
            "ODOO_VERSION": self.odoo_version or "19",
            "ODOO_EDITION": self.odoo_edition or "community",
            "DB_HOST": db_host,
            "DB_PORT": "5432",
            "DB_NAME": params.get("db_name") or self.name,
            "DB_USER": params.get("db_user") or "odoo",
            "DB_PASSWORD": params.get("db_password") or "",
            "DB_LOCAL": "1" if params.get("db_mode") == "local_pg" else "0",
            "DOMAIN": domain,
            "ADMIN_PASSWORD": params.get("admin_password") or "",
        }
        script = self._render_install_script(tokens)
        output = ssm.run_script(
            instance.aws_instance_id, script, region=region,
            comment="pcm provision: %s" % self.name,
            timeout=params.get("install_timeout") or 900,
        )
        ok = output.get("status") == "Success"
        self.message_post(body=_("Instalación Odoo (SSM) — estado: %s.") % output.get("status"))
        self._log("ssm_command", name=_("Instalar Odoo: %s") % self.name,
                  result="success" if ok else "failed",
                  error_message=None if ok else (output.get("stderr") or output.get("status")),
                  aws_request_id=output.get("command_id"), record=instance)
        if not ok:
            raise UserError(
                _("La instalación por SSM no terminó con éxito (estado: %s).")
                % output.get("status")
            )

    def _provision_dns(self, base, account, instance, params, domain):
        """Crea el registro DNS A apuntando a la IP pública (si se pidió)."""
        if not params.get("create_dns"):
            return
        if not instance.public_ip:
            self.message_post(body=_("Sin IP pública: se omite la creación del registro DNS."))
            return
        route53 = aws_route53.AwsRoute53Service(base)
        change_id = route53.create_record(
            hosted_zone_id=params["hosted_zone_id"],
            name=domain,
            record_type="A",
            value=instance.public_ip,
            ttl=params.get("ttl") or 300,
            comment="pcm provision: %s" % self.name,
        )
        record = self.env["primate.cloud.dns.record"].create({
            "name": domain,
            "account_id": account.id,
            "environment_id": self.id,
            "hosted_zone_id": params["hosted_zone_id"],
            "record_type": "A",
            "record_value": instance.public_ip,
            "ttl": params.get("ttl") or 300,
            "state": "active",
        })
        self.message_post(body=_("Registro DNS creado: %s -> %s.")
                          % (domain, instance.public_ip))
        self._log("dns_create", name=_("Crear DNS: %s") % domain,
                  result="success", aws_request_id=change_id, record=record)

    @staticmethod
    def _pg_engine_version(pg_version):
        """Mapea la versión mayor de PostgreSQL al EngineVersion de RDS, o None."""
        return pg_version or None

    def job_resume_provision(self, params):
        """Job: reintenta la instalación sobre la EC2 YA creada (recuperación).

        Compat/atajo histórico (pre-R2). Desde R2 delega en
        :meth:`job_install_instance` con ``resume=True`` (salta la creación de
        la BD — ya existe de un intento anterior) para que haya UNA sola
        implementación del install. No crea recursos nuevos.
        """
        self.ensure_one()
        self.state = "provisioning"
        return self.job_install_instance(params, resume=True)

    # ==================================================================
    # Staging (Fase 7): botón -> wizard -> flujo de 12 pasos
    # ==================================================================
    def action_create_staging(self):
        """Abre el wizard de creación de staging a partir de este entorno."""
        self.ensure_one()
        if self.state != "active":
            raise UserError(_("Solo se puede crear un staging desde un entorno activo."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Crear staging"),
            "res_model": "primate.cloud.staging.create.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_origin_environment_id": self.id},
        }

    def _enqueue_staging(self, params):
        """Crea el entorno staging (en 'provisioning') y encola su construcción.

        ``self`` es el entorno ORIGEN. Devuelve el entorno staging creado.
        """
        self.ensure_one()
        if not self.account_id:
            raise UserError(_("El entorno origen necesita una cuenta AWS."))
        # Origen explícito (Bloque 5): resuelve instancia+BD ya, con fallback
        # solo si es inequívoco, y los persiste en el staging para el refresh.
        origin_instance, origin_database = self._resolve_staging_origin(
            self.env["primate.cloud.ec2.instance"].browse(
                params.get("origin_instance_id") or []),
            self.env["primate.cloud.database"].browse(
                params.get("origin_database_id") or []),
        )
        staging = self.create({
            "name": params["name"],
            "project_id": self.project_id.id,
            "account_id": params.get("account_id") or self.account_id.id,
            "env_type": "staging",
            "state": "provisioning",
            "odoo_version": self.odoo_version,
            "odoo_edition": self.odoo_edition,
            "main_url": params.get("domain"),
            "origin_environment_id": self.id,
            "staging_origin_instance_id": origin_instance.id,
            "staging_origin_database_id": origin_database.id,
        })
        # R1 (D6): el vínculo instancia→instancia del staging (la verdad nueva);
        # los campos de flujo del entorno siguen vigentes hasta R4.
        if staging.primary_instance_id and self.primary_instance_id:
            staging.primary_instance_id.origin_instance_id = (
                self.primary_instance_id.id)
        # Token de idempotencia (ver _enqueue_provision): evita EC2 duplicadas
        # si el job de staging se re-ejecuta tras reiniciar el server.
        params = dict(params, client_token="pcm-%s" % uuid.uuid4().hex)
        params = self._ensure_transient_db_password(params)
        staging.with_delay(
            description=_("Crear staging: %s") % staging.name
        ).job_create_staging(params)
        return staging

    def action_refresh_staging(self):
        """Abre el wizard de refresco (opciones de la spec §14.5)."""
        self.ensure_one()
        if self.env_type != "staging":
            raise UserError(_("Refrescar solo aplica a entornos de tipo staging."))
        if not self.origin_environment_id:
            raise UserError(_("Este staging no tiene entorno origen registrado."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Refrescar staging"),
            "res_model": "primate.cloud.staging.refresh.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_staging_id": self.id},
        }

    def _resolve_staging_origin(self, instance=None, database=None):
        """Resuelve (instancia, BD) de este entorno para staging/refresh.

        Preferencia: los argumentos explícitos (elegidos en el wizard o
        persistidos en ``staging_origin_*``). Fallback SOLO si es inequívoco
        (exactamente una instancia y una base); con varias se exige elegir —
        se terminó el ``[:1]`` a ciegas del Bloque 7.

        Returns:
            tuple: (ec2.instance, database), ambos con exactamente 1 registro.
        """
        self.ensure_one()
        Instance = self.env["primate.cloud.ec2.instance"]
        Database = self.env["primate.cloud.database"]
        instance = (instance or Instance).exists()
        database = (database or Database).exists()
        if not instance and len(self.ec2_instance_ids) == 1:
            instance = self.ec2_instance_ids
        if not database and len(self.database_ids) == 1:
            database = self.database_ids
        if not instance or not database:
            raise UserError(_(
                "El entorno '%s' tiene varias (o ninguna) instancias/bases: "
                "indicá explícitamente cuál usar (elegilas en el wizard)."
            ) % self.name)
        return instance[:1], database[:1]

    # ------------------------------------------------------------------
    # Jobs de staging
    # ------------------------------------------------------------------
    def job_create_staging(self, params):
        """Job: flujo de 12 pasos para construir el staging (spec 7.2).

        ``self`` es el entorno staging. Reusa los helpers de aprovisionamiento
        (EC2, base vacía, instalación, DNS) y agrega los pasos propios de staging:
        copiar la base de origen, neutralizarla y clonar los repos al mismo commit.
        """
        self.ensure_one()
        origin = self.origin_environment_id
        account = self.account_id
        bus.provision_start(self.env, self, title=_("Creando staging: %s") % self.name)
        try:
            base = account._get_aws_service()
            region = params.get("region") or account.default_region
            domain = params.get("domain") or self.main_url or self.name
            staging_url = "https://%s" % domain

            bus.provision_step(self.env, self, _("Creando instancia EC2…"))
            instance = self._provision_ec2(base, account, params, region, domain)
            bus.provision_step(self.env, self, _("Configurando la base de datos…"))
            db_host = self._provision_database(base, account, instance, params, region)
            bus.provision_step(self.env, self, _("Instalando Odoo por SSM…"))
            self._provision_run_install(base, instance, params, region, db_host, domain)
            bus.provision_step(self.env, self,
                               _("Copiando la base desde el origen…"))
            # Pipeline de Bloques 3-4 (camino único): backup del origen +
            # restore estándar acá, que además neutraliza (destino no-prod).
            source_backup = self._staging_copy_via_backup(
                origin, instance, params, region
            )
            dump_key = source_backup.s3_key
            bus.provision_step(self.env, self, _("Clonando repositorios…"))
            self._staging_clone_repos(account, origin, instance, params, region)
            bus.provision_step(self.env, self, _("Configurando DNS…"))
            self._provision_dns(base, account, instance, params, domain)
            bus.provision_step(self.env, self, _("Reiniciando servicios…"))
            self._staging_restart(instance, region)
        except Exception as error:  # noqa: BLE001 - se normaliza y se audita
            self.state = "error"
            self.message_post(body=_("Creación de staging fallida: %s") % error)
            self._log("staging_create", name=_("Crear staging: %s") % self.name,
                      result="failed", error_message=str(error))
            bus.provision_done(self.env, self, ok=False,
                               message=_("Creación de staging fallida: %s") % error)
            return False

        self.write({
            "state": "active", "main_url": domain,
            "staging_creation_date": fields.Datetime.now(),
            "staging_created_by": self.env.uid,
            "staging_origin_backup": dump_key,
        })
        self.message_post(body=_("Staging creado y activo en %s.") % domain)
        self._log("staging_create", name=_("Crear staging: %s") % self.name, result="success")
        bus.provision_done(self.env, self, ok=True,
                           message=_("Staging activo en %s.") % domain)
        return True

    def job_refresh_staging(self, params=None):
        """Job: refresca el staging desde su origen (pipeline de Bloques 3-4).

        Opciones (wizard de refresco, spec §14.5) en ``params``:
        ``use_last_backup`` (no recargar producción), ``neutralize``
        (re-neutralizar, default True), ``refresh_repos`` (base+repos).
        Sin pre-backup del staging (v1): es descartable y el origen ya tiene
        su backup registrado.
        """
        self.ensure_one()
        params = params or {}
        origin = self.origin_environment_id
        if not origin:
            raise UserError(_("Este staging no tiene entorno origen registrado."))
        bus.provision_start(self.env, self,
                            title=_("Refrescando staging: %s") % self.name)
        try:
            region = origin.account_id.default_region
            origin_instance, origin_db = origin._resolve_staging_origin(
                self.staging_origin_instance_id, self.staging_origin_database_id,
            )
            # Destino: la instancia/BD propias del staging (vínculo explícito).
            target_instance, target_db = self._resolve_staging_origin()
            bus.provision_step(self.env, self,
                               _("Copiando la base desde el origen…"))
            source = self._staging_source_backup(
                origin, origin_instance, origin_db, region,
                use_last_backup=params.get("use_last_backup"),
                bucket=params.get("transfer_bucket"),
            )
            ok = self.job_restore_backup({
                "backup_id": source.id,
                "db_name": target_db.name,
                "instance_id": target_instance.id,
                "pre_backup": False,
                "neutralize": params.get("neutralize", True),
            })
            if not ok:
                raise UserError(_("Falló la restauración en el staging "
                                  "(ver bitácora)."))
            if params.get("refresh_repos"):
                bus.provision_step(self.env, self,
                                   _("Actualizando repositorios…"))
                self._staging_refresh_repos(origin, target_instance, region)
        except Exception as error:  # noqa: BLE001
            self.message_post(body=_("Refresco de staging fallido: %s") % error)
            self._log("staging_refresh", name=_("Refrescar staging: %s") % self.name,
                      result="failed", error_message=str(error))
            bus.provision_done(self.env, self, ok=False,
                               message=_("Refresco fallido: %s") % error)
            return False
        self.write({"staging_origin_backup": source.s3_key,
                    "staging_creation_date": fields.Datetime.now()})
        self.message_post(body=_("Staging refrescado desde %s.") % origin.display_name)
        self._log("staging_refresh", name=_("Refrescar staging: %s") % self.name)
        bus.provision_done(self.env, self, ok=True,
                           message=_("Staging refrescado desde %s.") % origin.display_name)
        return True

    # ------------------------------------------------------------------
    # Pasos específicos de staging
    # ------------------------------------------------------------------
    def _staging_source_backup(self, origin, origin_instance, origin_db,
                               region, use_last_backup=False, bucket=None):
        """Obtiene el backup fuente para crear/refrescar un staging.

        Con ``use_last_backup``: el último backup completado de la BD de
        origen (no recarga producción). Si no (o no hay ninguno): ejecuta un
        backup gestionado ad-hoc (Bloque 3, prefix ``staging``) en la
        instancia origen elegida — con soporte RDS por endpoint.
        """
        Backup = self.env["primate.cloud.backup"]
        if use_last_backup:
            last = Backup.search(
                [("database_id", "=", origin_db.id), ("state", "=", "completed")],
                order="backup_date desc", limit=1,
            )
            if last:
                return last
        bucket = bucket or origin.backup_policy_id.s3_bucket
        if not bucket:
            raise UserError(_(
                "No hay bucket S3 para el dump: indicá el bucket de "
                "transferencia o asigná al origen una política con bucket."))
        s3 = aws_s3.AwsS3Service(origin.account_id._get_aws_service())
        s3.ensure_bucket(bucket, region=region)
        source = origin._run_database_backup(
            origin_db, origin.backup_policy_id, bucket, "staging", region,
            instance=origin_instance, purpose="staging",
        )
        if source.state != "completed":
            raise UserError(_("Falló el backup del origen: %s")
                            % (source.error_message or "?"))
        return source

    def _staging_copy_via_backup(self, origin, dest_instance, params, region):
        """Copia origen→staging reusando el pipeline de Bloques 3-4.

        Un solo camino testeado: backup gestionado del origen (streaming, con
        filestore — el camino propio de Fase 7 no lo copiaba) + restore
        estándar en el destino, que además neutraliza (destino no-prod) y crea
        el registro de la BD del staging vinculado a su instancia.

        Devuelve el registro de backup usado como fuente (trazabilidad §14.4).
        """
        if not origin:
            raise UserError(_("El staging no tiene entorno origen para copiar la base."))
        origin_instance, origin_db = origin._resolve_staging_origin(
            self.staging_origin_instance_id, self.staging_origin_database_id,
        )
        source = self._staging_source_backup(
            origin, origin_instance, origin_db, region,
            use_last_backup=params.get("use_last_backup"),
            bucket=params.get("transfer_bucket"),
        )
        ok = self.job_restore_backup({
            "backup_id": source.id,
            "db_name": params.get("db_name") or self.name,
            "instance_id": dest_instance.id,
            # El destino es nuevo/descartable; el origen ya quedó respaldado.
            "pre_backup": False,
            "neutralize": True,
        })
        if not ok:
            raise UserError(_("Falló la restauración en el staging (ver bitácora)."))
        self.message_post(body=_("Base copiada desde %(origin)s (backup %(key)s).",
                                 origin=origin.display_name, key=source.s3_key))
        return source

    def _staging_refresh_repos(self, origin, instance, region):
        """Actualiza los repos del staging al commit actual del origen.

        A diferencia del clon inicial, acá los repos ya existen en el disco
        del staging: fetch + checkout, y se actualiza el registro espejo.
        """
        ssm = instance._get_ssm_service()
        for source_repo in origin.repository_ids:
            if not source_repo.github_url or not source_repo.local_path:
                continue
            ref = (source_repo.current_commit
                   or source_repo.configured_branch or "")
            output = ssm.run_script(
                instance.aws_instance_id,
                self._build_repo_update_script(source_repo.local_path, ref),
                region=instance.region or region,
                comment="pcm staging repo refresh: %s" % source_repo.name,
                timeout=900,
            )
            ok = output.get("status") == "Success"
            mirror = self.repository_ids.filtered(
                lambda r: r.github_url == source_repo.github_url
            )[:1]
            if mirror:
                mirror.current_commit = source_repo.current_commit if ok else False
        self.message_post(body=_("Repositorios actualizados desde el origen."))

    @staticmethod
    def _build_repo_update_script(path, ref):
        """fetch + checkout de un repo ya clonado en el staging."""
        quoted_path = shlex.quote(path)
        return "\n".join([
            "set -e",
            "cd %s" % quoted_path,
            "git fetch --all --tags --prune",
            "git checkout %s" % shlex.quote(ref),
        ])

    def _staging_neutralize(self, instance, params, region, staging_url):
        """Corre el SQL de neutralización en la base del staging (vía SSM)."""
        staging_db = params.get("db_name") or self.name
        sql = self._render_neutralization_sql(staging_url)
        output = instance._get_ssm_service().run_script(
            instance.aws_instance_id,
            self._build_neutralize_script(staging_db, sql),
            region=region, comment="pcm staging neutralize: %s" % self.name,
        )
        log_text = "%s\n%s" % (output.get("stdout") or "", output.get("stderr") or "")
        self.staging_neutralization_log = log_text.strip()
        if output.get("status") != "Success":
            raise UserError(_("Falló la neutralización: %s")
                            % (output.get("stderr") or output.get("status")))
        self.message_post(body=_("Base neutralizada para staging."))

    def _staging_clone_repos(self, account, origin, dest_instance, params, region):
        """Clona los repos del origen al mismo commit y los registra en el staging."""
        if not origin:
            return
        Repo = self.env["primate.cloud.repository"]
        ssm = dest_instance._get_ssm_service()
        for source in origin.repository_ids:
            if not source.github_url or not source.local_path:
                continue
            ref = source.current_commit or source.configured_branch or ""
            output = ssm.run_script(
                dest_instance.aws_instance_id,
                self._build_clone_script(source.github_url, source.local_path, ref),
                region=region, comment="pcm staging clone: %s" % source.name, timeout=900,
            )
            ok = output.get("status") == "Success"
            Repo.create({
                "name": source.name,
                "environment_id": self.id,
                "repo_type": source.repo_type,
                "github_url": source.github_url,
                "organization": source.organization,
                "configured_branch": source.configured_branch,
                "local_path": source.local_path,
                "current_commit": source.current_commit if ok else False,
            })
        self.message_post(body=_("Repositorios clonados desde el origen."))

    def _staging_restart(self, instance, region):
        """Reinicia los servicios (odoo + nginx) en el staging vía SSM."""
        instance._get_ssm_service().run_script(
            instance.aws_instance_id, self._build_restart_script(),
            region=region, comment="pcm staging restart: %s" % self.name,
        )

    @api.model
    def _render_neutralization_sql(self, staging_url):
        """Lee neutralization.sql y reemplaza el token %%STAGING_URL%%."""
        with file_open(NEUTRALIZATION_SQL_PATH, "r") as sql_file:
            sql = sql_file.read()
        return sql.replace("%%STAGING_URL%%", staging_url or "")

    # ------------------------------------------------------------------
    # Respaldos (Fase 8): validador de cumplimiento esperado vs detectado
    # ------------------------------------------------------------------
    @api.model
    def _cron_check_backup_compliance(self):
        """Cron diario: encola la verificación de cumplimiento por entorno.

        El cron solo selecciona y encola; la consulta a AWS va en queue_job.
        Los entornos sin política no consultan nada: se marcan directo.
        """
        environments = self.search([("backup_policy_id", "!=", False)])
        for environment in environments:
            environment.with_delay(
                description=_("Verificar respaldo: %s") % environment.name
            ).job_check_backup_compliance()
        no_policy = self.search([
            ("backup_policy_id", "=", False),
            ("backup_compliance", "!=", "no_policy"),
        ])
        if no_policy:
            no_policy.write({
                "backup_compliance": "no_policy",
                "backup_compliance_detail":
                    _("El entorno no tiene política de respaldo asignada."),
                "last_backup_check": fields.Datetime.now(),
            })
        _logger.info(
            "Verificación de respaldos encolada para %s entornos.", len(environments)
        )

    def action_check_backup_compliance(self):
        """Botón: encola la verificación de cumplimiento de este entorno."""
        self.ensure_one()
        if not self.backup_policy_id:
            raise UserError(_("Asigná una política de respaldo antes de verificar."))
        self.with_delay(
            description=_("Verificar respaldo: %s") % self.name
        ).job_check_backup_compliance()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "info",
                       "message": _("Verificación de respaldo encolada."),
                       "next": {"type": "ir.actions.act_window_close"}},
        }

    def job_check_backup_compliance(self):
        """Job: compara la política esperada contra la evidencia real.

        Persiste el resultado en los campos de cumplimiento y deja traza en la
        bitácora SOLO cuando el estado cambia (evita ruido con el cron diario).
        """
        self.ensure_one()
        previous = self.backup_compliance
        evidence = self._collect_backup_evidence()
        state, detail = self._evaluate_backup_compliance(
            self.backup_policy_id, evidence
        )
        self.write({
            "backup_compliance": state,
            "backup_compliance_detail": detail,
            "last_backup_check": fields.Datetime.now(),
        })
        if state != previous:
            result = {"ok": "success", "unverifiable": "partial"}.get(state, "failed")
            self._log(
                "backup_check",
                name=_("Verificar respaldo: %s") % self.name,
                result=result,
                error_message=detail if result == "failed" else None,
            )
            if state == "non_compliant":
                self.message_post(
                    body=_("El entorno NO cumple su política de respaldo "
                           "(%(policy)s):\n%(detail)s",
                           policy=self.backup_policy_id.name, detail=detail)
                )
        return state

    @staticmethod
    def _database_on_terminated_instance(database):
        """True si la BD apunta a una instancia EC2 terminada (huérfana, F5).

        Una BD cuya instancia está ``terminated`` es un registro sin infra viva
        detrás: no es un "incumplimiento" de respaldo, es "no aplica". El
        validador y el ejecutor la omiten para no generar falsos "No cumple" ni
        registros de backup fallidos perpetuos.
        """
        instance = database.ec2_instance_id
        return bool(instance) and instance.instance_state == "terminated"

    def _collect_backup_evidence(self):
        """Junta la evidencia real de respaldo, base por base.

        - RDS: consulta viva a AWS (retención configurada + último punto
          restaurable del backup automático) y refresca esos datos en el
          registro de la base.
        - PostgreSQL local: último backup ``completed`` del registro de PCM
          (`primate.cloud.backup`). La retención solo es verificable si la
          política es gestionada por PCM (el lifecycle S3 lo configura PCM
          con la retención de la política, ver Bloque 3).

        Returns:
            list[dict]: por base: ``{"name", "db_type", "last_backup"
            (datetime naive UTC | False), "retention_days" (int | None),
            "error" (str | None)}``.
        """
        self.ensure_one()
        Backup = self.env["primate.cloud.backup"]
        policy = self.backup_policy_id
        services_by_account = {}
        evidence = []
        for database in self.database_ids:
            # F5: una BD sobre instancia terminada es un registro huérfano
            # (típicamente de un re-provision viejo); se omite del cumplimiento
            # en vez de arrastrar el entorno a un falso "No cumple".
            if self._database_on_terminated_instance(database):
                evidence.append({
                    "name": database.name, "db_type": database.db_type,
                    "skipped": "instance_terminated",
                    "as_of": fields.Datetime.now(),
                })
                continue
            entry = {"name": database.name, "db_type": database.db_type,
                     "last_backup": False, "retention_days": None, "error": None,
                     "as_of": fields.Datetime.now()}
            if database.db_type == "rds" and database.rds_identifier:
                account = database.account_id
                try:
                    service = services_by_account.get(account.id)
                    if service is None:
                        service = aws_rds.AwsRdsService(account._get_aws_service())
                        services_by_account[account.id] = service
                    data = service.get_instance(
                        database.rds_identifier, region=account.default_region
                    ) or {}
                    entry["retention_days"] = data.get("backup_retention_days") or 0
                    entry["last_backup"] = dates.to_naive_utc(
                        data.get("latest_restorable_time")
                    )
                    database.write({
                        "backup_retention_days": entry["retention_days"],
                        "last_backup_date": entry["last_backup"] or False,
                    })
                except Exception as error:  # noqa: BLE001 - evidencia no verificable
                    entry["error"] = str(error)
            else:
                last = Backup.search(
                    [("database_id", "=", database.id), ("state", "=", "completed")],
                    order="backup_date desc", limit=1,
                )
                entry["last_backup"] = last.backup_date or False
                if policy.managed_by_pcm:
                    entry["retention_days"] = policy.expected_retention_days
                if last:
                    database.write({"last_backup_date": last.backup_date})
            evidence.append(entry)
        return evidence

    @api.model
    def _evaluate_backup_compliance(self, policy, evidence, now=None):
        """Regla PURA de comparación esperado vs detectado (spec §10.2).

        No consulta AWS ni escribe: recibe la política y la evidencia y decide.
        Todos los timestamps se comparan en UTC naive (como guarda Odoo).

        Args:
            policy (recordset): ``primate.cloud.backup.policy`` (o vacío).
            evidence (list[dict]): salida de :meth:`_collect_backup_evidence`.
            now (datetime, optional): inyectable para testear ventanas.

        Returns:
            tuple[str, str]: (estado de cumplimiento, detalle legible).
        """
        if not policy:
            return "no_policy", _("El entorno no tiene política de respaldo asignada.")
        if policy.policy_type == "none":
            return "ok", _("Política 'Sin respaldo gestionado': no se realiza comparación.")
        if not evidence:
            return "unverifiable", _("El entorno no tiene bases de datos registradas.")
        now = now or fields.Datetime.now()
        window_hours = BACKUP_FREQUENCY_WINDOW_HOURS.get(policy.expected_frequency)
        lines, states = [], []
        for entry in evidence:
            # F5: BD omitida (instancia terminada). Se muestra por transparencia
            # pero NO cuenta para el estado: no es cumple ni incumple.
            if entry.get("skipped"):
                lines.append(_("↷ %(db)s: instancia terminada, se omite del "
                               "cumplimiento", db=entry["name"]))
                continue
            if entry.get("error"):
                states.append("unverifiable")
                lines.append(_("? %(db)s: no verificable (%(error)s)",
                               db=entry["name"], error=entry["error"]))
                continue
            # Evidencia vieja o sin marca temporal: no se evalúa. Nunca dar
            # "Cumple" sobre datos viejos.
            as_of = entry.get("as_of")
            if not as_of or (now - as_of).total_seconds() \
                    > BACKUP_EVIDENCE_MAX_AGE_HOURS * 3600:
                states.append("unverifiable")
                lines.append(_(
                    "? %(db)s: evidencia %(when)s — más vieja que %(max)s h, "
                    "no evaluable",
                    db=entry["name"],
                    when=fields.Datetime.to_string(as_of) if as_of
                    else _("sin marca temporal"),
                    max=BACKUP_EVIDENCE_MAX_AGE_HOURS))
                continue
            state, problems = "ok", []
            if window_hours:
                last = entry.get("last_backup")
                if not last:
                    state = "non_compliant"
                    problems.append(_("sin backups registrados"))
                elif (now - last).total_seconds() > window_hours * 3600:
                    state = "non_compliant"
                    problems.append(_("último backup %(date)s (fuera de la ventana "
                                      "de %(hours)s h)",
                                      date=fields.Datetime.to_string(last),
                                      hours=window_hours))
            if policy.expected_retention_days:
                retention = entry.get("retention_days")
                if retention is None:
                    if state == "ok":
                        state = "unverifiable"
                    problems.append(_("retención no verificable"))
                elif retention < policy.expected_retention_days:
                    state = "non_compliant"
                    problems.append(_("retención %(real)s < %(expected)s días",
                                      real=retention,
                                      expected=policy.expected_retention_days))
            states.append(state)
            marker = {"ok": "✔", "non_compliant": "✖"}.get(state, "?")
            lines.append("%s %s: %s" % (
                marker, entry["name"], "; ".join(problems) or _("cumple")))
        # Prioridad: no cumple > no verificable > cumple.
        if "non_compliant" in states:
            overall = "non_compliant"
        elif "unverifiable" in states:
            overall = "unverifiable"
        else:
            overall = "ok"
        return overall, "\n".join(lines)

    # ------------------------------------------------------------------
    # Respaldos (Fase 8): ejecución de backups gestionados por PCM
    # ------------------------------------------------------------------
    @api.model
    def _backup_window_start(self, policy, now=None):
        """Inicio (UTC) de la ventana de ejecución vigente de la política.

        Un backup exitoso con fecha >= inicio de ventana significa "la ventana
        ya está cubierta". Todo en UTC (decisión de Fase 8): ``execution_hour``
        se interpreta en UTC igual que los Datetime de Odoo.

        Args:
            policy (recordset): política de respaldo.
            now (datetime, optional): inyectable para testear.

        Returns:
            datetime|bool: inicio de la ventana vigente, o False si la
            frecuencia no es programable (manual / sin frecuencia).
        """
        now = now or fields.Datetime.now()
        frequency = policy.expected_frequency
        if frequency == "hourly":
            return now.replace(minute=0, second=0, microsecond=0)
        if frequency not in ("daily", "twice_daily"):
            return False
        hour = int(policy.execution_hour or 0) % 24
        minute = int(((policy.execution_hour or 0) * 60) % 60)
        anchor = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if frequency == "daily":
            return anchor if anchor <= now else anchor - timedelta(days=1)
        # twice_daily: ventanas en execution_hour y execution_hour + 12; la
        # vigente es la más reciente que ya haya empezado.
        candidates = []
        for offset_hours in (0, 12):
            slot = anchor + timedelta(hours=offset_hours)
            candidates.extend([slot, slot - timedelta(days=1)])
        return max(slot for slot in candidates if slot <= now)

    @api.model
    def _cron_run_managed_backups(self):
        """Cron horario: encola el backup de los entornos cuya ventana venció.

        Reintenta como mucho una vez por hora mientras la ventana siga
        descubierta (sin tormenta de reintentos); cada intento fallido queda
        registrado. Un backup ``in_progress`` de la ventana también cuenta como
        cubierta para no encolar en paralelo.
        """
        now = fields.Datetime.now()
        Backup = self.env["primate.cloud.backup"]
        # Primero se liberan los in_progress zombis (job muerto sin cerrar):
        # si no, contarían como "ventana cubierta" y bloquearían el reintento.
        Backup._mark_stuck_failed(now)
        environments = self.search([
            ("state", "=", "active"),
            ("backup_policy_id.managed_by_pcm", "=", True),
        ])
        enqueued = 0
        for environment in environments:
            window_start = self._backup_window_start(
                environment.backup_policy_id, now
            )
            if not window_start:
                continue
            covered = Backup.search_count([
                ("environment_id", "=", environment.id),
                ("backup_type", "=", "pcm_dump"),
                ("state", "in", ("completed", "in_progress")),
                ("backup_date", ">=", window_start),
            ])
            if covered:
                continue
            environment.with_delay(
                description=_("Backup gestionado: %s") % environment.name
            ).job_run_backup()
            enqueued += 1
        _logger.info(
            "Backups gestionados encolados: %s de %s entornos.",
            enqueued, len(environments),
        )

    def action_run_backup(self):
        """Botón: encola un backup gestionado inmediato de este entorno."""
        self.ensure_one()
        policy = self.backup_policy_id
        if not policy or not policy.managed_by_pcm or not policy.s3_bucket:
            raise UserError(_(
                "El entorno necesita una política de respaldo gestionada por "
                "PCM (con bucket S3 de destino)."
            ))
        self.with_delay(
            description=_("Backup manual: %s") % self.name
        ).job_run_backup(trigger="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "info",
                       "message": _("Backup encolado."),
                       "next": {"type": "ir.actions.act_window_close"}},
        }

    def job_run_backup(self, trigger="scheduled"):
        """Job: backup gestionado del entorno (pg_dump + filestore vía SSM → S3).

        Cubre las bases **PostgreSQL locales** (las RDS las respalda AWS con su
        backup automático y el validador las verifica por esa vía). Cada base
        deja su registro en ``primate.cloud.backup``, también los intentos
        fallidos: son la evidencia del validador y la traza de reintentos.
        """
        self.ensure_one()
        policy = self.backup_policy_id
        if not policy or not policy.managed_by_pcm or not policy.s3_bucket:
            raise UserError(_("La política del entorno no está gestionada por PCM."))
        account = self.account_id
        region = account.default_region
        bucket = policy.s3_bucket
        prefix = (policy.s3_prefix or "pcm-backups").strip("/")
        try:
            # Bucket + regla de retención (lifecycle: AWS borra solo lo vencido).
            s3 = aws_s3.AwsS3Service(account._get_aws_service())
            s3.ensure_bucket(bucket, region=region)
            if policy.expected_retention_days:
                s3.put_lifecycle_rule(
                    bucket, prefix + "/", policy.expected_retention_days,
                    region=region,
                )
        except Exception as error:  # noqa: BLE001 - se audita y no se traga
            self._log("backup_run", name=_("Backup: %s") % self.name,
                      result="failed", error_message=str(error))
            self.message_post(
                body=_("Backup fallido preparando S3: %s") % error)
            return False

        # F5: se respaldan solo las BD locales con infra viva; las que apuntan a
        # una instancia terminada (huérfanas) se omiten en vez de generar un
        # registro de backup fallido en cada corrida.
        databases = self.database_ids.filtered(
            lambda d: d.db_type == "local_pg"
            and not self._database_on_terminated_instance(d)
        )
        results = [
            self._run_database_backup(
                database, policy, bucket, prefix, region,
                purpose="manual" if trigger == "manual" else "scheduled",
            ).state
            for database in databases
        ]
        completed = results.count("completed")
        if not results:
            result, summary = "success", _("Sin bases PostgreSQL locales que "
                                           "respaldar (las RDS las cubre AWS).")
        elif completed == len(results):
            result, summary = "success", _("%s base(s) respaldada(s).") % completed
        elif completed:
            result = "partial"
            summary = _("%(ok)s de %(total)s bases respaldadas; ver registros.",
                        ok=completed, total=len(results))
        else:
            result, summary = "failed", _("Falló el backup de todas las bases.")
        self._log("backup_run", name=_("Backup: %s") % self.name, result=result,
                  error_message=summary if result != "success" else None)
        if result != "success":
            self.message_post(body=_("Backup gestionado: %s") % summary)
        return result == "success"

    def _run_database_backup(self, database, policy, bucket, prefix, region,
                             instance=None, purpose="scheduled"):
        """Respalda UNA base. Devuelve el registro del backup.

        Decisiones del Bloque 3: chequeo barato del estado de la instancia
        ANTES de tocar SSM; nunca se enciende una instancia por un backup;
        todo intento (incluso fallido) queda registrado.

        Bloque 5: acepta un ejecutor explícito (``instance``) y bases RDS —
        el dump corre en esa EC2 apuntando al endpoint, con credenciales
        leídas in-situ del odoo.conf (nunca viajan por SSM ni logs).
        """
        Backup = self.env["primate.cloud.backup"].sudo()
        now = fields.Datetime.now()
        stamp = now.strftime("%Y%m%d-%H%M%S")
        key_base = "%s/%s/%s/%s/%s" % (
            prefix, self._backup_slug(self.project_id.name),
            self._backup_slug(self.name), self._backup_slug(database.name), stamp,
        )
        expiry = (now + timedelta(days=policy.expected_retention_days)
                  if policy.expected_retention_days else False)
        record = Backup.create({
            "name": _("Backup %(db)s %(stamp)s", db=database.name, stamp=stamp),
            "environment_id": self.id,
            "database_id": database.id,
            "backup_type": "pcm_dump",
            "source": "executed",
            "purpose": purpose,
            "backup_date": now,
            "s3_bucket": bucket,
            "s3_key": key_base + ".dump",
            "s3_filestore_key": key_base + "-filestore.tar.gz",
            "expiry_date": expiry,
        })
        instance = instance or database.ec2_instance_id
        if not instance:
            record.write({"state": "failed",
                          "error_message": _("La base no tiene una instancia "
                                             "EC2 asociada (ni se indicó un "
                                             "ejecutor).")})
            return record
        if instance.instance_state != "running":
            record.write({"state": "failed",
                          "error_message": _("Instancia %(name)s detenida "
                                             "(estado: %(state)s). No se "
                                             "enciende infra por un backup.",
                                             name=instance.name,
                                             state=instance.instance_state)})
            return record
        try:
            output = instance._get_ssm_service().run_script(
                instance.aws_instance_id,
                self._build_backup_script(
                    database.name, bucket,
                    record.s3_key, record.s3_filestore_key,
                    rds_endpoint=(database.rds_endpoint
                                  if database.db_type == "rds" else None),
                ),
                region=instance.region or region,
                comment="pcm backup: %s" % database.name,
                timeout=3600,
            )
        except Exception as error:  # noqa: BLE001 - SSM inaccesible: se registra
            record.write({"state": "failed",
                          "error_message": _("SSM inaccesible: %s") % error})
            return record
        stdout = output.get("stdout") or ""
        if output.get("status") != "Success" or "PCM_BACKUP_OK" not in stdout:
            record.write({"state": "failed",
                          "error_message": (output.get("stderr")
                                            or stdout or output.get("status"))})
            return record
        sizes = self._parse_backup_sizes(stdout)
        total_mb = (sizes.get("PCM_DUMP_SIZE_BYTES", 0)
                    + sizes.get("PCM_FS_SIZE_BYTES", 0)) / (1024.0 * 1024.0)
        record.write({"state": "completed", "size_mb": total_mb})
        database.write({"last_backup_date": now})
        return record

    @api.model
    def _build_backup_script(self, db_name, bucket, dump_key, filestore_key,
                             rds_endpoint=None):
        """Script SSM del backup gestionado (decisiones del Bloque 3).

        - **Streaming a S3**: el dump nunca toca el disco de la instancia
          (elimina el riesgo de llenar el disco de producción); ``aws s3 cp``
          aborta su multipart si el pipe falla (no quedan objetos a medias).
        - **Guarda de espacio** como cinturón (los temporales de pg_dump y
          aws-cli usan /tmp) + ``trap`` de limpieza en éxito o error.
        - **Cero credenciales**: peer auth local (``sudo -u postgres``) e
          instance profile para S3. El stdout solo lleva marcadores y tamaños.
        - **RDS** (Bloque 5, ``rds_endpoint``): el dump corre en la EC2
          ejecutora apuntando al endpoint; las credenciales se leen IN-SITU
          del odoo.conf de esa instancia (``PGPASSWORD`` en el mismo proceso,
          jamás en el input de SSM, el stdout ni la bitácora).
        """
        db = shlex.quote(db_name)
        bkt = shlex.quote(bucket)
        dump = shlex.quote(dump_key)
        filestore = shlex.quote(filestore_key)
        if rds_endpoint:
            dump_lines = [
                "DB_USER=$(awk -F' *= *' '/^db_user/ {print $2; exit}' %s)"
                % ODOO_CONF_PATH,
                "DB_PASSWORD=$(awk -F' *= *' '/^db_password/ {print $2; exit}' %s)"
                % ODOO_CONF_PATH,
                'PGPASSWORD="$DB_PASSWORD" pg_dump -h %s -U "$DB_USER" '
                "-Fc -d %s | aws s3 cp - s3://%s/%s --only-show-errors"
                % (shlex.quote(rds_endpoint), db, bkt, dump),
            ]
        else:
            dump_lines = [
                "sudo -u postgres pg_dump -Fc -d %s | "
                "aws s3 cp - s3://%s/%s --only-show-errors" % (db, bkt, dump),
            ]
        return self._wrap_bash("\n".join([
            "set -euo pipefail",
            "command -v aws >/dev/null 2>&1 || { echo \"PCM_ERROR: aws CLI no está instalado en la instancia (el install de PCM lo trae vía snap; instalalo o re-aprovisioná)\" >&2; exit 1; }",
            "FREE_MB=$(df -Pm /tmp | awk 'NR==2 {print $4}')",
            'if [ "$FREE_MB" -lt 1024 ]; then '
            'echo "PCM_ERROR: menos de 1 GB libre en /tmp (${FREE_MB} MB)" >&2; '
            "exit 1; fi",
            "trap 'rm -f /tmp/pcm_backup_* 2>/dev/null || true' EXIT",
            *dump_lines,
            'echo "PCM_DUMP_SIZE_BYTES=$(aws s3api head-object --bucket %s '
            '--key %s --query ContentLength --output text)"' % (bkt, dump),
            'FS_DIR=%s/%s' % (BACKUP_FILESTORE_BASE, db),
            'if [ -d "$FS_DIR" ]; then',
            '  tar -czf - -C "$(dirname "$FS_DIR")" "$(basename "$FS_DIR")" | '
            "aws s3 cp - s3://%s/%s --only-show-errors" % (bkt, filestore),
            '  echo "PCM_FS_SIZE_BYTES=$(aws s3api head-object --bucket %s '
            '--key %s --query ContentLength --output text)"' % (bkt, filestore),
            "else",
            '  echo "PCM_FS_SIZE_BYTES=0"',
            "fi",
            'echo "PCM_BACKUP_OK"',
        ]))

    @staticmethod
    def _wrap_bash(body):
        """Envuelve un script para que corra bajo bash vía heredoc.

        SSM (documento AWS-RunShellScript) ejecuta con ``/bin/sh`` = dash en
        Ubuntu, que NO soporta ``set -o pipefail``. Sin pipefail, un ``pg_dump``
        que falla en ``pg_dump | aws s3 cp`` dejaría subir un dump truncado sin
        error. El heredoc con marcador entrecomillado evita expansiones del
        shell externo; bash hace las suyas adentro.
        """
        return "bash <<'PCM_BASH_EOF'\n%s\nPCM_BASH_EOF" % body

    @staticmethod
    def _parse_backup_sizes(stdout):
        """Extrae los marcadores de tamaño (bytes) del stdout del script."""
        sizes = {}
        for line in (stdout or "").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key in ("PCM_DUMP_SIZE_BYTES", "PCM_FS_SIZE_BYTES"):
                try:
                    sizes[key] = int(value)
                except ValueError:
                    pass
        return sizes

    @staticmethod
    def _backup_slug(name):
        """Nombre seguro para keys S3 (sin espacios ni caracteres raros)."""
        return re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()) or "sin-nombre"

    # ------------------------------------------------------------------
    # Respaldos (Fase 8): restore (Bloque 4)
    # ------------------------------------------------------------------
    def job_restore_backup(self, params):
        """Job: restaura un backup sobre este entorno (el DESTINO).

        Regla de oro: **validar todo antes de dropear** — registro y objetos
        en S3, instancia corriendo, pre-backup del destino, espacio en disco y
        compatibilidad de versión PostgreSQL (estas dos últimas dentro del
        script, también antes del drop). Si algo falla DESPUÉS del drop, el
        mensaje incluye explícitamente la key del pre-backup: el operador ve
        su camino de recuperación sin buscarlo (sin auto-rollback en v1,
        decisión consciente).

        Args:
            params (dict): ``backup_id``, ``db_name``, ``instance_id``,
                ``pre_backup`` (bool).
        """
        self.ensure_one()
        backup = self.env["primate.cloud.backup"].browse(
            params["backup_id"]
        ).exists()
        db_name = params["db_name"]
        instance = self.env["primate.cloud.ec2.instance"].browse(
            params["instance_id"]
        ).exists()
        log_name = _("Restaurar %(backup)s → %(env)s",
                     backup=backup.name or "?", env=self.name)

        def fail(message, result="failed"):
            self._log("backup_restore", name=log_name, result=result,
                      error_message=message)
            self.message_post(body=_("Restore fallido: %s") % message)
            return False

        # 1. Registro válido + objetos realmente en S3 (¿los borró el lifecycle?).
        if not backup or backup.state != "completed":
            return fail(_("El backup no está disponible (estado: %s).")
                        % (backup.state if backup else "?"))
        if not DB_NAME_RE.match(db_name or ""):
            return fail(_("Nombre de base destino inválido: '%s'.") % db_name)
        # Fallback explícito para registros previos al campo s3_bucket
        # (decisión Bloque 4): el bucket de la política del entorno de origen.
        bucket = backup._resolve_bucket()
        if not bucket or not backup.s3_key:
            return fail(_(
                "No se pudo resolver el bucket del backup: el registro no lo "
                "tiene y la política del entorno de origen tampoco lo define "
                "(o falta la key S3)."))
        origin_account = backup.environment_id.account_id
        region = origin_account.default_region
        try:
            s3 = aws_s3.AwsS3Service(origin_account._get_aws_service())
            if not s3.head_object(bucket, backup.s3_key, region=region):
                return fail(_(
                    "El dump ya no está en S3 (¿expiró por lifecycle?): "
                    "s3://%(bucket)s/%(key)s",
                    bucket=bucket, key=backup.s3_key))
            has_filestore = bool(backup.s3_filestore_key) and bool(
                s3.head_object(bucket, backup.s3_filestore_key,
                               region=region))
        except Exception as error:  # noqa: BLE001 - se audita y aborta
            return fail(_("No se pudo verificar el dump en S3: %s") % error)

        # 2. Instancia destino corriendo (no se enciende infra para restaurar).
        if not instance or instance.environment_id != self:
            return fail(_("La instancia destino no pertenece al entorno."))
        if instance.instance_state != "running":
            return fail(_("La instancia destino no está corriendo (estado: "
                          "%s). No se enciende infra para restaurar.")
                        % instance.instance_state)

        # 3. Pre-backup del destino (red de seguridad): si falla, se ABORTA.
        pre_record = None
        target_db = self.env["primate.cloud.database"].search(
            [("environment_id", "=", self.id), ("name", "=", db_name)], limit=1,
        )
        if params.get("pre_backup") and target_db:
            policy = self.backup_policy_id or backup.environment_id.backup_policy_id
            pre_record = self._run_database_backup(
                target_db, policy, bucket, "pre-restore", region,
                purpose="pre_restore",
            )
            if pre_record.state != "completed":
                return fail(_(
                    "El pre-backup del destino falló (%s): se aborta el "
                    "restore. La base destino quedó intacta.")
                    % (pre_record.error_message or "?"))
        elif params.get("pre_backup"):
            self.message_post(body=_(
                "Pre-backup omitido: la base '%s' no existe aún en el destino."
            ) % db_name)

        # 4-5. Script: valida espacio y versión PG ANTES del drop; después
        # stop Odoo → terminar conexiones → drop → restore → filestore → start.
        try:
            output = instance._get_ssm_service().run_script(
                instance.aws_instance_id,
                self._build_backup_restore_script(db_name, backup, has_filestore),
                region=instance.region or region,
                comment="pcm restore: %s" % db_name,
                timeout=3600,
            )
        except Exception as error:  # noqa: BLE001 - SSM inaccesible
            return fail(_("SSM inaccesible: %s") % error)
        stdout = output.get("stdout") or ""
        stderr = output.get("stderr") or ""
        if output.get("status") != "Success" or "PCM_RESTORE_OK" not in stdout:
            error_text = stderr or stdout or output.get("status")
            if "PCM_ERROR_PG_MISMATCH" in stdout + stderr:
                error_text = _(
                    "Versión de PostgreSQL incompatible: el dump viene de una "
                    "versión más nueva que la del destino (actualizá "
                    "PostgreSQL del destino o usá otra instancia). Detalle: %s"
                ) % error_text
            if "PCM_DROP_STARTED" in stdout:
                # Fracaso POST-DROP: el camino de recuperación, a la vista.
                if pre_record:
                    error_text = _(
                        "Falló DESPUÉS del drop de '%(db)s'. El destino quedó "
                        "restaurable con este pre-backup: "
                        "s3://%(bucket)s/%(key)s. Error: %(error)s",
                        db=db_name, bucket=pre_record.s3_bucket,
                        key=pre_record.s3_key, error=error_text)
                else:
                    error_text = _(
                        "Falló DESPUÉS del drop de '%(db)s' y NO hay "
                        "pre-backup (se omitió o la base no existía). "
                        "Error: %(error)s", db=db_name, error=error_text)
            return fail(error_text)

        # 6. Neutralización: por default siempre que el destino NO sea
        # producción; el refresh de staging puede saltearla explícitamente
        # (spec §14.5). Sobre producción JAMÁS se neutraliza, pida lo que pida.
        neutralize = params.get("neutralize")
        if neutralize is None:
            neutralize = True
        if neutralize and self.env_type != "production":
            url = "https://%s" % (self.main_url or self.name)
            try:
                self._staging_neutralize(
                    instance, {"db_name": db_name},
                    instance.region or region, url,
                )
            except Exception as error:  # noqa: BLE001 - alerta fuerte, sin silencio
                return fail(_(
                    "Restore OK pero la NEUTRALIZACIÓN falló: %s. NO usar el "
                    "entorno hasta neutralizar a mano.") % error,
                    result="partial")

        # 7. Registro de la base destino (si no existía) + bitácora.
        if not target_db:
            self.env["primate.cloud.database"].create({
                "name": db_name, "account_id": self.account_id.id,
                "environment_id": self.id, "db_type": "local_pg",
                "ec2_instance_id": instance.id,
            })
        extra = "" if has_filestore else _(" (sin filestore: el backup no "
                                           "tenía o el objeto ya no está)")
        self._log("backup_restore", name=log_name, result="success")
        self.message_post(body=_(
            "Backup %(backup)s restaurado en '%(db)s' (instancia "
            "%(instance)s)%(extra)s.", backup=backup.name, db=db_name,
            instance=instance.name, extra=extra))
        return True

    @api.model
    def _build_backup_restore_script(self, db_name, backup, has_filestore):
        """Script SSM del restore (orden aprobado en el diseño del Bloque 4).

        Valida TODO antes de dropear (espacio en disco, versión PostgreSQL);
        recién después: **stop Odoo → pg_terminate_backend → dropdb** →
        createdb → pg_restore → filestore → start Odoo. ``PCM_DROP_STARTED``
        marca el punto de no retorno: si el script falla después, el job arma
        el mensaje de recuperación con la key del pre-backup.
        """
        db = shlex.quote(db_name)
        bkt = shlex.quote(backup._resolve_bucket())
        dump_key = shlex.quote(backup.s3_key)
        # El nombre del directorio dentro del tar es el de la BD de ORIGEN.
        origin_dir = shlex.quote(backup.database_id.name or db_name)
        need_mb = int(max(backup.size_mb or 0, 1) * 1.5)
        lines = [
            "set -euo pipefail",
            "command -v aws >/dev/null 2>&1 || { echo \"PCM_ERROR: aws CLI no está instalado en la instancia (el install de PCM lo trae vía snap; instalalo o re-aprovisioná)\" >&2; exit 1; }",
            'FNAME="/var/tmp/pcm_restore_$$.dump"',
            'FS_TAR="/var/tmp/pcm_restore_$$.tar.gz"',
            'FS_TMP="/var/tmp/pcm_restore_fs_$$"',
            "trap 'rm -rf \"$FNAME\" \"$FS_TAR\" \"$FS_TMP\" 2>/dev/null "
            "|| true' EXIT",
            # Guarda de espacio: acá el dump SÍ baja a disco (pg_restore -l
            # necesita el archivo y el destino no es la producción de origen).
            "NEED_MB=%d" % need_mb,
            "FREE_MB=$(df -Pm /var/tmp | awk 'NR==2 {print $4}')",
            'if [ "$FREE_MB" -lt "$NEED_MB" ]; then echo "PCM_ERROR: espacio '
            'insuficiente en /var/tmp (${FREE_MB} MB < ${NEED_MB} MB)" >&2; '
            "exit 1; fi",
            'aws s3 cp s3://%s/%s "$FNAME" --only-show-errors' % (bkt, dump_key),
            # Compatibilidad PostgreSQL ANTES de tocar nada del destino.
            'SRC_VER=$(sudo -u postgres pg_restore -l "$FNAME" | '
            "sed -n 's/.*dumped from database version "
            "\\([0-9]*\\).*/\\1/p' | head -1)",
            'DST_VER=$(sudo -u postgres psql -tAc "show server_version" '
            "| cut -d. -f1)",
            'if [ -n "$SRC_VER" ] && [ "$SRC_VER" -gt "$DST_VER" ]; then '
            'echo "PCM_ERROR_PG_MISMATCH origen=$SRC_VER destino=$DST_VER" '
            ">&2; exit 1; fi",
            # Orden aprobado: 1) detener Odoo, 2) terminar conexiones
            # residuales, 3) recién ahí el drop.
            "systemctl stop odoo",
            # Desde acá, CUALQUIER salida (éxito o fallo) re-arranca Odoo: la
            # EC2 puede hospedar más bases y un restore fallido no puede dejar
            # el servicio abajo para todas. El start vive en el trap EXIT.
            "trap 'systemctl start odoo || true; rm -rf \"$FNAME\" \"$FS_TAR\" "
            "\"$FS_TMP\" 2>/dev/null || true' EXIT",
            'sudo -u postgres psql -tAc "SELECT pg_terminate_backend(pid) '
            "FROM pg_stat_activity WHERE datname = '%s' AND pid <> "
            'pg_backend_pid();" || true' % db_name,
            'echo "PCM_DROP_STARTED"',
            "sudo -u postgres dropdb --if-exists %s" % db,
            "sudo -u postgres createdb -O odoo %s" % db,
            'sudo -u postgres pg_restore -d %s "$FNAME" || true' % db,
            # pg_restore devuelve != 0 por avisos ignorables (owners, etc.):
            # la sanidad real es que la base restaurada sea un Odoo.
            'sudo -u postgres psql -d %s -tAc "SELECT count(*) FROM '
            'ir_module_module" >/dev/null' % db,
        ]
        if has_filestore:
            filestore_key = shlex.quote(backup.s3_filestore_key)
            target_dir = "%s/%s" % (BACKUP_FILESTORE_BASE, db)
            lines += [
                'aws s3 cp s3://%s/%s "$FS_TAR" --only-show-errors'
                % (bkt, filestore_key),
                'mkdir -p "$FS_TMP"',
                'tar -xzf "$FS_TAR" -C "$FS_TMP"',
                "rm -rf %s" % target_dir,
                'mv "$FS_TMP"/%s %s' % (origin_dir, target_dir),
                "chown -R odoo:odoo %s" % target_dir,
            ]
        lines += [
            # El start NO va acá: lo hace el trap EXIT re-armado tras el stop,
            # para cubrir también todos los caminos de error.
            'echo "PCM_RESTORE_OK"',
        ]
        # Bajo bash (heredoc): usa pipefail/PIPESTATUS y arrays si hiciera falta;
        # SSM corre con dash, que no soporta pipefail.
        return self._wrap_bash("\n".join(lines))

    # ------------------------------------------------------------------
    # Constructores de scripts SSM (puros, testeables por contenido)
    # ------------------------------------------------------------------
    # NOTA (Bloque 5): los constructores propios de dump/restore de la Fase 7
    # (_build_dump_script / _build_restore_script) se eliminaron — la copia de
    # staging reusa el pipeline de backups (Bloques 3-4): un solo camino,
    # streaming en el origen y filestore incluido.

    @staticmethod
    def _build_neutralize_script(staging_db, sql):
        """psql -f con el SQL de neutralización (heredoc, sin expansión de shell)."""
        return "sudo -u postgres psql -d %s <<'PCMSQL'\n%s\nPCMSQL" % (
            shlex.quote(staging_db), sql,
        )

    @staticmethod
    def _build_clone_script(url, path, ref):
        """git clone del repo al destino y checkout al commit/rama indicado."""
        quoted_path = shlex.quote(path)
        lines = [
            "set -e",
            "sudo rm -rf %s" % quoted_path,
            "sudo -u odoo git clone %s %s" % (shlex.quote(url), quoted_path),
        ]
        if ref:
            lines.append("sudo -u odoo git -C %s checkout %s"
                         % (quoted_path, shlex.quote(ref)))
        return "\n".join(lines)

    @staticmethod
    def _build_restart_script():
        """Reinicio de servicios del staging."""
        return "set -e\nsudo systemctl restart odoo\nsudo systemctl restart nginx || true"

    # ------------------------------------------------------------------
    # Agregar addon a la instancia (Bloque B4)
    # ------------------------------------------------------------------
    def add_addon(self, vals):
        """Registra un repo/addon Y lo clona en la instancia (o ninguna cosa).

        Crea el registro de trazabilidad en estado 'Sin verificar' y encola el
        trabajo que clona + activa el addons_path + verifica. Si el clone falla,
        el registro se borra (no queda un 'instalado' mentiroso).
        """
        self.ensure_one()
        if not self.ec2_instance_ids[:1]:
            raise UserError(_("El entorno no tiene una instancia para clonar el addon."))
        url = (vals.get("github_url") or "").strip()
        if not url:
            raise UserError(_("Falta la URL del repositorio."))
        slug = github_api.GithubApiService.parse_repo_slug(
            url, vals.get("organization"))
        repo_name = (slug.split("/")[-1] if slug
                     else url.rstrip("/").split("/")[-1])
        repo_name = repo_name[:-4] if repo_name.endswith(".git") else repo_name
        repo = self.env["primate.cloud.repository"].create({
            "name": vals.get("name") or repo_name,
            "environment_id": self.id,
            "github_url": url,
            "organization": vals.get("organization") or False,
            "repo_type": vals.get("repo_type") or "custom_client",
            "configured_branch": vals.get("configured_branch") or False,
            "local_path": "%s/%s" % (CUSTOM_ADDONS_DIR, repo_name),
            "sync_state": "unknown",   # 'Sin verificar' hasta clonar + detectar
        })
        if vals.get("github_token"):
            repo.github_token = vals["github_token"]   # se cifra en el inverse
        self.with_delay(
            description=_("Agregar addon: %s") % repo.name
        ).job_add_addon(repo.id)
        return repo

    def job_add_addon(self, repo_id):
        """Job: clona el addon, activa el addons_path (reusa B3) y verifica."""
        repo = self.env["primate.cloud.repository"].browse(repo_id).exists()
        if not repo:
            return
        title = _("Agregar addon: %s") % repo.name
        instance = self.ec2_instance_ids[:1]
        if not instance:
            repo._log("addon_add", result="failed", name=title,
                      error_message=_("El entorno no tiene instancia."))
            repo.unlink()
            return
        # URL de clone: el token va por askpass (NUNCA en la URL persistida).
        token = repo._get_github_token()
        slug = repo._repo_slug()
        clone_url = "https://%sgithub.com/%s.git" % (
            "x-access-token@" if token else "", slug)
        cloned = instance.clone_addon(
            clone_url, repo.local_path, ref=repo.configured_branch or None,
            token=token)
        if not cloned:
            repo._log("addon_add", result="failed", name=title,
                      error_message=_("El clone del repositorio falló."))
            repo.unlink()   # o-ninguna: ni registro ni clon
            return
        # Activar el addons_path (retroactivo, reusa B3) o reiniciar para cargar.
        ensure = instance._ensure_custom_addons_path()
        if ensure in ("rolled_back", "error"):
            repo._log("addon_add", result="partial", name=title,
                      error_message=_(
                          "Clonado, pero el addons_path no quedó activo; el "
                          "addon NO está cargado (registrado como no verificado)."))
            repo.message_post(body=_("Addon clonado pero no cargado (addons_path)."))
            return   # registrado, no verificado — NO éxito mentiroso
        if ensure == "ready":
            instance.restart_odoo()   # ya estaba en el path → reiniciar y cargar
        # 'restarted' → _ensure ya reinició cargando el addon.
        # Verificar: ¿hay módulos (con __manifest__) en el clone?
        count = instance.addon_module_count(repo.local_path)
        if count > 0:
            repo.sync_state = "updated"   # verificado (presente y cargable)
            repo._log("addon_add", result="success", name=title,
                      error_message=_("Addon agregado y verificado (%d módulo(s)).")
                      % count)
        else:
            repo.sync_state = "unknown"   # registrado, no verificado
            repo._log("addon_add", result="partial", name=title,
                      error_message=_(
                          "Clonado pero sin __manifest__ detectable; registrado "
                          "como NO verificado."))
