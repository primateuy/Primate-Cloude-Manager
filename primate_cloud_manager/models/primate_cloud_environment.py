# -*- coding: utf-8 -*-
"""Entorno: instalación específica de Odoo (objeto central del módulo).

En Fase 2 es un esqueleto que agrupa la infraestructura sincronizada (EC2,
bases, DNS). En Fase 4 gana el flujo de aprovisionamiento completo
(EC2 → DB → install_odoo.sh por SSM → nginx → SSL → DNS), todo async vía
queue_job. La trazabilidad (Fase 5) y el staging (Fase 7) se agregan luego.
"""
import base64
import json
import logging
import re
import secrets
import shlex
import time
import uuid
import zlib
from datetime import datetime, timedelta

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import file_open

from ..services import aws_base, aws_ec2, aws_rds, aws_route53, aws_s3, aws_ssm, github_api
from ..tools import bus, crypto, dates
from .primate_cloud_ec2_instance import CUSTOM_ADDONS_DIR
from .primate_cloud_instance import LEGACY_SERVICE
from .primate_cloud_destruction import durable_log

_logger = logging.getLogger(__name__)

# Rutas (relativas al addons-path) de las plantillas corridas por SSM.
INSTALL_SCRIPT_PATH = "primate_cloud_manager/data/install_odoo.sh"
NEUTRALIZATION_SQL_PATH = "primate_cloud_manager/data/neutralization.sql"
# Multi-Odoo (R3): bootstrap del servidor (una vez) + install por instancia.
BOOTSTRAP_SCRIPT_PATH = "primate_cloud_manager/data/bootstrap_server.sh"
INSTANCE_INSTALL_SCRIPT_PATH = "primate_cloud_manager/data/install_instance.sh"

# Slug seguro para dirs/units/sites (validado ANTES de renderizar el script:
# un slug raro no puede convertirse en una ruta o comando inesperado).
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Charset de la contraseña PG transitoria (token_urlsafe): sin comillas ni
# escapes → segura para el CREATE USER inline del script. Se valida igual.
PG_PASSWORD_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Swap del bootstrap (D-R3.7).
BOOTSTRAP_SWAP_MB = 2048
# Puertos por slots de a 10 (D-R3.8): slot k → (8069+10k, 8072+10k). Los
# slots asignados NO se reciclan en v1 (una instancia archivada retiene el
# suyo: evita colisiones con units residuales en el servidor).
PORT_SLOT_BASE_HTTP = 8069
PORT_SLOT_BASE_GEVENT = 8072
PORT_SLOT_STRIDE = 10
# Timeout del health-probe HTTP a una instancia viva (segundos): corto, es
# lectura pura dentro de un job.
HEALTH_PROBE_TIMEOUT = 10

# --- HTTPS (D-R3.9 endurecida, 2026-10-22) ---
SSL_LE_EMAIL = "ops@primate.uy"
SSM_LONG_TIMEOUT_PARAM = "pcm.ssm_long_timeout_seconds"
SSM_LONG_TIMEOUT_DEFAULT = 4 * 3600   # restore/backup de bases grandes
SSL_RETRY_MAX = 6              # ejecuciones REALES de certbot (rate limits LE)
SSL_INSYNC_POLLS = 24          # GetChange hasta INSYNC: 24 × 5 s ≈ 2 min
SSL_INSYNC_POLL_SECONDS = 5

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

# F1 (restore desde dump externo): el detalle completo del restore va a un
# archivo en el servidor —ruta predecible, para poder ir a buscarlo— y a la
# bitácora viaja sólo un tail acotado: ``get_command_invocation`` trunca la
# salida a 24.000 caracteres, así que un restore verboso perdería justo el
# final, que es lo que se necesita leer cuando algo falla.
# F1: retención CORTA propia para los pre-backups del restore, que van a S3 y
# hasta ahora quedaban sin regla de expiración (se acumulaban). Es corta a
# propósito: no es la retención de la política, que puede ser de meses.
# Configurable por el parámetro ``pcm.pre_restore_retention_days``.
PRE_RESTORE_RETENTION_DAYS = 7

RESTORE_LOG_DIR = "/var/tmp/pcm/logs"
RESTORE_TAIL_BYTES = 4000


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
    # Marca de origen (regla dura 2026-10-22, punto 2a): todo lo que cuelga de
    # un entorno importado —instancias, bases, DNS, repos— hereda el solo
    # lectura. Vale también SIN máquina AWS (Cloudpepper: gemelo desde
    # archivo). Inmutable: solo el sync del import la escribe.
    pcm_origin = fields.Selection(
        [("pcm", "Montado por PCM"),
         ("imported_aws", "Importado de AWS (solo lectura)"),
         ("imported_cloudpepper", "Importado de Cloudpepper (solo lectura)")],
        string="Origen", default="pcm", required=True, readonly=True,
        copy=False, index=True, tracking=True)
    # Mismos defaults que la instancia: el formulario del entorno los muestra
    # ya completos (19 / Community) en vez de vacíos.
    odoo_version = fields.Selection(
        related="primary_instance_id.odoo_version", string="Versión Odoo",
        store=True, readonly=False, default="19",
    )
    odoo_edition = fields.Selection(
        related="primary_instance_id.odoo_edition", string="Edición Odoo",
        store=True, readonly=False, default="community",
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
    # Caché de UI del bootstrap multi-Odoo (R3). La VERDAD es el marker
    # /opt/pcm/.bootstrap-v1 en disco: toda operación que dependa del
    # bootstrap lo re-verifica por SSM (regla permanente: ninguna decisión
    # de seguridad/operación se toma de un caché).
    multiodoo_ready = fields.Boolean(
        string="Multi-Odoo listo", default=False, copy=False, readonly=True,
        help="El servidor pasó el bootstrap multi-Odoo (informativo; se "
             "re-verifica contra el marker en disco en cada operación).",
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

    # Método de reparto del costo crudo del servidor entre sus instancias (R5).
    # "default" = usar el global (system param pcm.cost.split_method). "usage"
    # está DEFINIDO pero DESHABILITADO (requiere medición por instancia, v2):
    # la constraint impide guardarlo.
    cost_split_method = fields.Selection(
        [("default", "Usar el default global"),
         ("equal", "Partes iguales (prorrateado por días)"),
         ("weight", "Por peso (prorrateado por días)"),
         ("usage", "Por uso (requiere medición — deshabilitado)")],
        string="Método de reparto de costo", default="default", required=True,
        help="Cómo se reparte el costo crudo de ESTE servidor entre sus "
             "instancias. 'usage' está deshabilitado hasta que haya medición "
             "por instancia (v2).",
    )

    @api.constrains("cost_split_method")
    def _check_cost_split_method(self):
        for rec in self:
            if rec.cost_split_method == "usage":
                raise ValidationError(_(
                    "El método de reparto 'por uso' aún no está disponible: "
                    "requiere medición de recursos por instancia (v2). Usá "
                    "'partes iguales' o 'por peso'."))

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

        Materializa el ref del partner (lazy) AL taggear. **No emite tag de
        cliente** (``client_ref = False``) cuando el partner es el CENTINELA
        "⚠ SIN CLIENTE" (proyecto sin cliente real, R5): taggear un cliente
        falso sería peor que no taggear. Desde R5 ``partner_id`` es required, así
        que el caso "sin partner" ya no ocurre por proyecto; el equivalente es el
        centinela. Igual se conserva la degradación defensiva: un ref ausente
        NUNCA rompe el aprovisionamiento (un recurso sin tag de cliente es un
        problema de reporte tolerable; un provision que aborta, NO).
        """
        self.ensure_one()
        partner = self.project_id.partner_id
        # El centinela no es cliente real: no se emite su tag (mismo criterio
        # que el viejo "sin partner"). Id en el param rename-proof de la migración.
        sentinel_id = int(self.env["ir.config_parameter"].sudo().get_param(
            "pcm.unassigned_partner_id") or 0)
        client_ref = False
        if partner and partner.id != sentinel_id:
            try:
                client_ref = partner._ensure_pcm_ref()
            except Exception:  # noqa: BLE001 - un ref ausente no aborta el provision
                _logger.warning(
                    "No se pudo materializar el pcm_ref del partner %s; el "
                    "recurso queda sin tag de cliente.", partner.id)
                client_ref = False
        return self.pcm_ref, client_ref

    # --- Reparto de costos por instancia (R5-B2) ------------------------
    def _effective_split_method(self):
        """Método de reparto efectivo: el del servidor, o el default global."""
        self.ensure_one()
        if self.cost_split_method and self.cost_split_method != "default":
            return self.cost_split_method
        return self.env["ir.config_parameter"].sudo().get_param(
            "pcm.cost.split_method", "equal")

    def _compute_cost_shares_vals(self, total_amount, period_start, period_end,
                                  granularity, now):
        """Vals de ``cost.share`` para repartir ``total_amount`` (crudo de ESTE
        servidor) entre sus instancias, PRORRATEADO por días de hospedaje.

        Peso efectivo por instancia = base × días_en_el_período, con
        base = 1 (``equal``) o ``cost_weight`` (``weight``). Sin instancias con
        días en el período (Q2: servidor recién creado / todo archivado antes) →
        UNA share "sin atribuir" con el total (invariante intacto, costo visible,
        nadie facturado). Orden por ``pcm_ref`` para que el residuo del splitter
        sea reproducible.
        """
        self.ensure_one()
        Share = self.env["primate.cloud.cost.share"]
        method = self._effective_split_method()
        # active_test=False: una instancia archivada pudo haber estado viva parte
        # del período y le corresponde su prorrateo.
        instances = self.with_context(
            active_test=False).instance_ids.sorted("pcm_ref")
        parts = []
        for inst in instances:
            days = inst._hosted_days_in_period(period_start, period_end)
            base = inst.cost_weight if method == "weight" else 1.0
            weight = base * days
            if weight > 0:
                parts.append((inst, weight))
        if not parts:
            return [self._unattributed_share_vals(
                total_amount, period_start, period_end, granularity, method, now)]
        amounts = Share._split_cents(total_amount, [w for _inst, w in parts])
        vals = []
        for (inst, _weight), amount in zip(parts, amounts):
            partner = inst.project_id.partner_id
            vals.append({
                "account_id": self.account_id.id,
                "period_start": period_start, "period_end": period_end,
                "granularity": granularity,
                "environment_id": self.id, "environment_ref": self.pcm_ref,
                "environment_name": self.name,
                "instance_id": inst.id, "instance_ref": inst.pcm_ref,
                "instance_name": inst.name,
                "project_id": inst.project_id.id or False,
                "partner_id": partner.id or False,
                "client_ref": partner.pcm_ref or False,
                "client_name": partner.name or False,
                "method": method, "amount": amount, "computed_at": now,
            })
        return vals

    def _unattributed_share_vals(self, total_amount, period_start, period_end,
                                 granularity, method, now):
        """Share del crudo de un servidor SIN instancias atribuibles (Q2)."""
        self.ensure_one()
        return {
            "account_id": self.account_id.id,
            "period_start": period_start, "period_end": period_end,
            "granularity": granularity,
            "environment_id": self.id, "environment_ref": self.pcm_ref,
            "environment_name": self.name,
            "instance_id": False, "instance_ref": False, "instance_name": False,
            "unattributed": True, "method": method,
            "amount": total_amount, "computed_at": now,
        }

    # --- Re-tag dedicado / compartido (R5-B3) --------------------------
    def _dedicated_client_partner(self):
        """Partner al que el servidor está DEDICADO, o recordset vacío si es
        COMPARTIDO / sin cliente real.

        Dedicado = TODAS las instancias vivas (no archivadas) son del MISMO
        cliente REAL — una sola EC2 no puede tener dos dueños. El centinela
        "⚠ SIN CLIENTE" NO cuenta como cliente: un servidor de puras instancias
        sin asignar, o de un cliente real + una sin asignar, NO es dedicado
        (taggear su client_id sobre-atribuiría todo el servidor en Cost Explorer).
        """
        self.ensure_one()
        live = self.with_context(active_test=False).instance_ids.filtered(
            lambda i: i.state != "archived")
        partners = live.mapped("project_id.partner_id")
        sentinel_id = int(self.env["ir.config_parameter"].sudo().get_param(
            "pcm.unassigned_partner_id") or 0)
        distinct = set(partners.ids)
        if len(distinct) == 1 and sentinel_id not in distinct:
            return partners[:1]
        return self.env["res.partner"]

    def _enqueue_client_tag_sync(self):
        """Encola el re-tag de ``primate:client_id``. Va por ``queue_job`` porque
        MUTA AWS; y encolar (una escritura en BD) NO puede hacer fallar la
        operación que agregó/archivó la instancia. Dedup por servidor."""
        self.ensure_one()
        if not self.ec2_instance_id or not self.ec2_instance_id.aws_instance_id:
            return   # sin máquina gestionada: nada que re-taggear
        self.with_delay(
            identity_key="pcm_retag_client_%s" % self.id,
            description=_("Re-tag client_id: %s") % self.name
        )._job_sync_client_tag()

    def _job_sync_client_tag(self):
        """Job: pone/quita ``primate:client_id`` en la EC2 según el servidor sea
        dedicado a un cliente real o compartido. NUNCA rompe la operación que lo
        disparó (regla Fase 9: el tagging no hace fallar lo que etiqueta) — captura,
        loguea y sigue."""
        self.ensure_one()
        machine = self.ec2_instance_id
        if (not machine or not machine.aws_instance_id
                or machine.instance_state == "terminated"):
            return   # sin máquina / terminada: nada que taggear
        # Punto 8 del contrato del modo adoptado: re-taggear ESCRIBE en AWS
        # —create_tags o delete_tags sobre la EC2 del cliente—, y lo dispara
        # algo tan inocente como registrar una instancia. En un servidor
        # adoptado en solo lectura no se toca AWS. No levanta: la regla de
        # Fase 9 es que el tagging nunca rompe la operación que lo disparó.
        if machine.management_state == "adopted_readonly":
            _logger.info("Re-tag de client_id omitido: %s está adoptado en "
                         "solo lectura.", machine.name)
            return
        try:
            ec2 = aws_ec2.AwsEc2Service(self.account_id._get_aws_service())
            region = machine.region or self.account_id.default_region
            partner = self._dedicated_client_partner()
            if partner:
                ec2.create_tags(
                    [machine.aws_instance_id],
                    [{"Key": aws_base.CLIENT_ID_TAG,
                      "Value": partner._ensure_pcm_ref()},
                     {"Key": aws_base.CLIENT_TAG, "Value": partner.name or ""}],
                    region=region)
            else:
                # Compartido / sin cliente real → se RETIRA el client_id (si no,
                # CE seguiría agrupando todo el servidor bajo el dueño anterior).
                ec2.delete_tags(
                    [machine.aws_instance_id],
                    [aws_base.CLIENT_ID_TAG, aws_base.CLIENT_TAG], region=region)
        except Exception as error:  # noqa: BLE001 - el tagging no rompe nada
            _logger.warning(
                "Re-tag de client_id del servidor %s falló (se ignora, regla "
                "Fase 9): %s", self.name, error)
            self.env["primate.cloud.operation.log"].log_operation(
                "retag", name=_("Re-tag client_id: %s") % self.name,
                record=self, result="failed", error_message=str(error))

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
            delegated = {
                key: vals.pop(key)
                for key in list(vals) if key in self.INSTANCE_DELEGATED_FIELDS
            }
            # Un False explícito no pisa el default de la instancia: versión y
            # edición son obligatorias y tienen default (19 / Community).
            for key in ("odoo_version", "odoo_edition"):
                if key in delegated and not delegated[key]:
                    delegated.pop(key)
            delegated_list.append(delegated)
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
        from .primate_cloud_region_setup import pcm_ssm_profile
        filled = dict(params)
        if not profile:
            filled["instance_profile"] = pcm_ssm_profile(self.env)
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
        # Cadena COMPLETA antes de tocar AWS (la instalación que viene después
        # de la EC2 no puede fallar por configuración que se sabía de antemano).
        self._exigir_cadena_de_instalacion(params)
        self.state = "provisioning"
        # Token de idempotencia: viaja en los args del job (queue_job los persiste),
        # así un requeue tras reiniciar el server NO crea una EC2 duplicada.
        params = dict(params, client_token="pcm-%s" % uuid.uuid4().hex)
        params = self._ensure_transient_db_password(params)
        # Los secretos NO viajan en el job: va una referencia (2026-10-27).
        params = self.env["primate.cloud.job.secret"].stash(params)
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
    # Concurrencia por servidor (R3-B2): lock + asignación de puertos
    # ------------------------------------------------------------------
    def _server_lock_key(self, scope):
        """Clave estable del advisory lock por (servidor, alcance) → bigint.

        Determinista entre procesos (crc32, no ``hash()`` que varía por seed
        — mismo patrón que el SG del auto-discovery). Alcances distintos
        (``instance-install`` vs ``port-alloc``) usan claves distintas: la
        asignación de puertos no debe esperar los minutos de un install.
        """
        self.ensure_one()
        crc = zlib.crc32(scope.encode("utf-8")) & 0xFFFFFFFF
        return (self.id << 32) | crc

    def _acquire_server_lock(self, scope):
        """Toma el lock de SESIÓN del servidor (unlock explícito del caller).

        Serializa dos «agregar instancia» sobre la MISMA máquina; servidores
        distintos no se bloquean entre sí. Devuelve la clave para el release.
        """
        key = self._server_lock_key(scope)
        self.env.cr.execute("SELECT pg_advisory_lock(%s)", (key,))
        return key

    def _release_server_lock(self, key):
        """Libera el lock de sesión, tolerando una transacción abortada.

        Si un error SQL dejó la transacción en estado aborted, el UNLOCK
        directo fallaría y el lock quedaría colgado en la conexión del pool
        (serializaría PARA SIEMPRE los installs de este servidor): se hace
        rollback (la transacción ya estaba perdida) y se libera igual.
        """
        try:
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (key,))
        except Exception:  # noqa: BLE001 - transacción abortada
            self.env.cr.rollback()
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (key,))

    def _allocate_ports(self, lock=True):
        """Primer slot de puertos libre del servidor (atómico, D-R3.8).

        Slots de a 10 desde (8069, 8072). Cuenta TODAS las instancias del
        servidor **incluidas las archivadas** (los slots no se reciclan en
        v1: una unit residual en el servidor no puede chocar con un slot
        re-asignado). Redes de seguridad: la constraint
        ``UNIQUE(environment_id, http_port)`` de R1 (atrás) y el ``ss -ltn``
        del script (adelante, puertos ocupados por fuera de PCM).

        **Atomicidad**: toma ``pg_advisory_xact_lock`` (se libera solo al
        commit) — dos asignaciones concurrentes se serializan y la segunda
        ve lo commiteado por la primera. Llamar SOLO desde transacciones
        cortas (crear instancia + encolar), nunca dentro del trabajo largo
        de un job. ``lock=False`` es SOLO para previews informativos (el
        wizard muestra el slot probable sin retener el lock); toda
        asignación REAL va con lock.

        Returns:
            tuple[int, int]: ``(http_port, gevent_port)`` del slot asignado.
        """
        self.ensure_one()
        if lock:
            self.env.cr.execute("SELECT pg_advisory_xact_lock(%s)",
                                (self._server_lock_key("port-alloc"),))
        instances = self.env["primate.cloud.instance"].with_context(
            active_test=False).search([("environment_id", "=", self.id)])
        used = set()
        for inst in instances:
            used.update(p for p in (inst.http_port, inst.gevent_port) if p)
        slot = 0
        while True:
            http = PORT_SLOT_BASE_HTTP + PORT_SLOT_STRIDE * slot
            gevent = PORT_SLOT_BASE_GEVENT + PORT_SLOT_STRIDE * slot
            if http not in used and gevent not in used:
                return http, gevent
            slot += 1

    def _create_instance_with_ports(self, vals):
        """Crea una instancia del servidor con su slot de puertos asignado.

        Único camino válido para crear instancias que se van a instalar en
        el layout multi-Odoo (el wizard de B3 pasa por acá): la asignación
        y el INSERT quedan bajo el mismo ``xact_lock`` de
        :meth:`_allocate_ports`, así dos creaciones simultáneas no pueden
        agarrar el mismo slot.
        """
        self.ensure_one()
        http_port, gevent_port = self._allocate_ports()
        return self.env["primate.cloud.instance"].create(dict(
            vals, environment_id=self.id,
            http_port=http_port, gevent_port=gevent_port,
        ))

    def _enqueue_add_instance(self, instance, params):
        """Valida y encola el montaje de OTRO Odoo en este servidor (R3-B2).

        Como en la cadena R2, lo transitorio se genera al ENCOLAR (queue_job
        persiste los args): la contraseña PG propia de la instancia viaja en
        los params y un requeue reusa la MISMA.
        """
        self.ensure_one()
        instance.ensure_one()
        if instance.environment_id != self:
            raise UserError(_("La instancia no pertenece a este servidor."))
        if self.state != "active":
            raise UserError(_(
                "Solo se agregan instancias a un servidor activo."))
        if params.get("db_mode", "local_pg") != "local_pg":
            raise UserError(_(
                "Agregar instancia soporta PostgreSQL local del servidor en "
                "v1 (una RDS propia por instancia queda anotada en backlog)."))
        params = dict(params, db_mode="local_pg")
        self._exigir_cadena_de_instalacion(params, instance=instance)
        params = self._ensure_transient_db_password(params)
        # Los secretos NO viajan en el job: va una referencia (2026-10-27).
        params = self.env["primate.cloud.job.secret"].stash(params)
        instance.state = "draft"
        self.with_delay(
            description=_("Agregar instancia: %s") % instance.display_name
        ).job_add_instance(instance.id, params)
        return True

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
    def _render_script_template(self, path, tokens):
        """Lee una plantilla y reemplaza los tokens %%...%% (renderer común).

        Args:
            path (str): ruta relativa al addons-path de la plantilla.
            tokens (dict): ``{"ODOO_VERSION": "19", ...}``.

        Returns:
            str: el script listo para correr por SSM.
        """
        with file_open(path, "r") as script_file:
            script = script_file.read()
        for key, value in tokens.items():
            # None → "" (token ausente); pero 0/False NO se colapsan a "" —
            # un ``workers = 0`` (default multi-Odoo) debe quedar "0" en el
            # conf, no vacío (int('') revienta el arranque de Odoo).
            rendered = "" if value is None else str(value)
            script = script.replace("%%%%%s%%%%" % key, rendered)
        return script

    @api.model
    def _render_install_script(self, tokens):
        """Plantilla LEGACY single-Odoo (servidores pre-R3)."""
        return self._render_script_template(INSTALL_SCRIPT_PATH, tokens)

    # ------------------------------------------------------------------
    # Builders multi-Odoo (R3-B1): renderizan los scripts REALES; los jobs
    # que los corren por SSM llegan en R3-B2.
    # ------------------------------------------------------------------
    @api.model
    def _build_bootstrap_script(self, swap_mb=BOOTSTRAP_SWAP_MB):
        """Renderiza bootstrap_server.sh (una vez por servidor, idempotente)."""
        return self._render_script_template(
            BOOTSTRAP_SCRIPT_PATH, {"SWAP_MB": int(swap_mb)})

    def _build_instance_install_script(self, instance, params, is_default):
        """Renderiza install_instance.sh para UNA instancia (por slug).

        Valida ANTES de renderizar todo lo que viaja inline al shell: slug y
        db_name con charset seguro, puertos enteros, contraseña PG con el
        charset de token_urlsafe (sin comillas → el CREATE USER inline no es
        inyectable). El conf resultante lleva SIEMPRE el candado multi-tenant
        (db_filter = ^db$ + list_db = False) — fijado por golden.

        Args:
            instance (recordset): la primate.cloud.instance a montar.
            params (dict): parámetros del wizard/job (db_*, dominio, admin, workers).
            is_default (bool): primera instancia del servidor (default_server).

        Returns:
            str: el script listo para correr por SSM.
        """
        self.ensure_one()
        instance.ensure_one()
        slug = instance.slug or ""
        if not SLUG_RE.match(slug):
            raise UserError(_("Slug inválido para instalar: %r.") % slug)
        db_name = params.get("db_name") or ""
        if not DB_NAME_RE.match(db_name):
            raise UserError(_("Nombre de base inválido: %r.") % db_name)
        pg_password = params.get("db_password") or ""
        db_local = params.get("db_mode") == "local_pg"
        if db_local and not PG_PASSWORD_RE.match(pg_password):
            raise UserError(_(
                "Contraseña PG con caracteres inseguros para el install: "
                "se espera el charset de token_urlsafe."))
        pg_user = instance.pg_user or ""
        if db_local and not SLUG_RE.match(pg_user.replace("_", "-")):
            raise UserError(_("Usuario PG inválido: %r.") % pg_user)
        if not instance.odoo_version:
            raise UserError(_("La instancia no tiene versión de Odoo."))
        return self._render_script_template(INSTANCE_INSTALL_SCRIPT_PATH, {
            "SLUG": slug,
            "ODOO_VERSION": instance.odoo_version,
            "ODOO_EDITION": instance.odoo_edition or "community",
            "HTTP_PORT": int(instance.http_port),
            "GEVENT_PORT": int(instance.gevent_port),
            "WORKERS": int(params.get("workers") or 0),
            "DB_HOST": params.get("db_host") or "localhost",
            "DB_PORT": int(params.get("db_port") or 5432),
            "DB_NAME": db_name,
            "PG_USER": pg_user,
            "PG_PASSWORD": pg_password,
            "DB_LOCAL": "1" if db_local else "0",
            "DOMAIN": params.get("domain") or instance.main_url or "",
            "ADMIN_PASSWORD": params.get("admin_password") or "",
            "IS_DEFAULT": "1" if is_default else "0",
        })

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

            # Defensa: la misma validación del encolado, por si el job llegó por
            # otro camino (o con params viejos). Falla ACÁ, antes de la EC2.
            self._exigir_cadena_de_instalacion(params)

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
        params = self.env["primate.cloud.job.secret"].reveal(params)
        self._pcm_assert_env_not_imported("instance_install")
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

            # 0. IP fija (Elastic IP) ANTES de todo lo que usa la IP: sslip.io,
            #    el registro A y el certificado dependen de ella. Acá y no en
            #    el job del servidor para que «Reintentar instalación» también
            #    la reintente (es idempotente).
            if params.get("elastic_ip", True):
                bus.provision_step(self.env, self,
                                   _("Asignando la IP fija (Elastic IP)…"))
                machine._pcm_ensure_elastic_ip()

            # 1. Base de datos (RDS / PostgreSQL local / ninguna) --------------
            if resume:
                db_host = "localhost" if params.get("db_mode") == "local_pg" \
                    else (params.get("db_host") or "localhost")
            else:
                bus.provision_step(self.env, self,
                                   _("Configurando la base de datos…"))
                db_host = self._provision_database(
                    base, account, machine, params, region)

            # 2. Registro DNS (A -> IP pública) ANTES del install (D-R3.9
            #    endurecida): el paso SSL necesita el dominio resolviendo, y
            #    crearlo acá deja la propagación corriendo MIENTRAS el
            #    install trabaja. El ChangeId alimenta la espera de INSYNC.
            dns_change_id = None
            if params.get("create_dns"):
                bus.provision_step(self.env, self, _("Configurando DNS…"))
                dns_change_id = self._provision_dns(
                    base, account, machine, params, domain)

            # 3. Layout multi-Odoo (R3, D-R3.1): los servidores NUEVOS nacen
            #    multi-Odoo desde su primera instancia — bootstrap del
            #    servidor + install POR SLUG (install_odoo.sh queda solo
            #    como camino legacy, hoy lo usa staging hasta R4).
            if not instance_rec:
                raise UserError(_(
                    "El entorno no tiene instancia primaria: no hay qué "
                    "instalar (¿se archivó?)."))
            ssm = aws_ssm.AwsSsmService(base)
            bus.provision_step(self.env, self,
                               _("Preparando el servidor (bootstrap multi-Odoo)…"))
            self._ensure_server_bootstrap(ssm, machine, region)
            bus.provision_step(self.env, self,
                               _("Instalando Odoo por SSM (puede tardar varios minutos)…"))
            self._materialize_multiodoo_layout(instance_rec, params)
            self._provision_run_instance_install(
                ssm, machine, instance_rec, dict(params, db_host=db_host),
                region, domain, is_default=True)
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

        # 5. HTTPS verificado (decisión 1 del OK: warning + auto-retry, JAMÁS
        #    bloquea — la instancia ya quedó activa sirviendo HTTP).
        ssl_result = None
        if instance_rec and self._ssl_wanted(domain, params):
            bus.provision_step(self.env, self,
                               _("Emitiendo HTTPS (propagación + certbot)…"))
            try:
                ssl_result = self._provision_ssl(
                    instance_rec, change_id=dns_change_id)
            except Exception as error:  # noqa: BLE001 — el cron lo retoma
                instance_rec._set_ssl_error("pending", str(error))
                self.message_post(body=_(
                    "El paso HTTPS falló (%s): la instancia quedó en HTTP y "
                    "el reintento automático sigue.") % error)
        bus.provision_done(self.env, self, ok=True,
                           message=_("Entorno activo en %(url)s.", url=(
                               "https://%s" % domain if ssl_result == "active"
                               else domain)))
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
        self._pcm_assert_env_not_imported("instance_install")
        # El job de instalación NO termina en `failed` cuando falla: captura el
        # error, deja entorno e instancia en 'error' y devuelve False, así que
        # queue_job lo marca `done`. Buscar sólo `failed` dejaba el reintento
        # sin nada que reintentar (ensayo de Barraca Julia, 2026-09-19). Con el
        # entorno en 'error' y la máquina viva, el último install del entorno
        # —done o failed— es el que falló.
        machine = self.ec2_instance_id or self.ec2_instance_ids.filtered(
            lambda m: m.instance_state != "terminated")[:1]
        failed_job = self.env["queue.job"]
        if self.state == "error" and machine and machine.instance_state != "terminated":
            failed_job = self.env["queue.job"].search([
                ("model_name", "=", self._name),
                ("method_name", "=", "job_install_instance"),
                ("state", "in", ("failed", "done")),
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

    # Flujos que todavía ESCRIBEN con rutas del layout legacy → bloque de R4
    # que los recablea (y levanta su guard). Regla permanente: lo destructivo
    # no-recableado se bloquea en runtime, no se anota en un doc. El guard se
    # quita EN EL MISMO commit que recablea el flujo (config y addons: R4-B2).
    # Vacío: R4-B1..B5 recablearon todos los flujos legacy-destructivos (cada
    # bloque levantó su guard al recablear). Se conserva la maquinaria del
    # guard (por si un flujo futuro la necesita) aunque hoy no bloquee nada.
    LEGACY_FLOW_UNBLOCKED_IN = {}

    def _legacy_flow_blocked_reason(self, flow):
        """Por qué un flujo legacy-destructivo NO puede correr acá (o None).

        Un flujo aún no recableado que escribe/borra con rutas legacy
        (`/etc/odoo/odoo.conf`, unit ``odoo``, dropdb por nombre, filestore
        viejo) solo es seguro sobre un servidor legacy PURO: una única
        instancia no archivada con la unit compartida. Si hay alguna
        instancia materializada al layout multi — o más de una (destino
        ambiguo) — correrlo podría tocar el Odoo de OTRO cliente (el peor
        caso: el restore dropea por nombre una base viva ajena sin haber
        parado su servicio). Los jobs usan la razón para fallar honesto;
        las acciones interactivas usan :meth:`_ensure_legacy_flow_allowed`.
        """
        self.ensure_one()
        instances = self.instance_ids.filtered(lambda i: i.state != "archived")
        multi = instances.filtered(
            lambda i: i.service_name and i.service_name != LEGACY_SERVICE)
        if not multi and len(instances) <= 1:
            return None
        return _(
            "Operación «%(flow)s» bloqueada en este servidor: todavía usa "
            "rutas del layout legacy y acá conviven %(count)d instancias "
            "(layout multi-Odoo) — correrla podría tocar el Odoo de OTRO "
            "cliente. Se libera al recablearse en %(block)s.",
            flow=flow, count=len(instances),
            block=self.LEGACY_FLOW_UNBLOCKED_IN.get(flow, "R4"),
        )

    def _ensure_legacy_flow_allowed(self, flow):
        """Guard interactivo: levanta UserError si el flujo está bloqueado."""
        reason = self._legacy_flow_blocked_reason(flow)
        if reason:
            raise UserError(reason)
        return True

    def _is_legacy_layout(self):
        """¿Este servidor corre un Odoo instalado con el layout legacy (pre-R3)?

        Criterio: alguna instancia NO archivada, YA instalada (activa), cuya
        unit es la legacy compartida ``odoo`` (un solo Odoo en /opt/odoo).
        Las instancias draft no cuentan: nacen con los defaults legacy hasta
        materializarse al instalar.
        """
        self.ensure_one()
        return any(
            inst.state == "active" and inst.service_name == LEGACY_SERVICE
            for inst in self.instance_ids)

    def action_create_instance(self):
        """Agregar OTRO Odoo a este servidor — abre el wizard real (R3-B3).

        Gate por layout (D-R3.2): un servidor LEGACY sigue gated con el
        motivo nuevo (la adopción in-place al layout /opt/pcm es una
        mini-fase posterior si hace falta). Los servidores multi-Odoo — o
        vírgenes de instalación — abren el wizard.
        """
        self.ensure_one()
        if self.state != "active":
            raise UserError(_(
                "Solo se agregan instancias a un servidor activo."))
        if self._is_legacy_layout():
            raise UserError(_(
                "Este servidor tiene layout legacy (un solo Odoo pre-R3): "
                "montarle un segundo Odoo requiere adoptarlo al layout "
                "multi-Odoo (mini-fase pendiente del recableo). Para un Odoo "
                "nuevo hoy, creá un entorno."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Agregar instancia"),
            "res_model": "primate.cloud.instance.create.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_environment_id": self.id},
        }

    # ------------------------------------------------------------------
    # Multi-Odoo (R3-B2): bootstrap del servidor + agregar instancia
    # ------------------------------------------------------------------
    def _active_machine(self):
        """La máquina viva del servidor (o recordset vacío)."""
        self.ensure_one()
        machine = self.ec2_instance_id or self.ec2_instance_ids.filtered(
            lambda m: m.instance_state != "terminated")[:1]
        if machine and machine.instance_state == "terminated":
            return machine.browse()
        return machine

    def job_bootstrap_server(self):
        """Job: prepara el servidor para multi-Odoo (bootstrap idempotente).

        El script sale enseguida si el marker ya está; correrlo de nuevo es
        barato y es la ÚNICA forma válida de saber que el servidor está
        listo (el campo ``multiodoo_ready`` es caché de UI).
        """
        self.ensure_one()
        try:
            machine = self._active_machine()
            if not machine:
                raise UserError(_(
                    "El servidor no tiene una máquina activa para bootstrapear."))
            base = self.account_id._get_aws_service()
            ssm = aws_ssm.AwsSsmService(base)
            region = machine.region or self.account_id.default_region
            self._ensure_server_bootstrap(ssm, machine, region)
            return True
        except Exception as error:  # noqa: BLE001 - se audita, no se relanza
            self.message_post(body=_("Bootstrap multi-Odoo fallido: %s") % error)
            self._log("server_bootstrap",
                      name=_("Bootstrap multi-Odoo: %s") % self.name,
                      result="failed", error_message=str(error))
            return False

    def _ensure_server_bootstrap(self, ssm, machine, region):
        """Corre bootstrap_server.sh por SSM y exige el marker REAL.

        Regla permanente: la decisión «este servidor está listo para montar
        otro Odoo» se toma del marker en disco (el script lo chequea y
        no-opea si ya está), NUNCA del caché ``multiodoo_ready``. Levanta
        UserError si el bootstrap no terminó bien (el caller audita).
        """
        self.ensure_one()
        script = self._build_bootstrap_script()
        output = ssm.run_script(
            machine.aws_instance_id, script, region=region,
            comment="pcm bootstrap: %s" % self.name, timeout=900,
            check=False,   # inspecciona status + centinela PCM_BOOTSTRAP_OK
        )
        stdout = output.get("stdout") or ""
        ok = output.get("status") == "Success" and "PCM_BOOTSTRAP_OK" in stdout
        if not ok:
            raise UserError(_(
                "El bootstrap multi-Odoo no terminó bien (estado: %(status)s"
                "%(detail)s).",
                status=output.get("status"),
                detail=self._pcm_error_detail(stdout, output.get("stderr")),
            ))
        # Solo se audita la aplicación REAL (el no-op por marker es ruido).
        if "ya aplicado" not in stdout:
            self._log("server_bootstrap",
                      name=_("Bootstrap multi-Odoo: %s") % self.name,
                      result="success", aws_request_id=output.get("command_id"))
        if not self.multiodoo_ready:
            self.multiodoo_ready = True
        return True

    def job_add_instance(self, instance_id, params):
        """Job R3-B2: monta OTRO Odoo en este servidor SIN tocar los vivos.

        Orden (diseño R3 §8, con las dos reglas permanentes en el centro):

        1. **Lock por servidor** (sesión + unlock en finally): dos «agregar
           instancia» sobre la misma máquina se serializan.
        2. **Bootstrap fresco**: el marker en disco es la verdad, no el caché.
        3. **Snapshot HTTP de TODAS las instancias vivas** del servidor.
        4. ``install_instance.sh`` por SSM (guardas + trap clean-slate).
        5. **Re-snapshot**: lo que servía ANTES tiene que servir DESPUÉS
           (patrón PCM_HTTP_WAS: lo ya muerto antes no bloquea, se anota).
           Si un vecino vivo dejó de responder, se DESMONTA la instancia
           nueva (teardown por slug) y el job queda failed con nombre y
           apellido en la bitácora.
        """
        self.ensure_one()
        params = self.env["primate.cloud.job.secret"].reveal(params)
        self._pcm_assert_env_not_imported("instance_install")
        instance = self.env["primate.cloud.instance"].browse(instance_id).exists()
        if not instance or instance.environment_id != self:
            self._log("instance_install", result="failed",
                      name=_("Agregar instancia a %s") % self.name,
                      error_message=_("La instancia no existe o no pertenece "
                                      "a este servidor."))
            return False
        title = _("Agregar instancia: %s") % instance.name
        lock_key = self._acquire_server_lock("instance-install")
        try:
            bus.provision_start(self.env, self, title=title)
            try:
                machine = self._active_machine()
                if not machine:
                    raise UserError(_(
                        "El servidor no tiene una máquina activa."))
                base = self.account_id._get_aws_service()
                ssm = aws_ssm.AwsSsmService(base)
                # La región es la de la MÁQUINA del servidor, siempre: el
                # formulario de staging trae su propio campo región (default: la
                # de la cuenta) y, si pisaba a la de la máquina, el SSM iba a
                # otra región → "InvalidInstanceId" (ensayo de Barraca Julia,
                # servidor en us-east-2 con la cuenta en us-east-1).
                region = (machine.region or params.get("region")
                          or self.account_id.default_region)
                instance.state = "installing"
                # Layout por slug en el REGISTRO (rutas + pg_user propio,
                # D-R3.10): sin esto la instancia llegaría al install con los
                # defaults legacy (pg_user 'odoo' compartido) y el bookkeeping
                # de R4 (logs/config/backups por instancia) apuntaría mal.
                self._materialize_multiodoo_layout(instance, params)

                # 1b. DNS ANTES del install (D-R3.9 endurecida), best-effort
                # como siempre (§8.1): si Route53 falla, el install sigue, el
                # motivo queda en dns_pending y el paso SSL se saltea (sin
                # dominio resolviendo no hay Let's Encrypt).
                dns_change_id = None
                if params.get("create_dns"):
                    bus.provision_step(self.env, self, _("Configurando DNS…"))
                    try:
                        dns_change_id = self._provision_dns(
                            base, self.account_id, machine, params,
                            params.get("domain"), instance=instance)
                        instance.sudo().dns_pending = False
                    except Exception as error:  # noqa: BLE001 - best-effort
                        instance._set_dns_pending(str(error), params)
                        self.message_post(body=_(
                            "ADVERTENCIA: el DNS falló (%s). El install sigue; "
                            "reintentar luego con «Crear DNS ahora».") % error)

                # 2. Bootstrap fresco (no-op barato si el marker ya está).
                bus.provision_step(self.env, self,
                                   _("Verificando el bootstrap multi-Odoo…"))
                self._ensure_server_bootstrap(ssm, machine, region)

                # 3. Salud de los vecinos ANTES (desde afuera).
                bus.provision_step(self.env, self,
                                   _("Salud de las instancias vivas (antes)…"))
                before = self._instances_health_snapshot(
                    ssm, machine, region, exclude=instance)
                alive_before = [i for i, ok in before.items() if ok]
                dead_before = [i for i, ok in before.items() if not ok]
                if dead_before:
                    # No se les achaca al install un muerto previo; se anota.
                    self.message_post(body=_(
                        "Instancias que YA no respondían antes del install "
                        "(no bloquean): %s.") % ", ".join(
                            i.display_name for i in dead_before))

                # 4. Install por slug (el trap del script desmonta lo propio).
                bus.provision_step(self.env, self, _(
                    "Instalando Odoo por SSM (puede tardar varios minutos)…"))
                others = self.instance_ids.filtered(
                    lambda i: i.id != instance.id and i.state != "archived")
                self._provision_run_instance_install(
                    ssm, machine, instance, params, region,
                    params.get("domain") or instance.main_url or instance.slug,
                    is_default=not others)

                # 5. Re-snapshot: vivo-antes DEBE seguir vivo-después.
                bus.provision_step(self.env, self,
                                   _("Re-verificando la salud de los vecinos…"))
                broken = [i for i in alive_before
                          if not self._instance_http_alive(ssm, machine,
                                                           region, i)]
                if broken:
                    self._teardown_instance_artifacts(
                        ssm, machine, region, instance, params)
                    raise UserError(_(
                        "Vecinos que servían ANTES dejaron de responder tras "
                        "el install: %s. Se desmontó la instancia nueva; los "
                        "vecinos no se tocaron.") % ", ".join(
                            i.display_name for i in broken))
            except Exception as error:  # noqa: BLE001 - se audita, no se relanza
                instance.state = "error"
                self.message_post(body=_("Agregar instancia fallido: %s") % error)
                self._log("instance_install", name=title, result="failed",
                          error_message=str(error), record=instance)
                bus.provision_done(self.env, self, ok=False,
                                   message=_("Instalación fallida: %s") % error)
                return False

            # Éxito: activar + registrar la BD de la instancia.
            database = self._upsert_provisioned_database({
                "name": params.get("db_name"),
                "account_id": self.account_id.id,
                "environment_id": self.id,
                "instance_id": instance.id,
                "db_type": "local_pg",
                "ec2_instance_id": machine.id,
                "state": "available",
            })
            instance.write({
                "state": "active",
                "main_url": params.get("domain") or instance.main_url,
                "database_id": database.id,
            })
            # HTTPS verificado (D-R3.9 endurecida): el DNS ya se intentó ANTES
            # del install; con DNS sano se emite el certificado. Warning +
            # auto-retry, jamás bloquea (decisión 1 del OK).
            if (self._ssl_wanted(params.get("domain") or instance.main_url,
                                 params) and not instance.dns_pending):
                bus.provision_step(self.env, self,
                                   _("Emitiendo HTTPS (propagación + certbot)…"))
                try:
                    self._provision_ssl(instance, change_id=dns_change_id)
                except Exception as error:  # noqa: BLE001 — el cron lo retoma
                    instance._set_ssl_error("pending", str(error))
                    self.message_post(body=_(
                        "El paso HTTPS falló (%s): la instancia quedó en HTTP "
                        "y el reintento automático sigue.") % error)
            self._log("instance_install", name=title, result="success",
                      record=instance)
            bus.provision_done(self.env, self, ok=True,
                               message=_("Instancia %s activa.") % instance.name)
            return True
        finally:
            self._release_server_lock(lock_key)

    def _provision_run_instance_install(self, ssm, machine, instance, params,
                                        region, domain, is_default):
        """Renderiza y corre install_instance.sh por SSM (chatter + bitácora).

        Exige estado Success **y** el marcador ``PCM_INSTALL_OK`` (un run
        truncado por timeout puede quedar Success a medias). Ante fallo,
        levanta UserError con el marcador ``PCM_ERR_*`` accionable si vino.
        """
        script = self._build_instance_install_script(
            instance, dict(params, domain=domain), is_default=is_default)
        output = ssm.run_script(
            machine.aws_instance_id, script, region=region,
            comment="pcm instance install: %s" % instance.slug,
            timeout=params.get("install_timeout") or 900,
            check=False,   # inspecciona status + centinela PCM_INSTALL_OK
        )
        stdout = output.get("stdout") or ""
        ok = output.get("status") == "Success" and "PCM_INSTALL_OK" in stdout
        self.message_post(body=_(
            "Instalación de la instancia %(slug)s (SSM) — estado: %(status)s.",
            slug=instance.slug, status=output.get("status")))
        self._log("ssm_command",
                  name=_("Instalar instancia: %s") % instance.display_name,
                  result="success" if ok else "failed",
                  error_message=None if ok else (
                      self._pcm_error_detail(stdout, output.get("stderr"))
                      or output.get("status")),
                  aws_request_id=output.get("command_id"), record=instance)
        if not ok:
            raise UserError(_(
                "La instalación por SSM no terminó con éxito (estado: "
                "%(status)s%(detail)s).",
                status=output.get("status"),
                detail=self._pcm_error_detail(stdout, output.get("stderr")),
            ))

    @staticmethod
    def _pcm_error_detail(stdout, stderr=None):
        """Primera línea ``PCM_ERR_*`` del stdout (o stderr), para mensajes
        accionables; '' si no hay nada que citar."""
        for line in (stdout or "").splitlines():
            if line.strip().startswith("PCM_ERR"):
                return " — %s" % line.strip()
        stderr = (stderr or "").strip()
        return (" — %s" % stderr.splitlines()[0]) if stderr else ""

    # ------------------------------------------------------------------
    # Salud de instancias vivas (patrón PCM_HTTP_WAS a nivel job)
    # ------------------------------------------------------------------
    def _instances_health_snapshot(self, ssm, machine, region, exclude=None):
        """HTTP de cada instancia ACTIVA del servidor → {instancia: bool}.

        Es la referencia del contrato no-romper-lo-ajeno: lo que servía
        ANTES del install tiene que servir DESPUÉS; lo que ya estaba roto
        no bloquea (no se le achaca al install un muerto previo).
        """
        self.ensure_one()
        result = {}
        for inst in self.instance_ids.filtered(
                lambda i: i.state == "active"
                and (not exclude or i.id != exclude.id)):
            result[inst] = self._instance_http_alive(ssm, machine, region, inst)
        return result

    def _instance_http_alive(self, ssm, machine, region, instance):
        """¿La instancia responde HTTP? Probe corto, read-only.

        Desde AFUERA (IP del servidor + header Host del dominio: valida
        también el ruteo nginx) cuando hay IP pública y dominio; si no,
        fallback por SSM con curl al puerto propio en localhost.
        """
        if machine.public_ip and instance.main_url:
            # Con HTTPS activo el probe va DIRECTO por https (verificando el
            # cert: un cert roto es salud rota); si https no responde se cae
            # al probe http por IP para distinguir "TLS roto" de "Odoo muerto".
            if instance.ssl_state == "active":
                try:
                    response = requests.get(
                        "https://%s/web/login" % instance.main_url,
                        timeout=HEALTH_PROBE_TIMEOUT, allow_redirects=True)
                    if response.status_code < 500:
                        return True
                except requests.RequestException:
                    pass
            try:
                response = requests.get(
                    "http://%s/web/login" % machine.public_ip,
                    headers={"Host": instance.main_url},
                    timeout=HEALTH_PROBE_TIMEOUT, allow_redirects=True)
                return response.status_code < 500
            except requests.RequestException:
                return False
        if not instance.http_port:
            return False
        command = ("curl -s -o /dev/null -m 5 -w '%%{http_code}' "
                   "http://127.0.0.1:%d/web/login || true") % instance.http_port
        # check=False: es un probe read-only; un fallo de SSM degrada a "muerto"
        # (código no-dígito → False), no a excepción.
        output = ssm.run_script(
            machine.aws_instance_id, command, region=region,
            comment="pcm health probe: %s" % instance.slug,
            timeout=30, agent_timeout=30, check=False)
        code = (output.get("stdout") or "").strip()
        # curl imprime 000 cuando NO pudo conectar: eso es muerto, no vivo.
        return code.isdigit() and 100 <= int(code) < 500

    # ------------------------------------------------------------------
    # Teardown por slug (rollback orquestado desde el job)
    # ------------------------------------------------------------------
    def _build_instance_teardown_script(self, instance, db_name=None,
                                        drop_db=False):
        """Desmonta los artefactos POR SLUG de una instancia (orden del trap).

        Para el rollback orquestado desde el job (vecino roto DESPUÉS de un
        install exitoso — el trap del script ya no corre) y como remedio
        futuro del ``PCM_ERR_DIRTY_SLUG``. Misma semántica clean-slate que
        el trap: SOLO lo del slug; ``dropdb`` únicamente con ``drop_db``
        (la BD nació en la corrida que se desmonta — garantizado por la
        guarda ``PCM_ERR_DB_EXISTS`` del install); JAMÁS toca el usuario
        PG compartido ``odoo`` de los servidores legacy.
        """
        self.ensure_one()
        instance.ensure_one()
        slug = instance.slug or ""
        if not SLUG_RE.match(slug):
            raise UserError(_("Slug inválido para desmontar: %r.") % slug)
        if drop_db and not DB_NAME_RE.match(db_name or ""):
            raise UserError(_("Nombre de base inválido: %r.") % db_name)
        pg_user = instance.pg_user or ""
        drop_user = pg_user.startswith("odoo_") and SLUG_RE.match(
            pg_user.replace("_", "-"))
        lines = [
            "#!/usr/bin/env bash",
            # Sin -e: el teardown es best-effort, desmonta todo lo que pueda.
            "set -uo pipefail",
            'UNIT="odoo-%s.service"' % slug,
            'systemctl stop "${UNIT}" 2>/dev/null || true',
            'systemctl disable "${UNIT}" 2>/dev/null || true',
            'rm -f "/etc/systemd/system/${UNIT}"',
            "systemctl daemon-reload",
            'rm -f "/etc/nginx/sites-enabled/pcm-%s.conf" '
            '"/etc/nginx/sites-available/pcm-%s.conf"' % (slug, slug),
            "nginx -t && systemctl reload nginx || true",
        ]
        if instance.main_url:
            # Punto F del OK de HTTPS: el certificado muere con la instancia
            # (best-effort: un cert huérfano no debe frenar el teardown).
            lines.append("certbot delete --cert-name %s -n 2>/dev/null || true"
                         % shlex.quote(instance.main_url))
        if drop_db:
            lines.append('sudo -u postgres dropdb --if-exists "%s"' % db_name)
        if drop_user:
            lines.append('sudo -u postgres dropuser --if-exists "%s"' % pg_user)
        lines += [
            'rm -rf "/opt/pcm/instances/%s"' % slug,
            'rm -f "/etc/odoo/%s.conf" "/etc/logrotate.d/odoo-%s"' % (slug, slug),
            'echo "PCM_TEARDOWN_DONE"',
        ]
        return "\n".join(lines) + "\n"

    def _teardown_instance_artifacts(self, ssm, machine, region, instance,
                                     params):
        """Corre el teardown por slug en el servidor y lo deja en el chatter."""
        script = self._build_instance_teardown_script(
            instance, db_name=params.get("db_name"), drop_db=True)
        # check=False BY DECISION: el teardown es best-effort (script sin -e,
        # desmonta todo lo que pueda); su éxito se mide por el centinela
        # PCM_TEARDOWN_DONE, no por el exit code, y reporta INCOMPLETO si falta.
        output = ssm.run_script(
            machine.aws_instance_id, script, region=region,
            comment="pcm instance teardown: %s" % instance.slug, timeout=300,
            check=False)
        done = "PCM_TEARDOWN_DONE" in (output.get("stdout") or "")
        self.message_post(body=_(
            "Teardown de la instancia %(slug)s: %(result)s.",
            slug=instance.slug,
            result=_("completado") if done else _("INCOMPLETO — revisar el "
                                                  "servidor a mano")))
        return done

    def _materialize_multiodoo_layout(self, instance, params):
        """Escribe en la instancia las rutas/credencial del layout por slug.

        Idempotente (retry con los mismos args → mismos valores). El
        pg_user propio ``odoo_<slug>`` materializa D-R3.10; con BD remota
        (RDS) la credencial es la master que viaja en params.
        """
        self.ensure_one()
        instance.ensure_one()
        slug = instance.slug or ""
        if not SLUG_RE.match(slug):
            raise UserError(_("Slug inválido para instalar: %r.") % slug)
        if not instance.odoo_version:
            raise UserError(_("La instancia no tiene versión de Odoo."))
        runtime = "/opt/pcm/runtime/odoo-%s" % instance.odoo_version
        vals = {
            "service_name": "odoo-%s" % slug,
            "conf_path": "/etc/odoo/%s.conf" % slug,
            "data_dir": "/opt/pcm/instances/%s/data" % slug,
            "addons_dir": "/opt/pcm/instances/%s/addons" % slug,
            # Runtime por instancia (R4-B1): con esto operan shell/-u/-i y logs.
            "python_bin": "%s/venv/bin/python3" % runtime,
            "odoo_bin": "%s/src/odoo-bin" % runtime,
            "log_path": "/opt/pcm/instances/%s/log/odoo.log" % slug,
        }
        if params.get("db_mode") == "local_pg":
            vals["pg_user"] = "odoo_%s" % slug.replace("-", "_")
        else:
            vals["pg_user"] = params.get("db_user") or "odoo"
        # Puertos: se respetan los ya asignados (el allocator corre al CREAR
        # la instancia; la primaria nace con el slot 0 = 8069/8072).
        if not instance.http_port or not instance.gevent_port:
            vals["http_port"], vals["gevent_port"] = self._allocate_ports()
        instance.write(vals)
        return instance

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
            check=False,   # inspecciona status y lo refleja en el chatter/estado
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

    def _provision_dns(self, base, account, machine, params, domain,
                       instance=None):
        """Crea el registro DNS A apuntando a la IP pública (si se pidió).

        ``instance`` (primate.cloud.instance) ata el registro al Odoo real;
        sin ella, el mixin de R1 resuelve la instancia primaria (correcto
        para la cadena, donde la primaria ES la instalada).
        """
        if not params.get("create_dns"):
            return
        if not machine.public_ip:
            self.message_post(body=_("Sin IP pública: se omite la creación del registro DNS."))
            return
        route53 = aws_route53.AwsRoute53Service(base)
        change_id = route53.create_record(
            hosted_zone_id=params["hosted_zone_id"],
            name=domain,
            record_type="A",
            value=machine.public_ip,
            ttl=params.get("ttl") or 300,
            comment="pcm provision: %s" % self.name,
        )
        vals = {
            "name": domain,
            "account_id": account.id,
            "environment_id": self.id,
            "hosted_zone_id": params["hosted_zone_id"],
            "record_type": "A",
            "record_value": machine.public_ip,
            "ttl": params.get("ttl") or 300,
            "state": "active",
        }
        if instance is not None:
            vals["instance_id"] = instance.id
        record = self.env["primate.cloud.dns.record"].create(vals)
        self.message_post(body=_("Registro DNS creado: %s -> %s.")
                          % (domain, machine.public_ip))
        self._log("dns_create", name=_("Crear DNS: %s") % domain,
                  result="success", aws_request_id=change_id, record=record)
        # El paso SSL usa el ChangeId para esperar INSYNC antes de certbot.
        return change_id

    def pcm_is_imported(self):
        """¿Entorno importado? Marca de origen O alguna máquina adoptada."""
        self.ensure_one()
        return self.pcm_origin != "pcm" or any(
            m.management_state == "adopted_readonly"
            for m in self.ec2_instance_ids)

    def _pcm_assert_env_not_imported(self, accion):
        """Regla dura 2026-10-22: un entorno IMPORTADO no se toca — corte
        temprano y con mensaje (y el cerrojo de frontera igual bloquearía la
        llamada a AWS si algo se escapara)."""
        for env_rec in self:
            for machine in env_rec.ec2_instance_ids:
                if machine.management_state == "adopted_readonly":
                    machine._exigir_gestion_habilitada(accion)
            if env_rec.pcm_origin != "pcm":
                durable_log(self.env, {
                    "action_type": "readonly_block", "result": "failed",
                    "name": _("Bloqueado (importado): %(a)s sobre %(n)s",
                              a=accion, n=env_rec.name),
                    "user_id": self.env.uid,
                    "resource_model": env_rec._name,
                    "resource_id": env_rec.id,
                    "error_message": _("Entorno importado: solo lectura."),
                })
                raise UserError(_(
                    "«%(n)s» es un entorno IMPORTADO (%(o)s): solo lectura "
                    "sin excepción — no se puede %(a)s.", n=env_rec.name,
                    o=dict(env_rec._fields["pcm_origin"].selection)[
                        env_rec.pcm_origin], a=accion))

    def write(self, vals):
        """La marca de origen de un importado es inmutable."""
        if "pcm_origin" in vals and not self.env.context.get("pcm_import_sync"):
            for env_rec in self:
                if env_rec.pcm_origin != "pcm" and \
                        vals["pcm_origin"] != env_rec.pcm_origin:
                    raise UserError(_(
                        "«%s» es un entorno IMPORTADO: su marca de origen es "
                        "permanente.", env_rec.name))
                if env_rec.pcm_origin == "pcm" and vals["pcm_origin"] != "pcm":
                    raise UserError(_(
                        "La marca de importado la pone solo el import."))
        return super().write(vals)

    def unlink(self):
        for env_rec in self:
            if env_rec.pcm_is_imported():
                raise UserError(_(
                    "«%s» es un entorno IMPORTADO (solo lectura): no se "
                    "elimina de PCM.", env_rec.name))
        return super().unlink()

    @api.model
    def _check_hosted_zone(self, account, hosted_zone_id):
        """Guardia temprana: la zona DEBE existir en la cuenta ANTES de gastar.

        La llaman los wizards (aprovisionar / agregar instancia) al confirmar:
        un id inexistente cortaba recién en el job, con la EC2 ya creada y un
        NoSuchHostedZone críptico (corrida real 2026-10-22, data de demo)."""
        route53 = aws_route53.AwsRoute53Service(account._get_aws_service())
        if not route53.zone_exists(hosted_zone_id):
            raise UserError(_(
                "La zona Route53 «%(zone)s» no existe en la cuenta "
                "«%(account)s»: revisá el Hosted Zone ID (Registros DNS → "
                "zonas de la cuenta). No se creó ningún recurso.",
                zone=hosted_zone_id, account=account.display_name))
        return True

    # ==================================================================
    # HTTPS (D-R3.9 endurecida, 2026-10-22): el certificado es un paso
    # VERIFICADO del modelo, no la cola best-effort del install. Orden:
    # Route53 INSYNC → dig contra los NS autoritativos (+ guarda AAAA) →
    # certbot --nginx → expiry por openssl → GET https real + redirect →
    # web.base.url. certbot SOLO corre con la propagación confirmada
    # (ajuste C: los rate limits de Let's Encrypt no se queman esperando).
    # ==================================================================
    @staticmethod
    def _ssl_wanted(domain, params):
        """¿Corre el paso SSL al terminar el montaje? Siempre que el dominio
        sea REAL — con o sin DNS en Route 53 (sslip.io, o DNS del cliente):
        la verificación de propagación decide si ya se puede. No: si el
        usuario lo apagó (ssl_auto=False), o dominios reservados/placeholder
        que jamás van a resolver (.local, .invalid, .localhost, .test...)."""
        if params.get("ssl_auto") is False:
            return False
        dominio = (domain or "").strip().lower().rstrip(".")
        if "." not in dominio:
            return False
        reservados = (".local", ".invalid", ".localhost", ".test",
                      ".example", ".internal", ".lan")
        return not dominio.endswith(reservados)

    def _ssl_use_le_staging(self):
        """``pcm.ssl_use_le_staging`` (default False): agrega ``--staging`` a
        certbot para iterar sin consumir límites de producción de LE. Los
        certs de staging NO validan en el navegador: la verificación real
        baja a verify=False mientras está prendido."""
        value = self.env["ir.config_parameter"].sudo().get_param(
            "pcm.ssl_use_le_staging", "False")
        return str(value).strip().lower() in ("1", "true", "yes")

    def _provision_ssl(self, instance, change_id=None):
        """Un ciclo completo de emisión HTTPS para ``instance.main_url``.

        NUNCA rompe el montaje (decisión 1 del OK): toda falla deja la
        instancia sirviendo HTTP con ``ssl_state`` pending/failed y motivo.

        Returns:
            str: ``"active"`` / ``"not_propagated"`` (no consumió intento) /
            ``"certbot_failed"`` / ``"verify_failed"`` / ``"aaaa_conflict"`` /
            ``"no_domain"`` / ``"no_machine"``.
        """
        self.ensure_one()
        domain = (instance.main_url or "").strip().rstrip("/")
        if not domain:
            instance.sudo().ssl_state = "none"
            return "no_domain"
        machine = self._active_machine()
        if not machine or not machine.public_ip:
            instance._set_ssl_error("pending", _(
                "El servidor no tiene máquina activa con IP pública."))
            return "no_machine"
        # Regla dura 2026-10-22: certbot JAMÁS corre sobre un importado.
        machine._exigir_gestion_habilitada("ssl_enable")
        base = self.account_id._get_aws_service()
        ssm = aws_ssm.AwsSsmService(base)
        region = machine.region or self.account_id.default_region
        if instance.ssl_state != "pending":
            instance.sudo().write({
                "ssl_state": "pending",
                "ssl_pending_since": fields.Datetime.now()})
        if not instance.ssl_pending_since:
            instance.sudo().ssl_pending_since = fields.Datetime.now()

        # 1. Route53 INSYNC (solo con ChangeId fresco del provisioning).
        if change_id:
            route53 = aws_route53.AwsRoute53Service(base)
            for _attempt in range(SSL_INSYNC_POLLS):
                if route53.get_change_status(change_id) == "INSYNC":
                    break
                time.sleep(SSL_INSYNC_POLL_SECONDS)

        # 2. Propagación contra los NS autoritativos + guarda AAAA (ajuste D).
        propagation = self._ssl_check_propagation(
            ssm, machine, region, instance, domain)
        if propagation == "aaaa_conflict":
            reason = _(
                "El dominio %(d)s tiene un registro AAAA (IPv6) que no apunta "
                "a esta instancia: Let's Encrypt validaría contra esa IPv6 "
                "ajena y fallaría. Borrá el AAAA (o apuntalo a la máquina) y "
                "reintentá con «Activar HTTPS ahora».", d=domain)
            instance._set_ssl_error("failed", reason)
            instance.message_post(body=reason)
            self._log("ssl_enable", name=_("HTTPS: %s") % domain,
                      result="failed", error_message=reason, record=instance)
            return "aaaa_conflict"
        if propagation != "ok":
            # NO consume intento de certbot: el cron re-chequea y recién
            # cuando el dominio resuelva a la IP corre certbot.
            instance._set_ssl_error("pending", _(
                "DNS aún sin propagar (%(detail)s): certbot NO se ejecutó; "
                "el reintento automático sigue.", detail=propagation))
            return "not_propagated"

        # 3. certbot (ÚNICO punto que consume intento).
        use_staging = self._ssl_use_le_staging()
        ok, detail = self._ssl_run_certbot(
            ssm, machine, region, instance, domain, use_staging)
        if not ok:
            attempts = instance.ssl_retry_count + 1
            exhausted = attempts >= SSL_RETRY_MAX
            instance.sudo().write({
                "ssl_retry_count": attempts,
                "ssl_state": "failed" if exhausted else "pending",
                "ssl_last_error": (detail or "")[:400],
            })
            instance.message_post(body=_(
                "certbot falló (intento %(n)s/%(max)s): %(why)s%(end)s",
                n=attempts, max=SSL_RETRY_MAX, why=detail,
                end=_(" — reintentos agotados: queda en HTTP con el botón "
                      "«Activar HTTPS ahora».") if exhausted else ""))
            self._log("ssl_enable", name=_("HTTPS: %s") % domain,
                      result="failed", error_message=(detail or "")[:400],
                      record=instance)
            return "certbot_failed"

        # 4. Vencimiento real del certificado (openssl en el servidor).
        expiry = self._ssl_read_cert_expiry(ssm, machine, region, domain)

        # 5. Verificación REAL: https responde y http redirige.
        verified, why = self._ssl_verify_https(domain, verify=not use_staging)
        if not verified:
            attempts = instance.ssl_retry_count + 1
            exhausted = attempts >= SSL_RETRY_MAX
            instance.sudo().write({
                "ssl_retry_count": attempts,
                "ssl_state": "failed" if exhausted else "pending",
                "ssl_last_error": (why or "")[:400],
            })
            instance.message_post(body=_(
                "certbot emitió pero la verificación HTTPS real falló: "
                "%(why)s (¿puerto 443 cerrado en el camino?).", why=why))
            self._log("ssl_enable", name=_("HTTPS: %s") % domain,
                      result="failed", error_message=(why or "")[:400],
                      record=instance)
            return "verify_failed"

        # 6. web.base.url de la instancia a https (ajuste B, best-effort).
        self._ssl_update_web_base_url(ssm, machine, region, instance, domain)

        instance.sudo().write({
            "ssl_state": "active",
            "ssl_cert_expiry": expiry,
            "ssl_last_error": False,
            "ssl_expiry_warned": False,
        })
        instance.message_post(body=_(
            "✅ HTTPS activo y verificado en https://%(d)s%(stg)s (el "
            "certificado vence el %(exp)s).", d=domain,
            stg=_(" [LE STAGING: certificado de prueba]") if use_staging else "",
            exp=expiry or "?"))
        self._log("ssl_enable", name=_("HTTPS: %s") % domain,
                  result="success", record=instance)
        return "active"

    def _ssl_check_propagation(self, ssm, machine, region, instance, domain):
        """``dig`` desde el servidor: A contra CADA NS autoritativo de la zona
        (Route53, vía el registro DNS de PCM) y AAAA del resolver público.

        Returns:
            str: ``"ok"`` / ``"aaaa_conflict"`` / detalle humano de qué falta.
        """
        nameservers = []
        record = self.env["primate.cloud.dns.record"].sudo().search(
            [("name", "=", domain), ("hosted_zone_id", "!=", False)], limit=1)
        if record:
            try:
                route53 = aws_route53.AwsRoute53Service(
                    self.account_id._get_aws_service())
                nameservers = route53.get_zone_nameservers(record.hosted_zone_id)
            except Exception:  # noqa: BLE001 — sin NS, cae al resolver local
                nameservers = []
        answers, aaaa = self._dig_answers(
            ssm, machine, region, domain, nameservers or ["system"])
        if aaaa:
            return "aaaa_conflict"
        if not answers:
            return _("dig no respondió desde el servidor")
        missing = [key for key, ips in answers.items()
                   if machine.public_ip not in ips]
        if missing:
            return _("%(who)s aún no devuelve %(ip)s",
                     who=", ".join(missing), ip=machine.public_ip)
        return "ok"

    @staticmethod
    def _dig_answers(ssm, machine, region, domain, servers):
        """``dig`` desde el SERVIDOR (solo lectura): respuestas A por resolver
        y la AAAA del resolver del sistema.

        Args:
            servers (list[str]): NS o resolvers a consultar; ``"system"`` es
                el resolver de la máquina.

        Returns:
            tuple(dict, list): ``({"A[<resolver>]": [ips]}, [ipv6])``.
        """
        lines = []
        for server in servers[:4]:
            at = "" if server == "system" else " @%s" % shlex.quote(server)
            lines.append(
                'echo "A[%s]=$(dig +short +time=3 +tries=1 A %s%s '
                '| tr \'\\n\' \',\')"' % (server, shlex.quote(domain), at))
        lines.append(
            'echo "AAAA=$(dig +short +time=3 +tries=1 AAAA %s '
            '| tr \'\\n\' \',\')"' % shlex.quote(domain))
        output = ssm.run_script(
            machine.aws_instance_id, "\n".join(lines), region=region,
            comment="pcm ssl propagation: %s" % domain,
            timeout=90, agent_timeout=60, check=False)
        answers = {}
        for line in (output.get("stdout") or "").splitlines():
            if "=" in line and (line.startswith("A[") or line.startswith("AAAA")):
                key, _sep, value = line.partition("=")
                # dig +short también lista los CNAME intermedios: solo IPv4/6.
                answers[key] = [v for v in value.strip().split(",")
                                if v and not v.endswith(".")]
        return answers, answers.pop("AAAA", [])

    def _ssl_run_certbot(self, ssm, machine, region, instance, domain,
                         use_staging):
        """Ejecuta certbot --nginx por SSM. Returns (ok, detalle)."""
        staging_flag = " --staging" if use_staging else ""
        # Un linaje de STAGING ya existente (p. ej. el parámetro quedó en True
        # en un ensayo) no se reemplaza solo: certbot -n se queda con él porque
        # no está vencido. Pidiendo producción, se fuerza la reemisión.
        force = "" if use_staging else (
            'if grep -qs "acme-staging" /etc/letsencrypt/renewal/%(d)s.conf; '
            'then EXTRA="--force-renewal"; fi\n' % {"d": domain})
        script = (
            "set -e\n"
            'EXTRA=""\n'
            "%(force)s"
            "certbot --nginx -n --agree-tos --redirect -m %(mail)s "
            "-d %(domain)s%(staging)s $EXTRA\n"
            "nginx -t && systemctl reload nginx\n"
            'echo "PCM_SSL_OK"' % {
                "force": force,
                "mail": shlex.quote(SSL_LE_EMAIL),
                "domain": shlex.quote(domain),
                "staging": staging_flag,
            })
        output = ssm.run_script(
            machine.aws_instance_id, script, region=region,
            comment="pcm ssl certbot: %s" % domain,
            timeout=420, check=False)
        stdout = output.get("stdout") or ""
        ok = output.get("status") == "Success" and "PCM_SSL_OK" in stdout
        if ok:
            return True, ""
        tail = "\n".join((stdout + "\n" + (output.get("stderr") or ""))
                         .strip().splitlines()[-4:])
        return False, tail or (output.get("status") or "sin salida")

    def _ssl_read_cert_expiry(self, ssm, machine, region, domain):
        """``notAfter`` del cert vivo (openssl), como Datetime (o False)."""
        output = ssm.run_script(
            machine.aws_instance_id,
            "openssl x509 -enddate -noout -in "
            "/etc/letsencrypt/live/%s/cert.pem || true" % shlex.quote(domain),
            region=region, comment="pcm ssl expiry: %s" % domain,
            timeout=60, agent_timeout=30, check=False)
        for line in (output.get("stdout") or "").splitlines():
            if line.startswith("notAfter="):
                raw = line.split("=", 1)[1].strip()
                try:
                    return datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z")
                except ValueError:
                    return False
        return False

    def _ssl_refresh_cert_expiry(self, instance):
        """Re-lee el vencimiento del cert (lo usa la vigilancia del cron)."""
        self.ensure_one()
        machine = self._active_machine()
        if not machine or not instance.main_url:
            return False
        ssm = aws_ssm.AwsSsmService(self.account_id._get_aws_service())
        region = machine.region or self.account_id.default_region
        expiry = self._ssl_read_cert_expiry(
            ssm, machine, region, instance.main_url)
        if expiry:
            instance.sudo().ssl_cert_expiry = expiry
        return expiry

    @staticmethod
    def _ssl_verify_https(domain, verify=True):
        """GET https real + redirect de http. Returns (ok, motivo)."""
        try:
            response = requests.get(
                "https://%s/web/login" % domain, timeout=15, verify=verify)
            if response.status_code >= 500:
                return False, "https respondió %s" % response.status_code
        except requests.RequestException as error:
            return False, "https no respondió: %s" % error
        try:
            redirect = requests.get(
                "http://%s/web/login" % domain, timeout=15,
                allow_redirects=False)
            if redirect.status_code not in (301, 302, 308):
                return False, ("http no redirige a https (respondió %s)"
                               % redirect.status_code)
        except requests.RequestException as error:
            return False, "http no respondió: %s" % error
        return True, ""

    def _ssl_update_web_base_url(self, ssm, machine, region, instance, domain):
        """``web.base.url`` → https en la BD de la instancia (ajuste B).

        Idempotente (upsert por SQL) y best-effort: un fallo acá se avisa
        pero no tumba el estado active — el próximo login de admin por https
        lo corrige solo. Corre en el SERVIDOR con las credenciales del
        odoo.conf de la instancia (sirve para BD local y RDS por igual)."""
        conf_path = instance.conf_path or "/etc/odoo/%s.conf" % instance.slug
        url = "https://%s" % domain
        script = (
            "python3 - <<'PCM_PY'\n"
            "import configparser, os, subprocess\n"
            "conf = configparser.ConfigParser()\n"
            "conf.read(%(conf)r)\n"
            "opts = conf['options']\n"
            "host = opts.get('db_host', 'localhost')\n"
            "host = 'localhost' if host in ('', 'False') else host\n"
            "port = opts.get('db_port', '5432')\n"
            "port = '5432' if port in ('', 'False') else port\n"
            "sql = (\"INSERT INTO ir_config_parameter(key, value, create_date,\"\n"
            "       \" write_date, create_uid, write_uid) SELECT\"\n"
            "       \" 'web.base.url', %(url)r, now(), now(), 1, 1 WHERE NOT\"\n"
            "       \" EXISTS (SELECT 1 FROM ir_config_parameter WHERE\"\n"
            "       \" key='web.base.url'); UPDATE ir_config_parameter SET\"\n"
            "       \" value=%(url)r, write_date=now() WHERE\"\n"
            "       \" key='web.base.url' AND value IS DISTINCT FROM %(url)r;\")\n"
            "env = dict(os.environ, PGPASSWORD=opts.get('db_password', ''))\n"
            "subprocess.run(['psql', '-h', host, '-p', port, '-U',\n"
            "                opts.get('db_user', 'odoo'), '-d',\n"
            "                opts.get('db_name', ''), '-v', 'ON_ERROR_STOP=1',\n"
            "                '-c', sql], env=env, check=True)\n"
            "print('PCM_BASEURL_OK')\n"
            "PCM_PY" % {"conf": conf_path, "url": url})
        output = ssm.run_script(
            machine.aws_instance_id, script, region=region,
            comment="pcm ssl web.base.url: %s" % domain,
            timeout=90, agent_timeout=60, check=False)
        ok = "PCM_BASEURL_OK" in (output.get("stdout") or "")
        if not ok:
            instance.message_post(body=_(
                "ADVERTENCIA: no pude fijar web.base.url=%(url)s en la BD de "
                "la instancia (se corrige solo con el próximo login de admin "
                "por https). Detalle: %(why)s", url=url,
                why=(output.get("stderr") or output.get("status") or "?")[:200]))
        return ok

    def _ssl_delete_certificate(self, ssm, machine, region, domain,
                                record=None):
        """``certbot delete`` best-effort + logueado (punto F / teardown)."""
        output = ssm.run_script(
            machine.aws_instance_id,
            "certbot delete --cert-name %s -n 2>&1 || true"
            % shlex.quote(domain),
            region=region, comment="pcm ssl delete: %s" % domain,
            timeout=90, agent_timeout=60, check=False)
        self._log("ssl_delete", name=_("Borrar certificado: %s") % domain,
                  result="success" if output.get("status") == "Success"
                  else "failed", record=record)
        return output

    def _apply_instance_domain_change(self, instance, old_domain):
        """Punto F: vhost reescrito al dominio nuevo (limpio, en 80), cert
        viejo borrado, y reemisión por el ciclo normal de _provision_ssl.

        El template del vhost es el MISMO de install_instance.sh (si se toca
        allá, tocarlo acá): regenerarlo limpio evita heredar los bloques 443
        que certbot inyectó para el dominio viejo."""
        self.ensure_one()
        machine = self._active_machine()
        if not machine:
            instance.message_post(body=_(
                "Cambio de dominio: el servidor no tiene máquina activa; el "
                "vhost quedará para el próximo ciclo (Activar HTTPS ahora)."))
            return False
        ssm = aws_ssm.AwsSsmService(self.account_id._get_aws_service())
        region = machine.region or self.account_id.default_region
        slug = instance.slug
        new_domain = (instance.main_url or "").strip()
        vhost = self._instance_vhost(instance, new_domain)
        script = (
            "set -e\n"
            "cat > /etc/nginx/sites-available/pcm-%(slug)s.conf <<'PCM_NGINX'\n"
            "%(vhost)s"
            "PCM_NGINX\n"
            "ln -sf /etc/nginx/sites-available/pcm-%(slug)s.conf "
            "/etc/nginx/sites-enabled/pcm-%(slug)s.conf\n"
            "nginx -t && systemctl reload nginx\n"
            'echo "PCM_VHOST_OK"' % {"slug": slug, "vhost": vhost})
        output = ssm.run_script(
            machine.aws_instance_id, script, region=region,
            comment="pcm domain change: %s" % slug,
            timeout=120, check=False)
        if "PCM_VHOST_OK" not in (output.get("stdout") or ""):
            instance._set_ssl_error("failed", _(
                "No pude reescribir el vhost al dominio nuevo: %s")
                % (output.get("stderr") or output.get("status") or "?")[:200])
            instance.message_post(body=instance.ssl_last_error)
            return False
        if old_domain:
            self._ssl_delete_certificate(
                ssm, machine, region, old_domain, record=instance)
        return self._provision_ssl(instance) == "active"

    @staticmethod
    def _instance_vhost(instance, domain):
        """vhost de nginx de una instancia (solo puerto 80; certbot le suma
        el 443 y el redirect). Es el MISMO template de install_instance.sh:
        si se toca allá, tocarlo acá."""
        return (
            "server {\n"
            "    listen 80;\n"
            "    server_name %(domain)s;\n"
            "    location /websocket {\n"
            "        proxy_pass http://127.0.0.1:%(gevent)s;\n"
            "        proxy_set_header Upgrade $http_upgrade;\n"
            '        proxy_set_header Connection "Upgrade";\n'
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "    }\n"
            "    location / {\n"
            "        proxy_pass http://127.0.0.1:%(http)s;\n"
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "        proxy_set_header X-Forwarded-Proto $scheme;\n"
            "        proxy_set_header X-Forwarded-Host $host;\n"
            "    }\n"
            "}\n" % {"domain": domain, "gevent": instance.gevent_port,
                     "http": instance.http_port})

    # ==================================================================
    # Dominio definitivo sobre una instancia que ya sirve (decisión
    # 2026-10-27): los montajes sin dominio quedan en sslip.io con HTTPS y
    # el dominio se suma después, por PCM («Crear DNS ahora», Route 53) o
    # a mano («Asociar dominio existente»). Los dos caminos terminan acá.
    #
    # NO destructivo: el dominio nuevo se monta EN PARALELO (vhost aparte +
    # certificado propio + GET https real) y recién con todo verificado se
    # retira el viejo. Si algo falla antes, se desarma lo nuevo y la
    # instancia sigue como estaba (sslip.io con HTTPS). Sin reinstalar ni
    # tocar la base, salvo web.base.url.
    # ==================================================================
    DOMAIN_CHECK_RESOLVERS = ("system", "1.1.1.1", "8.8.8.8")

    def _domain_resolution_problem(self, ssm, machine, region, domain,
                                   nameservers=None):
        """¿El dominio resuelve a la IP del servidor? None si sí; si no, el
        motivo diciendo A QUÉ apunta (lectura pura, dig desde el servidor)."""
        servers = list(nameservers or self.DOMAIN_CHECK_RESOLVERS)
        answers, aaaa = self._dig_answers(ssm, machine, region, domain, servers)
        if aaaa:
            return _(
                "%(d)s tiene un registro AAAA (IPv6: %(v6)s): Let's Encrypt "
                "validaría contra esa IPv6. Borralo (o apuntalo al servidor).",
                d=domain, v6=", ".join(aaaa))
        if not answers:
            return _("No pude consultar el DNS desde el servidor.")
        ajenos = []
        for key, ips in sorted(answers.items()):
            if machine.public_ip not in ips:
                ajenos.append("%s → %s" % (key[2:-1], ", ".join(ips)
                                           or _("no resuelve")))
        if ajenos:
            return _(
                "%(d)s no apunta a la IP del servidor (%(ip)s): %(det)s.",
                d=domain, ip=machine.public_ip, det="; ".join(ajenos))
        return None

    def _switch_domain(self, instance, new_domain, change_id=None,
                       nameservers=None, wait_propagation=0):
        """Pasa ``instance`` a ``new_domain`` sin cortar lo que ya sirve.

        Args:
            change_id (str): ChangeId de Route 53 (camino PCM): se espera
                INSYNC antes de mirar la propagación.
            nameservers (list[str]): NS autoritativos a consultar (camino
                PCM); sin ellos, resolvers públicos (camino manual).
            wait_propagation (int): segundos máximos esperando que el
                dominio resuelva (0 = una sola consulta y, si no, corta).

        Returns:
            str: ``"active"`` / ``"not_resolving"`` / ``"vhost_failed"`` /
            ``"certbot_failed"`` / ``"verify_failed"`` / ``"swap_failed"`` /
            ``"no_machine"``.
        """
        self.ensure_one()
        instance.ensure_one()
        instance._pcm_assert_not_imported(_("cambiar el dominio"))
        new_domain = (new_domain or "").strip().lower().rstrip(".")
        old_domain = (instance.main_url or "").strip().rstrip("/")
        machine = self._active_machine()
        if not machine or not machine.public_ip:
            return instance._domain_switch_failed(_(
                "El servidor no tiene máquina activa con IP pública."),
                "no_machine")
        machine._exigir_gestion_habilitada("domain_change")
        base = self.account_id._get_aws_service()
        ssm = aws_ssm.AwsSsmService(base)
        region = machine.region or self.account_id.default_region
        slug = instance.slug
        use_staging = self._ssl_use_le_staging()
        log_name = _("Dominio %(old)s → %(new)s", old=old_domain or "—",
                     new=new_domain)

        # 1. Route 53 INSYNC (solo camino PCM) + el dominio tiene que resolver
        #    a la IP del servidor. Sin eso no se toca NADA.
        if change_id:
            route53 = aws_route53.AwsRoute53Service(base)
            for _attempt in range(SSL_INSYNC_POLLS):
                if route53.get_change_status(change_id) == "INSYNC":
                    break
                time.sleep(SSL_INSYNC_POLL_SECONDS)
        waited = 0
        while True:
            problem = self._domain_resolution_problem(
                ssm, machine, region, new_domain, nameservers)
            if not problem or waited >= wait_propagation:
                break
            time.sleep(SSL_INSYNC_POLL_SECONDS)
            waited += SSL_INSYNC_POLL_SECONDS
        if problem:
            self._log("domain_switch", name=log_name, result="failed",
                      error_message=problem, record=instance)
            return instance._domain_switch_failed(_(
                "%(p)s No se cambió nada: la instancia sigue en %(old)s.",
                p=problem, old=old_domain or _("su dirección actual")),
                "not_resolving")

        # 2. vhost PARALELO para el dominio nuevo (el de la instancia sigue).
        nuevo = "pcm-%s-dominio-nuevo.conf" % slug
        script = (
            "set -e\n"
            "cat > /etc/nginx/sites-available/%(f)s <<'PCM_NGINX'\n"
            "%(vhost)s"
            "PCM_NGINX\n"
            "ln -sf /etc/nginx/sites-available/%(f)s "
            "/etc/nginx/sites-enabled/%(f)s\n"
            "if ! nginx -t; then rm -f /etc/nginx/sites-enabled/%(f)s "
            "/etc/nginx/sites-available/%(f)s; exit 1; fi\n"
            "systemctl reload nginx\n"
            'echo "PCM_VHOST_OK"' % {
                "f": nuevo,
                "vhost": self._instance_vhost(instance, new_domain)})
        output = ssm.run_script(
            machine.aws_instance_id, script, region=region,
            comment="pcm domain switch vhost: %s" % slug,
            timeout=120, check=False)
        if "PCM_VHOST_OK" not in (output.get("stdout") or ""):
            why = (output.get("stderr") or output.get("status") or "?")[:200]
            self._log("domain_switch", name=log_name, result="failed",
                      error_message=why, record=instance)
            return instance._domain_switch_failed(_(
                "No pude preparar nginx para %(d)s: %(w)s. No se cambió nada.",
                d=new_domain, w=why), "vhost_failed")

        # 3. Certificado del dominio nuevo + verificación https REAL.
        ok, detail = self._ssl_run_certbot(
            ssm, machine, region, instance, new_domain, use_staging)
        if ok:
            ok, detail = self._ssl_verify_https(
                new_domain, verify=not use_staging)
            fallo = "verify_failed"
        else:
            fallo = "certbot_failed"
        if not ok:
            self._domain_switch_rollback(ssm, machine, region, slug, nuevo,
                                         new_domain, old_domain)
            self._log("domain_switch", name=log_name, result="failed",
                      error_message=(detail or "")[:400], record=instance)
            return instance._domain_switch_failed(_(
                "No se pudo emitir/verificar HTTPS para %(d)s: %(w)s. Se "
                "desarmó lo nuevo: la instancia sigue en %(old)s con su HTTPS.",
                d=new_domain, w=detail, old=old_domain), fallo)

        # 4. Recién ahora se retira lo viejo: el vhost nuevo pasa a ser el de
        #    la instancia (con los bloques 443 que certbot le agregó).
        actual = "pcm-%s.conf" % slug
        script = (
            "set -e\n"
            "A=/etc/nginx/sites-available; E=/etc/nginx/sites-enabled\n"
            "mkdir -p /var/tmp/pcm\n"
            "cp -f $A/%(a)s /var/tmp/pcm/%(a)s.antes 2>/dev/null || true\n"
            "rm -f $E/%(a)s $E/%(n)s\n"
            "mv -f $A/%(n)s $A/%(a)s\n"
            "ln -sf $A/%(a)s $E/%(a)s\n"
            "if ! nginx -t; then\n"
            "  cp -f /var/tmp/pcm/%(a)s.antes $A/%(a)s; nginx -t && "
            "systemctl reload nginx; exit 1\n"
            "fi\n"
            "systemctl reload nginx\n"
            'echo "PCM_SWAP_OK"' % {"a": actual, "n": nuevo})
        output = ssm.run_script(
            machine.aws_instance_id, script, region=region,
            comment="pcm domain switch swap: %s" % slug,
            timeout=120, check=False)
        if "PCM_SWAP_OK" not in (output.get("stdout") or ""):
            why = (output.get("stderr") or output.get("status") or "?")[:200]
            self._log("domain_switch", name=log_name, result="failed",
                      error_message=why, record=instance)
            return instance._domain_switch_failed(_(
                "El certificado de %(d)s se emitió pero no pude activar su "
                "vhost: %(w)s. Se restauró el vhost anterior (%(old)s).",
                d=new_domain, w=why, old=old_domain), "swap_failed")
        if old_domain and old_domain != new_domain:
            self._ssl_delete_certificate(
                ssm, machine, region, old_domain, record=instance)
        expiry = self._ssl_read_cert_expiry(ssm, machine, region, new_domain)
        self._ssl_update_web_base_url(ssm, machine, region, instance,
                                      new_domain)

        # 5. PCM: dominio, estado HTTPS y links (todo sale de main_url +
        #    ssl_state). Sin disparar el flujo viejo de cambio de dominio.
        instance.with_context(pcm_skip_ssl_hooks=True).sudo().write({
            "main_url": new_domain,
            "ssl_state": "active",
            "ssl_cert_expiry": expiry,
            "ssl_last_error": False,
            "ssl_retry_count": 0,
            "ssl_expiry_warned": False,
            "domain_switch_target": False,
            "domain_switch_state": False,
            "domain_switch_error": False,
        })
        if self.main_url == old_domain and instance == self.primary_instance_id:
            self.sudo().main_url = new_domain
        instance.message_post(body=_(
            "✅ Dominio cambiado: %(old)s → https://%(new)s (certificado "
            "verificado, vence el %(exp)s). Se borró el certificado de "
            "%(old)s y web.base.url quedó en https://%(new)s.",
            old=old_domain or "—", new=new_domain, exp=expiry or "?"))
        self._log("domain_switch", name=log_name, result="success",
                  record=instance)
        return "active"

    def _domain_switch_rollback(self, ssm, machine, region, slug, nuevo,
                                new_domain, old_domain):
        """Desarma lo nuevo (vhost paralelo y su certificado si llegó a
        emitirse). Nunca toca el vhost ni el certificado de la instancia."""
        borrar_cert = ""
        if new_domain and new_domain != old_domain:
            borrar_cert = ("certbot delete --cert-name %s -n >/dev/null 2>&1 "
                           "|| true\n" % shlex.quote(new_domain))
        script = (
            "rm -f /etc/nginx/sites-enabled/%(n)s "
            "/etc/nginx/sites-available/%(n)s\n"
            "%(cert)s"
            "nginx -t && systemctl reload nginx\n"
            'echo "PCM_ROLLBACK_OK"' % {"n": nuevo, "cert": borrar_cert})
        return ssm.run_script(
            machine.aws_instance_id, script, region=region,
            comment="pcm domain switch rollback: %s" % slug,
            timeout=120, check=False)

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
        self._pcm_assert_env_not_imported("staging_origin")
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
        # R4-B5: el guard de staging se levantó — la copia usa el backup/
        # restore por instancia de B4 (conf/filestore del slug de origen).
        # Origen explícito (Bloque 5): resuelve instancia+BD ya, con fallback
        # solo si es inequívoco, y los persiste en el staging para el refresh.
        origin_instance, origin_database = self._resolve_staging_origin(
            self.env["primate.cloud.ec2.instance"].browse(
                params.get("origin_instance_id") or []),
            self.env["primate.cloud.database"].browse(
                params.get("origin_database_id") or []),
        )
        # R4-B5 (D-R4.6): destino = servidor EXISTENTE → el staging es una
        # INSTANCIA nueva en ese servidor (job_add_instance de R3 + copia B4),
        # no un servidor nuevo. Sin target → comportamiento actual (EC2 nueva).
        target_server = self.env["primate.cloud.environment"].browse(
            params.get("target_server_id") or []).exists()
        # Cadena completa del staging ANTES de crear nada (servidor nuevo =
        # EC2 facturable; mismo servidor = instancia + copia de la base).
        from .primate_cloud_instance import slugify
        self._exigir_cadena_de_instalacion(
            dict(params, db_mode="local_pg",
                 db_name=params.get("db_name") or self._staging_db_name_default()),
            version=self.odoo_version, edition=self.odoo_edition,
            slug=slugify(params.get("name")),
            bucket=params.get("transfer_bucket"), exigir_bucket=True)
        if target_server:
            return self._enqueue_staging_instance(
                target_server, params, origin_instance, origin_database)
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
        # Los secretos NO viajan en el job: va una referencia (2026-10-27).
        params = self.env["primate.cloud.job.secret"].stash(params)
        staging.with_delay(
            description=_("Crear staging: %s") % staging.name
        ).job_create_staging(params)
        return staging

    def _staging_db_name_default(self):
        """Nombre de base del staging por default, con el charset de DB_NAME_RE.

        Antes era ``"<nombre>_staging".lower()``: con un nombre con espacios
        ("Barraca Julia") daba una base inválida que fallaba recién al montar.
        """
        self.ensure_one()
        base = re.sub(r"[^a-z0-9_]+", "_", (self.name or "").lower()).strip("_")
        return "%s_staging" % (base or "base")

    def _exigir_cadena_de_instalacion(self, params, instance=None, version=None,
                                      edition=None, slug=None, bucket=None,
                                      exigir_bucket=False):
        """Valida TODO lo que la instalación va a exigir, ANTES de tocar AWS.

        Mismo principio que el semáforo de red («cadena completa verde antes de
        aprovisionar»), aplicado a la configuración: la versión y la edición de
        Odoo, el slug, el nombre de la base, la contraseña PG y el bucket de
        transferencia se validaban recién al usarse —con la EC2 ya creada y
        facturando (ensayo de Barraca Julia, 2026-09-19). Junta todos los
        problemas en un solo error, para corregirlos de una vez.

        Args:
            params (dict): parámetros de la cadena (db_name, db_mode, db_password…).
            instance (recordset, optional): la instancia a montar; por default,
                la primaria del entorno.
            version, edition, slug (str, optional): para cuando la instancia
                todavía no existe (staging): se validan los valores que va a tener.
            bucket (str, optional): bucket de transferencia (staging).
            exigir_bucket (bool): si el bucket es obligatorio en este camino.

        Raises:
            UserError: con la lista de lo que falta o es inválido.
        """
        self.ensure_one()
        errores = []
        if instance is None and version is None:
            instance = self.primary_instance_id
            if not instance:
                errores.append(_(
                    "el entorno no tiene su Odoo (instancia primaria): asignale un "
                    "proyecto (cliente) al entorno"))
        if instance:
            version = instance.odoo_version
            edition = instance.odoo_edition
            slug = instance.slug
        if instance is not None or version is not None:
            if not version:
                errores.append(_("falta la versión de Odoo"))
            if not edition:
                errores.append(_("falta la edición de Odoo (Community o Enterprise)"))
            if not SLUG_RE.match(slug or ""):
                errores.append(_("el nombre técnico (slug) %r no es válido: usá letras, "
                                 "números y guiones") % (slug or ""))
        db_name = (params.get("db_name") or "").strip()
        if not DB_NAME_RE.match(db_name):
            errores.append(_("el nombre de la base %r no es válido: solo letras, números, "
                             "guion y guion bajo, sin espacios") % db_name)
        pg_password = params.get("db_password")
        # Dentro del job llega como referencia al depósito de secretos: ya se
        # validó al encolar (en claro), acá no hay texto que mirar.
        if (params.get("db_mode", "local_pg") == "local_pg"
                and isinstance(pg_password, str) and pg_password
                and not PG_PASSWORD_RE.match(pg_password)):
            errores.append(_("la contraseña de PostgreSQL tiene caracteres no admitidos "
                             "(usá letras, números, guion y guion bajo)"))
        # RDS con una cuenta sin permisos de RDS: cortar ACÁ, antes de crear
        # la EC2 (pcm-prod-provision no tiene ningún permiso de RDS).
        if params.get("db_mode") == "rds" and self.account_id.provision_only:
            errores.append(_(
                "Amazon RDS no está disponible con la cuenta «%s» (solo "
                "aprovisionamiento): sus permisos (pcm-prod-provision) no "
                "incluyen RDS. Elegí PostgreSQL local") % self.account_id.name)
        if exigir_bucket:
            bucket = (bucket or "").strip()
            if not bucket:
                errores.append(_("falta el bucket S3 de transferencia"))
            elif not bucket.startswith("pcm-"):
                errores.append(_("el bucket de transferencia %r no es un bucket pcm-*: los "
                                 "permisos de S3 de PCM están acotados a esos buckets") % bucket)
        if errores:
            raise UserError(_(
                "No se puede continuar: la configuración está incompleta. No se creó "
                "nada en AWS.\n- %s") % "\n- ".join(errores))

    def _enqueue_staging_instance(self, target_server, params,
                                  origin_instance, origin_database):
        """Staging = una INSTANCIA nueva en un servidor EXISTENTE (R4-B5).

        ``self`` es el entorno ORIGEN; ``target_server`` es dónde se monta el
        staging (por default, el mismo servidor del origen — el caso barato
        que R3 habilitó). Crea la instancia staging con su slot de puertos,
        la vincula al origen (``origin_instance_id``, R1) y encola el montaje
        + copia. NO crea un entorno ni una EC2 nuevos.
        """
        self.ensure_one()
        if target_server.state != "active":
            raise UserError(_(
                "El servidor destino del staging debe estar activo."))
        if target_server._is_legacy_layout():
            raise UserError(_(
                "El servidor destino tiene layout legacy: no se le puede "
                "montar un staging como instancia (requiere multi-Odoo)."))
        from .primate_cloud_instance import slugify
        db_name = params.get("db_name") or self._staging_db_name_default()
        # El origen del staging es la INSTANCIA Odoo dueña de la base que se
        # copia (inequívoco por B4: origin_database.instance_id), NO
        # máquina→servidor→primaria (eso era el "[:1]" de Fase 8: stageando la
        # instancia B de un servidor multi, el staging diría que vino de A).
        # Fallback a la primaria solo en origen legacy (base sin instance_id).
        origin_odoo = origin_database.instance_id or self.primary_instance_id
        instance = target_server._create_instance_with_ports({
            "name": params["name"],
            "project_id": self.project_id.id,
            "slug": slugify(params["name"]),
            "env_type": "staging",
            "odoo_version": self.odoo_version,
            "odoo_edition": self.odoo_edition,
            "main_url": params.get("domain"),
            "origin_instance_id": origin_odoo.id or False,
        })
        job_params = dict(
            params, client_token="pcm-%s" % uuid.uuid4().hex,
            db_mode="local_pg", db_name=db_name,
            origin_environment_id=self.id,
            origin_instance_id=origin_instance.id if origin_instance else False,
            origin_database_id=origin_database.id if origin_database else False,
        )
        job_params = target_server._ensure_transient_db_password(job_params)
        job_params = self.env["primate.cloud.job.secret"].stash(job_params)
        target_server.with_delay(
            description=_("Crear staging (instancia): %s") % params["name"]
        ).job_create_staging_instance(instance.id, job_params)
        return instance

    def job_create_staging_instance(self, instance_id, params):
        """Job R4-B5: monta el staging como instancia + copia la base del origen.

        ``self`` es el SERVIDOR destino. Pasos: (1) ``job_add_instance`` de R3
        monta el Odoo (bootstrap+install, aislado por slug, sin tocar vecinos);
        (2) copia la base del origen por el pipeline backup/restore de B4
        (por instancia) sobre la BD del staging, con neutralización; (3) clona
        los repos del origen al ``addons_dir`` del slug.
        """
        self.ensure_one()
        params = self.env["primate.cloud.job.secret"].reveal(params)
        instance = self.env["primate.cloud.instance"].browse(instance_id).exists()
        if not instance:
            return False
        origin = self.env["primate.cloud.environment"].browse(
            params.get("origin_environment_id") or []).exists()
        # Regla dura 2026-10-22: un staging cuyo ORIGEN es importado correría
        # pg_dump en un productivo ajeno — prohibido sin excepción.
        origin._pcm_assert_env_not_imported("staging_origin")
        origin_machine = self.env["primate.cloud.ec2.instance"].browse(
            params.get("origin_instance_id") or []).exists()
        origin_machine._exigir_gestion_habilitada("staging_origin")
        # 1. Montar el Odoo (R3): bootstrap + install por slug, health de vecinos.
        if not self.job_add_instance(instance_id, params):
            return False   # job_add_instance ya auditó y dejó la instancia en error
        try:
            machine = self._active_machine()
            # Cada máquina con SU región (ver job_add_instance): el destino, la
            # de este servidor; el backup del origen, la de la máquina origen.
            region = (machine.region or params.get("region")
                      or self.account_id.default_region)
            # 2. Copiar la base del origen sobre la del staging (pipeline B4).
            origin_instance = self.env["primate.cloud.ec2.instance"].browse(
                params.get("origin_instance_id") or []).exists()
            origin_db = self.env["primate.cloud.database"].browse(
                params.get("origin_database_id") or []).exists()
            origin_region = origin_instance.region or region
            source = self._staging_source_backup(
                origin, origin_instance, origin_db, origin_region,
                use_last_backup=params.get("use_last_backup"),
                bucket=params.get("transfer_bucket"))
            ok = self.job_restore_backup({
                "backup_id": source.id, "db_name": params["db_name"],
                "instance_id": machine.id, "odoo_instance_id": instance.id,
                "pre_backup": False, "neutralize": True,
                "pcm_fresh_clone": True})  # la BD nació en ESTE flujo
            if not ok:
                raise UserError(_("Falló la copia de la base al staging."))
            # 3. Repos del origen → addons_dir del slug del staging.
            if origin:
                self._staging_clone_repos_to_instance(
                    origin, machine, instance, region)
        except Exception as error:  # noqa: BLE001 - se audita, no re-lanza
            instance.state = "error"
            self.message_post(body=_("Staging (instancia) fallido: %s") % error)
            self._log("staging_create", record=instance, result="failed",
                      name=_("Crear staging: %s") % instance.name,
                      error_message=str(error))
            return False
        instance.write({"database_id": self.env["primate.cloud.database"].search(
            [("environment_id", "=", self.id), ("name", "=", params["db_name"])],
            limit=1).id})
        self._log("staging_create", record=instance, result="success",
                  name=_("Crear staging: %s") % instance.name)
        self.message_post(body=_(
            "Staging montado como instancia %s.") % instance.display_name)
        return True

    def _staging_clone_repos_to_instance(self, origin, machine, instance, region):
        """Clona los repos del origen al addons_dir del slug del staging (B5)."""
        Repo = self.env["primate.cloud.repository"]
        ssm = machine._get_ssm_service()
        addons_dir = instance.addons_dir or CUSTOM_ADDONS_DIR
        for source in origin.repository_ids:
            if not source.github_url or not source.local_path:
                continue
            # Ruta en el slug del staging (no la del origen).
            name = source.local_path.rstrip("/").split("/")[-1]
            dest_path = "%s/%s" % (addons_dir, name)
            ref = source.current_commit or source.configured_branch or ""
            output = ssm.run_script(
                machine.aws_instance_id,
                self._build_clone_script(source.github_url, dest_path, ref),
                region=region, comment="pcm staging clone: %s" % source.name,
                timeout=900)
            ok = output.get("status") == "Success"
            Repo.create({
                "name": source.name, "environment_id": self.id,
                "instance_id": instance.id, "repo_type": source.repo_type,
                "github_url": source.github_url,
                "organization": source.organization,
                "configured_branch": source.configured_branch,
                "local_path": dest_path,
                "current_commit": source.current_commit if ok else False,
            })
        self.message_post(body=_("Repositorios clonados al staging."))

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
        params = self.env["primate.cloud.job.secret"].reveal(params)
        origin = self.origin_environment_id
        origin._pcm_assert_env_not_imported("staging_origin")
        account = self.account_id
        bus.provision_start(self.env, self, title=_("Creando staging: %s") % self.name)
        try:
            base = account._get_aws_service()
            region = params.get("region") or account.default_region
            domain = params.get("domain") or self.main_url or self.name
            staging_url = "https://%s" % domain

            bus.provision_step(self.env, self, _("Creando instancia EC2…"))
            instance = self._provision_ec2(base, account, params, region, domain)
            if params.get("elastic_ip", True):
                bus.provision_step(self.env, self,
                                   _("Asignando la IP fija (Elastic IP)…"))
                instance._pcm_ensure_elastic_ip()
            bus.provision_step(self.env, self, _("Configurando la base de datos…"))
            db_host = self._provision_database(base, account, instance, params, region)
            # DNS ANTES del install (D-R3.9 endurecida): la propagación corre
            # mientras el install trabaja; el paso SSL del final la espera.
            dns_change_id = None
            if params.get("create_dns"):
                bus.provision_step(self.env, self, _("Configurando DNS…"))
                dns_change_id = self._provision_dns(
                    base, account, instance, params, domain)
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
        # HTTPS verificado también para el staging (warning, nunca bloquea).
        if self._ssl_wanted(domain, params) and self.primary_instance_id:
            bus.provision_step(self.env, self,
                               _("Emitiendo HTTPS (propagación + certbot)…"))
            try:
                self._provision_ssl(self.primary_instance_id,
                                    change_id=dns_change_id)
            except Exception as error:  # noqa: BLE001 — el cron lo retoma
                self.primary_instance_id._set_ssl_error("pending", str(error))
                self.message_post(body=_(
                    "El paso HTTPS del staging falló (%s): queda en HTTP con "
                    "reintento automático.") % error)
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
        self._pcm_assert_env_not_imported("restore")
        params = params or {}
        origin = self.origin_environment_id
        if not origin:
            raise UserError(_("Este staging no tiene entorno origen registrado."))
        # R4-B5: guard levantado (la copia/restore es por instancia, B4).
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
                # Destino staging (descartable): exento de la triple
                # confirmación (regla 2026-10-22, excepción documentada).
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
        origin._pcm_bucket_valido(bucket, _("dump del staging"))
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
            "pcm_fresh_clone": True,
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
            check=False,   # chequea status != Success y levanta con su mensaje
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
    def _pcm_bucket_valido(self, bucket, uso):
        """Los permisos de S3 están acotados a los buckets de PCM DE ESTA
        CUENTA: ``pcm-<uso>-<cuenta>`` (con sufijo opcional), con condición
        aws:ResourceAccount. Se valida antes de empezar, con mensaje claro,
        en vez de un AccessDenied a mitad del job."""
        bucket = (bucket or "").strip()
        cuenta = (self.account_id.aws_account_id or "").strip()
        if not bucket.startswith("pcm-"):
            raise UserError(_(
                "El bucket «%(b)s» (%(uso)s) no empieza con «pcm-»: los "
                "permisos de S3 de PCM sólo alcanzan a los buckets "
                "pcm-<uso>-<cuenta>.", b=bucket, uso=uso))
        if cuenta and ("-%s" % cuenta) not in bucket:
            raise UserError(_(
                "El bucket «%(b)s» (%(uso)s) no lleva el número de cuenta "
                "(%(c)s): los permisos de S3 de PCM sólo alcanzan a los "
                "buckets pcm-<uso>-%(c)s de esta cuenta. Usá, por ejemplo, "
                "pcm-backups-%(c)s.", b=bucket, uso=uso, c=cuenta))
        return bucket

    @api.model
    def _ssm_long_timeout(self):
        """Tope (s) de los comandos SSM largos: restore y backup.

        Una base grande (Barraca Julia: 11 GB de zip, 12,8 GB de SQL y 262 mil
        archivos) no entra en la hora del default de AWS-RunShellScript. Este
        valor viaja como executionTimeout al agente y es también lo que PCM
        espera. Parámetro ``pcm.ssm_long_timeout_seconds``; default 4 h.
        """
        value = self.env["ir.config_parameter"].sudo().get_param(
            SSM_LONG_TIMEOUT_PARAM, str(SSM_LONG_TIMEOUT_DEFAULT))
        try:
            return max(int(value), 3600)
        except (TypeError, ValueError):
            return SSM_LONG_TIMEOUT_DEFAULT

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
            # Regla dura 2026-10-22: lo importado no se toca. El job lo
            # bloquearía igual (frontera), pero encolarlo cada hora sólo
            # llena la bitácora de bloqueos y la cola de jobs fallidos.
            if environment.pcm_is_imported():
                continue
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

    def action_restore_external_dump(self):
        """Botón: abre el asistente de restore desde un dump EXTERNO (F1).

        Hasta acá el asistente sólo se abría desde "Restaurar…" de un backup del
        catálogo, siempre con ``backup_id``: el camino del dump externo existía
        en el job pero no tenía ningún click que llegara a él. Este entorno va
        precargado como destino, sin backup, y con la base destino si el
        entorno tiene una sola. Toda la validación sigue en el asistente y en el
        job; acá no se decide nada.
        """
        self.ensure_one()
        self._pcm_assert_env_not_imported("restore")
        ctx = {"default_target_environment_id": self.id,
               "default_origin_type": "s3_uri"}
        bases = self.env["primate.cloud.database"].search(
            [("environment_id", "=", self.id)])
        if len(bases) == 1:
            ctx["default_target_db_name"] = bases.name
        return {
            "type": "ir.actions.act_window",
            "name": _("Restaurar desde un dump"),
            "res_model": "primate.cloud.backup.restore.wizard",
            "view_mode": "form",
            "target": "new",
            "context": ctx,
        }

    def action_run_backup(self):
        """Botón: encola un backup gestionado inmediato de este entorno."""
        self.ensure_one()
        self._pcm_assert_env_not_imported("backup_run")
        policy = self.backup_policy_id
        if not policy or not policy.managed_by_pcm or not policy.s3_bucket:
            raise UserError(_(
                "El entorno necesita una política de respaldo gestionada por "
                "PCM (con bucket S3 de destino)."
            ))
        # R4-B4: backup recableado (filestore/conf por instancia) → sin guard.
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
        self._pcm_assert_env_not_imported("backup_run")
        policy = self.backup_policy_id
        if not policy or not policy.managed_by_pcm or not policy.s3_bucket:
            raise UserError(_("La política del entorno no está gestionada por PCM."))
        # R4-B4: el guard de backup se levantó — el dump usa el conf/filestore
        # de la instancia Odoo de CADA base (multi-tenant correcto).
        account = self.account_id
        region = account.default_region
        bucket = policy.s3_bucket
        prefix = (policy.s3_prefix or "pcm-backups").strip("/")
        try:
            self._pcm_bucket_valido(bucket, _("backups"))
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
        # R4-B4: el conf (credenciales RDS) y el filestore salen de la
        # instancia ODOO de la base — en multi son los del slug, no legacy.
        # Si no se resuelve inequívoca, falla HONESTO (registro failed), nunca
        # cae a paths legacy (respaldaría datos ajenos).
        try:
            conf_path, filestore_base = self._backup_instance_paths(database)
        except UserError as error:
            record.write({"state": "failed",
                          "error_message": str(error)})
            return record
        try:
            output = instance._get_ssm_service().run_script(
                instance.aws_instance_id,
                self._build_backup_script(
                    database.name, bucket,
                    record.s3_key, record.s3_filestore_key,
                    rds_endpoint=(database.rds_endpoint
                                  if database.db_type == "rds" else None),
                    conf_path=conf_path, filestore_base=filestore_base,
                ),
                region=instance.region or region,
                comment="pcm backup: %s" % database.name,
                timeout=self._ssm_long_timeout(),
                # check=False: el except de abajo es SOLO para SSM inaccesible;
                # un comando que corre y falla lo detecta el status+PCM_BACKUP_OK.
                check=False,
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

    def _backup_odoo_instance(self, database):
        """La instancia Odoo cuyo runtime respalda esta base — o vacío (R4-B4).

        MISMO criterio que :meth:`_resolve_restore_instance`: NO adivina en
        multi. La base lleva ``instance_id`` real (mixin R1); si falta, solo
        es seguro caer a la primaria en un servidor legacy PURO (una única
        instancia no archivada). En multi, sin ``instance_id`` explícito,
        devuelve vacío → el backup falla claro en vez de respaldar el conf/
        filestore de OTRO cliente (exposición cruzada silenciosa por el
        fallback: exactamente el riesgo (b) que el guard cubría).
        """
        self.ensure_one()
        if database.instance_id:
            return database.instance_id
        vivas = self.instance_ids.filtered(lambda i: i.state != "archived")
        return vivas if len(vivas) == 1 else self.env["primate.cloud.instance"]

    def _backup_instance_paths(self, database):
        """(conf_path, filestore_base) de la instancia Odoo de la base.

        Levanta si no se puede resolver de forma inequívoca (multi sin
        ``instance_id``): nunca devuelve paths legacy por defecto en un
        servidor compartido.
        """
        odoo_inst = self._backup_odoo_instance(database)
        if not odoo_inst:
            raise UserError(_(
                "No se pudo determinar la instancia Odoo de la base «%(db)s» "
                "en un servidor con varias instancias: sin ella el backup "
                "tomaría el filestore de otro cliente. Asociá la base a su "
                "instancia.", db=database.name))
        data_dir = odoo_inst.data_dir or "/opt/odoo/.local/share/Odoo"
        return (odoo_inst.conf_path or ODOO_CONF_PATH,
                "%s/filestore" % data_dir.rstrip("/"))

    @api.model
    def _build_backup_script(self, db_name, bucket, dump_key, filestore_key,
                             conf_path, filestore_base, rds_endpoint=None):
        """Script SSM del backup gestionado (decisiones del Bloque 3).

        R4-B4: ``conf_path`` (credenciales RDS in-situ) y ``filestore_base``
        son REQUERIDOS y salen de LA instancia Odoo de la base — en multi el
        filestore vive en ``/opt/pcm/instances/<slug>/data/filestore``, no en
        el path legacy. Sin default a propósito (trampa del ``workers=0``): un
        llamador que se olvide revienta en la firma, no respalda el filestore
        ajeno en silencio.

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
        conf = shlex.quote(conf_path)
        if rds_endpoint:
            dump_lines = [
                "DB_USER=$(awk -F' *= *' '/^db_user/ {print $2; exit}' %s)"
                % conf,
                "DB_PASSWORD=$(awk -F' *= *' '/^db_password/ {print $2; exit}' %s)"
                % conf,
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
            'FS_DIR=%s/%s' % (shlex.quote(filestore_base), db),
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

        **F1 (origen externo).** ``origin`` permite restaurar un dump que NO
        produjo PCM: una key de S3 (subida por el operador a un bucket
        ``pcm-*``) o una ruta del servidor. El contrato es **exactamente uno**
        de ``backup_id`` u ``origin``, validado server-side por
        :meth:`_restore_origen_normalizado`. Sin ``origin`` el camino es EL
        MISMO de antes: los cuatro llamadores del staging no lo pasan.

        Args:
            params (dict): ``db_name``, ``instance_id``, ``pre_backup``
                (bool), y **uno** de ``backup_id`` u ``origin``. Opcionales:
                ``odoo_instance_id``, ``neutralize``, ``delete_source``
                (borrar la key de origen si el restore sale bien).
        """
        self.ensure_one()
        params = self.env["primate.cloud.job.secret"].reveal(params)
        origen = self._restore_origen_normalizado(params)
        backup = self.env["primate.cloud.backup"].browse(
            params.get("backup_id") or []
        ).exists()
        db_name = params["db_name"]
        # Regla dura 2026-10-22: restaurar PISANDO una base registrada exige
        # la triple confirmación (el job NO arranca sin el job_token). Quedan
        # exentos, documentado: (a) el clon fresco del staging (la base nació
        # en ESTE flujo, flag interno pcm_fresh_clone), (b) pisar un STAGING
        # (descartable por definición: el refresh diario no pide ceremonia).
        machine_target = self._active_machine()
        if machine_target and machine_target.management_state == \
                "adopted_readonly":
            raise UserError(_(
                "El entorno destino es IMPORTADO (solo lectura): un restore "
                "acá está prohibido sin excepción."))
        existing_db = self.env["primate.cloud.database"].search([
            ("environment_id", "=", self.id), ("name", "=", db_name)], limit=1)
        owner = self.env["primate.cloud.instance"].search(
            [("database_id", "=", existing_db.id)], limit=1) if existing_db \
            else self.env["primate.cloud.instance"]
        pisa = (existing_db and not params.get("pcm_fresh_clone")
                and (owner.env_type or self.env_type) != "staging")
        if pisa:
            destruction = params.get("destruction") or {}
            self.env["primate.cloud.destruction.ticket"].burn_job_token(
                existing_db, "restore_overwrite",
                destruction.get("ticket_id"), destruction.get("job_token"))
        instance = self.env["primate.cloud.ec2.instance"].browse(
            params["instance_id"]
        ).exists()
        # Con dump externo no hay registro de catálogo: el nombre de la
        # bitácora sale del origen, para que diga QUÉ se restauró y no "?".
        log_name = _("Restaurar %(origen)s → %(env)s",
                     origen=(origen["uri"] if origen else (backup.name or "?")),
                     env=self.name)

        # Contenedor mutable: `fail` se define antes de que el script corra,
        # pero el mensaje de error tiene que poder citar la ruta del log
        # completo cuando ya existe (F1). Sin esto, el operador recibe el tail
        # y no sabe dónde está el resto.
        log_remoto = {"ref": ""}

        def fail(message, result="failed"):
            detalle = "%s%s" % (message, log_remoto["ref"])
            self._log("backup_restore", name=log_name, result=result,
                      error_message=detalle)
            self.message_post(body=_("Restore fallido: %s") % detalle)
            return False

        # R4-B4: el guard del restore (el peor caso) se levantó. El script
        # ahora para/arranca la UNIT de la instancia destino, dropea/crea con
        # SU pg_user y restaura el filestore en SU data_dir — nunca toca el
        # Odoo de otro cliente del mismo servidor. La instancia destino se
        # resuelve explícita (params) o por la BD destino.

        # 1. Origen válido. El nombre de la base destino se valida SIEMPRE.
        #    El resto lo resuelve el resolver que corresponda (F1): el del
        #    catálogo verifica el registro y que los objetos SIGAN en S3; el
        #    externo mide el dump para la guarda de espacio. Los dos devuelven
        #    la misma tupla, así que acá no hay `if` de cuarenta líneas ni
        #    reindentación del bloque histórico.
        if not DB_NAME_RE.match(db_name or ""):
            return fail(_("Nombre de base destino inválido: '%s'.") % db_name)
        if origen:
            (bucket, region, origin_account, has_filestore, origen_mb,
             error) = self._restore_resolver_externo(origen)
        else:
            (bucket, region, origin_account, has_filestore, origen_mb,
             error) = self._restore_resolver_catalogo(backup, db_name)
        if error:
            return fail(error)

        # 2. Instancia destino corriendo (no se enciende infra para restaurar).
        if not instance or instance.environment_id != self:
            return fail(_("La instancia destino no pertenece al entorno."))
        if instance.instance_state != "running":
            return fail(_("La instancia destino no está corriendo (estado: "
                          "%s). No se enciende infra para restaurar.")
                        % instance.instance_state)

        # 2b. R4-B4: la instancia ODOO destino (qué unit parar, con qué
        #     pg_user y en qué filestore) debe ser INEQUÍVOCA — es la
        #     salvaguarda que reemplaza al guard: sin esto, en un servidor
        #     multi se pararía/dropearía el Odoo equivocado. Explícita
        #     (params) → la de la BD destino → única del servidor; si no,
        #     falla claro (el selector llega en R4-B6).
        odoo_instance = self._resolve_restore_instance(
            params.get("odoo_instance_id"), target_db=self.env[
                "primate.cloud.database"].search(
                [("environment_id", "=", self.id), ("name", "=", db_name)],
                limit=1))
        if not odoo_instance:
            return fail(_(
                "No se pudo determinar de forma inequívoca la instancia Odoo "
                "destino (el servidor hospeda varias): indicá la instancia."))

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

        # Neutralización: por default siempre que el destino NO sea
        # producción; el refresh de staging puede saltearla explícitamente
        # (spec §14.5). Sobre producción JAMÁS se neutraliza, pida lo que pida.
        # R4-B5: el "producción" que importa es el de la INSTANCIA destino,
        # NO el del servidor — un staging (env_type=staging) montado en un
        # servidor de producción DEBE neutralizarse (si mirara self.env_type
        # del servidor, se saltearía y el staging quedaría con mail/crons de
        # prod: el peor accidente de staging).
        #
        # 2026-09-19: va DENTRO del script del restore, con Odoo parado. Antes
        # era un segundo comando SSM después del arranque: Odoo corría sobre la
        # base sin neutralizar (crons y SMTP de producción), y si la
        # neutralización fallaba quedaba así (Barraca Julia).
        neutralize = params.get("neutralize")
        if neutralize is None:
            neutralize = True
        neutralize_sql = None
        if neutralize and odoo_instance.env_type != "production":
            neutralize_sql = self._render_neutralization_sql(
                "https://%s" % (odoo_instance.main_url or db_name))

        # 4-5. Script: valida espacio y versión PG ANTES del drop; después
        # stop Odoo → terminar conexiones → drop → restore → filestore →
        # neutralización (si corresponde) → start.
        # (todo sobre la unit/pg_user/filestore de la instancia ODOO destino).
        try:
            output = instance._get_ssm_service().run_script(
                instance.aws_instance_id,
                self._build_backup_restore_script(
                    db_name, backup, has_filestore, odoo_instance,
                    origin=origen, origin_size_mb=origen_mb,
                    neutralize_sql=neutralize_sql),
                region=instance.region or region,
                comment="pcm restore: %s" % db_name,
                timeout=self._ssm_long_timeout(),
                # check=False: el except de abajo es SOLO para SSM inaccesible;
                # un comando que corre y falla lo detecta el status+PCM_RESTORE_OK.
                check=False,
            )
        except Exception as error:  # noqa: BLE001 - SSM inaccesible
            return fail(_("SSM inaccesible: %s") % error)
        stdout = output.get("stdout") or ""
        stderr = output.get("stderr") or ""
        # F1: el script emite marcadores por stdout (el detalle va a un archivo
        # en el servidor). De acá salen la ruta del log, el formato detectado y
        # QUÉ se limpió — las tres cosas van a la bitácora.
        marcas = self._restore_marcadores(stdout)
        ref_log = (_(" Log completo en el servidor: %s.") % marcas["log_path"]
                   if marcas.get("log_path") else "")
        log_remoto["ref"] = ref_log
        if "PCM_NEUTRALIZE_FAILED" in stdout + stderr:
            # La base quedó restaurada pero sin neutralizar, y por eso la
            # unit quedó PARADA a propósito (pcm_start_unit).
            self.staging_neutralization_log = (stderr or stdout).strip()
            return fail(_(
                "Restore OK pero la NEUTRALIZACIÓN falló. Odoo quedó DETENIDO "
                "a propósito: no se arranca sobre una base con el correo y los "
                "crons de origen. Neutralizar a mano y recién ahí arrancar la "
                "unit. Detalle: %s") % (stderr or stdout).strip()[-2000:],
                result="partial")
        if output.get("status") != "Success" or "PCM_RESTORE_OK" not in stdout:
            # La cola del log (stdout) dice QUÉ falló; el stderr de SSM suele
            # ser sólo «failed to run commands: exit status 1».
            error_text = ("%s%s" % (stdout.strip()[-2000:],
                                    " · stderr: %s" % stderr.strip()[-500:]
                                    if stderr.strip() else "")
                          if stdout.strip() else stderr or output.get("status"))
            if "PCM_ERROR_PG_MISMATCH" in stdout + stderr:
                error_text = _(
                    "Versión de PostgreSQL incompatible: el dump viene de una "
                    "versión más nueva que la del destino (actualizá "
                    "PostgreSQL del destino o usá otra instancia). Detalle: %s"
                ) % error_text
            if "PCM_UNIT_LEFT_STOPPED" in stdout + stderr:
                error_text = _(
                    "%s (Odoo quedó DETENIDO: la base se recreó y no llegó a "
                    "neutralizarse.)") % error_text
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

        # 6. Neutralización: ya corrió DENTRO del script, antes del arranque.
        # Se exige su marcador: un script que dijo OK sin neutralizar (no
        # debería pasar) no se da por bueno en silencio.
        if neutralize_sql:
            if "PCM_NEUTRALIZED" not in stdout:
                return fail(_(
                    "El restore terminó sin confirmar la neutralización. NO "
                    "usar el entorno hasta verificarla a mano."),
                    result="partial")
            if "PCM_CHECKLIST_OK" not in stdout:
                return fail(_(
                    "El restore terminó sin el checklist de SELECTs en verde: "
                    "la base NO se rotula neutralizada. Verificarla a mano."),
                    result="partial")
            checks = marcas.get("checks") or {}
            resumen = ", ".join("%s=%s" % kv for kv in checks.items())
            self.staging_neutralization_log = _(
                "Neutralizada dentro del restore, antes de arrancar Odoo. "
                "Checklist en verde: %s") % resumen
            self.message_post(body=_(
                "Base neutralizada (antes de arrancar Odoo). Checklist de "
                "SELECTs en verde: %s") % resumen)

        # 7. Registro de la base destino (si no existía) + bitácora.
        if not target_db:
            self.env["primate.cloud.database"].create({
                "name": db_name, "account_id": self.account_id.id,
                "environment_id": self.id, "db_type": "local_pg",
                "ec2_instance_id": instance.id,
                "instance_id": odoo_instance.id,
            })
        # El filestore se informa desde lo que PASÓ. Con dump externo, los
        # marcadores del script (PCM_FILESTORE_OK/SKIP + la cantidad); la nota
        # de has_filestore es del camino del catálogo y con un dump externo
        # decía "sin filestore" aunque se hubieran restaurado 2.470 archivos.
        if origen:
            extra = ""
        else:
            extra = "" if has_filestore else _(" (sin filestore: el backup no "
                                               "tenía o el objeto ya no está)")

        # --- F1: limpieza, borrado del origen y retención de los pre-backups ---
        # La limpieza de temporales la hace el `trap` del script SIEMPRE (éxito
        # o fallo); acá sólo se ASIENTA lo que informó, porque el motivo de
        # todo esto es el espacio en disco del servidor y hay que poder
        # auditarlo después.
        notas = []
        if marcas.get("cleaned"):
            notas.append(_("Temporales borrados en el servidor: %s.")
                         % marcas["cleaned"])
        if marcas.get("format"):
            notas.append(_("Formato detectado: %s.") % marcas["format"])
        if origen and "filestore" in marcas:
            if marcas["filestore"]:
                notas.append(_("Filestore restaurado (%s archivos).")
                             % (marcas.get("filestore_files") or "?"))
            else:
                notas.append(_("Sin filestore en el origen: se restauró sólo la base."))
        if marcas.get("warning_role"):
            notas.append(_("ATENCIÓN: %s") % marcas["warning_role"])
        if origen:
            # El borrado de la key de origen va SÓLO con el restore exitoso: si
            # falló, la key se conserva para reintentar sin volver a subir
            # varios GB. `delete_source` viene del wizard (default: sí).
            if params.get("delete_source", True):
                borrado = self._restore_borrar_origen(
                    origen, origin_account, region)
                notas.append(_("Origen borrado: %s.") % borrado)
            else:
                notas.append(_("Origen conservado a pedido: %s.")
                             % origen["uri"])
        if params.get("pre_backup") and pre_record and bucket:
            # Los pre-backups van a S3 y hasta ahora quedaban sin regla de
            # expiración: se acumulaban. Retención CORTA propia (no la de la
            # política, que puede ser de meses).
            self._restore_asegurar_retencion_pre_backup(bucket, region)
        resumen = (" " + " ".join(notas)) if notas else ""

        self._log("backup_restore", name=log_name, result="success",
                  error_message=(resumen.strip() + ref_log).strip() or None)
        self.message_post(body=_(
            "%(origen)s restaurado en '%(db)s' (instancia "
            "%(instance)s)%(extra)s.%(resumen)s%(log)s",
            origen=(origen["uri"] if origen else backup.name), db=db_name,
            instance=instance.name, extra=extra, resumen=resumen, log=ref_log))
        return True

    def _resolve_restore_instance(self, odoo_instance_id=None, target_db=None):
        """Instancia Odoo destino del restore, INEQUÍVOCA o recordset vacío.

        Prioridad: explícita (params) → la de la BD destino (si existe) →
        única instancia no archivada del servidor. Si el servidor hospeda
        varias y no hay pista, devuelve vacío (el caller falla): es la
        salvaguarda que reemplaza al guard — jamás se para/dropea a ciegas.
        """
        self.ensure_one()
        Instance = self.env["primate.cloud.instance"]
        if odoo_instance_id:
            inst = Instance.browse(odoo_instance_id).exists()
            return inst if inst and inst.environment_id == self else Instance
        if target_db and target_db.instance_id:
            return target_db.instance_id
        vivas = self.instance_ids.filtered(lambda i: i.state != "archived")
        return vivas if len(vivas) == 1 else Instance

    # ------------------------------------------------------------------
    # F1: resolución del ORIGEN del restore
    # ------------------------------------------------------------------
    def _restore_resolver_catalogo(self, backup, db_name):
        """Resuelve el origen cuando viene del CATÁLOGO. No reindenta el job.

        Extraído del cuerpo de :meth:`job_restore_backup` para que la
        bifurcación catálogo/externo (F1) sea de dos líneas en vez de un
        ``if/else`` de cuarenta. **La lógica es la de siempre**, verbatim: el
        estado del registro, el fallback de bucket para registros previos al
        campo ``s3_bucket``, y que los objetos SIGAN en S3 (el lifecycle pudo
        haberlos borrado).

        Returns:
            tuple: ``(bucket, region, account, has_filestore, tamano_mb,
                error)``. El tamaño va en 0: en este camino la guarda de
                espacio la calcula el script con ``backup.size_mb``. Si
                ``error`` no es ``None`` es el mensaje para ``fail()`` y el
                resto viene vacío — se devuelve en vez de levantar para no
                cambiar el comportamiento del job, que registra el fallo en la
                bitácora y devuelve ``False``.
        """
        self.ensure_one()
        vacio = (False, False, self.env["primate.cloud.account"], False, 0.0)
        if not backup or backup.state != "completed":
            return vacio + (_("El backup no está disponible (estado: %s).")
                            % (backup.state if backup else "?"),)
        bucket = backup._resolve_bucket()
        if not bucket or not backup.s3_key:
            return vacio + (_(
                "No se pudo resolver el bucket del backup: el registro no lo "
                "tiene y la política del entorno de origen tampoco lo define "
                "(o falta la key S3)."),)
        account = backup.environment_id.account_id
        region = account.default_region
        try:
            s3 = aws_s3.AwsS3Service(account._get_aws_service())
            if not s3.head_object(bucket, backup.s3_key, region=region):
                return vacio + (_(
                    "El dump ya no está en S3 (¿expiró por lifecycle?): "
                    "s3://%(bucket)s/%(key)s",
                    bucket=bucket, key=backup.s3_key),)
            has_filestore = bool(backup.s3_filestore_key) and bool(
                s3.head_object(bucket, backup.s3_filestore_key, region=region))
        except Exception as error:  # noqa: BLE001 - se audita y aborta
            return vacio + (_("No se pudo verificar el dump en S3: %s")
                            % error,)
        return bucket, region, account, has_filestore, 0.0, None

    def _restore_resolver_externo(self, origen):
        """Resuelve el origen cuando es un dump EXTERNO (F1).

        Con dump externo no hay entorno de origen: la cuenta es la del entorno
        **destino**. El filestore, si viene, está DENTRO del zip, así que
        ``has_filestore`` es False acá (lo maneja el script por formato).

        Devuelve el tamaño en la misma tupla a propósito: medir el objeto ES
        la validación de que existe, así que separar ambas cosas obligaba a
        pegarle DOS veces a ``head_object`` por restore y dejaba la lectura del
        tamaño lejos de su chequeo (si una cambia, la otra queda desfasada).

        Returns:
            tuple: ``(bucket, region, account, has_filestore, tamano_mb,
                error)``.
        """
        self.ensure_one()
        account = self.account_id
        region = account.default_region
        try:
            tamano_mb = self._restore_origen_tamano_mb(origen, account, region)
        except UserError as error:
            return (False, False, account, False, 0.0, str(error))
        return (origen.get("bucket") or False, region, account, False,
                tamano_mb, None)

    @api.model
    def _restore_origen_normalizado(self, params):
        """Valida y normaliza el origen del restore. Devuelve ``dict`` o ``None``.

        Invariante del contrato (server-side, no se confía en la vista):
        **exactamente uno** de ``backup_id`` (copia del catálogo) u ``origin``
        (dump externo). Nunca los dos, nunca ninguno. Sin ``origin`` devuelve
        ``None`` y el job sigue por el camino histórico, que es el que usan los
        cuatro llamadores del staging.

        Raises:
            UserError: si vienen los dos, si no viene ninguno, o si el origen
                externo está mal formado.
        """
        origin = params.get("origin") or None
        tiene_backup = bool(params.get("backup_id"))
        if origin and tiene_backup:
            raise UserError(_(
                "Origen ambiguo: llegaron una copia del catálogo Y un origen "
                "externo. Tiene que ser exactamente uno."))
        if not origin and not tiene_backup:
            raise UserError(_(
                "Falta el origen del restore: una copia del catálogo o un "
                "dump externo (key de S3 o ruta en el servidor)."))
        if not origin:
            return None

        tipo = origin.get("type")
        if tipo not in ("s3_uri", "server_path"):
            raise UserError(_(
                "Tipo de origen no soportado: '%s'. Se admiten una key de S3 "
                "o una ruta en el servidor.") % (tipo or ""))
        uri = (origin.get("uri") or "").strip()
        if not uri:
            raise UserError(_("El origen externo no tiene ruta ni key."))
        fmt = origin.get("format") or "auto"
        if fmt not in ("auto", "custom", "plain", "odoo_zip"):
            raise UserError(_("Formato de dump no soportado: '%s'.") % fmt)

        if tipo == "s3_uri":
            if not uri.startswith("s3://"):
                raise UserError(_(
                    "La key de origen tiene que ser una URI de S3 "
                    "(s3://bucket/key). Recibido: %s") % uri)
            bucket, _sep, key = uri[len("s3://"):].partition("/")
            if not bucket or not key:
                raise UserError(_(
                    "URI de S3 incompleta: falta el bucket o la key (%s).")
                    % uri)
            # La IAM está acotada a arn:aws:s3:::pcm-* en las DOS identidades
            # (el user pcm-operator y el instance profile pcm-ssm-role): un
            # objeto fuera de esos buckets falla con AccessDenied recién en el
            # servidor, a mitad del job. Se corta acá, con mensaje accionable.
            if not bucket.startswith("pcm-"):
                raise UserError(_(
                    "El bucket '%(bucket)s' no empieza con 'pcm-'. Los "
                    "permisos de S3 están acotados a los buckets pcm-*, así "
                    "que ni PCM ni la instancia pueden leer ahí. Subí el dump "
                    "a un bucket pcm-* y reintentá.", bucket=bucket))
            # Y de ESTA cuenta (pcm-<uso>-<cuenta>): la IAM lo exige con
            # aws:ResourceAccount (2026-10-27).
            if len(self) == 1:
                self._pcm_bucket_valido(bucket, _("dump a restaurar"))
            return {"type": tipo, "uri": uri, "format": fmt,
                    "bucket": bucket, "key": key}

        # server_path: ruta absoluta y sin metacaracteres de shell. El tamaño
        # NO se puede resolver desde Odoo (el archivo vive en el servidor), así
        # que la guarda de espacio de esa rama la hace el script con `stat`.
        if not uri.startswith("/"):
            raise UserError(_(
                "La ruta en el servidor tiene que ser absoluta (empezar con "
                "'/'). Recibido: %s") % uri)
        if any(c in uri for c in "\n\r\t\"'`$;|&<>*?"):
            raise UserError(_(
                "La ruta en el servidor tiene caracteres no permitidos: %s")
                % uri)
        return {"type": tipo, "uri": uri, "format": fmt}

    def _restore_origen_tamano_mb(self, origen, account, region):
        """Tamaño del objeto de origen, en MB, para la guarda de espacio.

        Sólo resuelve el caso ``s3_uri``, con ``head_object`` (usa
        ``s3:GetObject``, que ya está en la política: cero IAM nuevo). Para
        ``server_path`` devuelve 0 y la guarda la hace el script con ``stat``,
        porque el archivo está en el servidor y Odoo no lo puede medir.

        Raises:
            UserError: si el objeto no existe o no se puede leer — es mejor
                fallar acá que a mitad del restore.
        """
        self.ensure_one()
        if origen["type"] != "s3_uri":
            return 0.0
        try:
            s3 = aws_s3.AwsS3Service(account._get_aws_service())
            meta = s3.head_object(origen["bucket"], origen["key"],
                                  region=region)
        except Exception as error:  # noqa: BLE001 - se normaliza a UserError
            raise UserError(_(
                "No se pudo leer el dump en S3 (%(uri)s): %(error)s",
                uri=origen["uri"], error=error))
        if not meta:
            raise UserError(_(
                "El dump no existe en S3: %s. Verificá que lo subiste y que "
                "la key está bien escrita.") % origen["uri"])
        # OJO: `head_object` del adaptador NO devuelve el dict crudo de
        # boto3: normaliza a {"key", "size", "last_modified"}. Leer
        # "ContentLength" acá daba SIEMPRE None → tamaño 0 → la guarda de
        # espacio quedaba desactivada en silencio (ver aws_s3.py:147).
        return (meta.get("size") or 0) / (1024.0 * 1024.0)

    @staticmethod
    def _restore_marcadores(stdout):
        """Extrae los marcadores ``PCM_*`` que emite el script del restore.

        Devuelve un dict con lo que haya: ``log_path``, ``format``,
        ``cleaned``, ``filestore``. Se usa para la bitácora y el chatter.
        """
        datos = {}
        for linea in (stdout or "").splitlines():
            linea = linea.strip()
            if linea.startswith("PCM_LOG_PATH="):
                datos["log_path"] = linea.split("=", 1)[1]
            elif linea.startswith("PCM_FORMAT="):
                datos["format"] = linea.split("=", 1)[1]
            elif linea.startswith("PCM_CLEANED "):
                datos["cleaned"] = linea[len("PCM_CLEANED "):]
            elif linea.startswith("PCM_FILESTORE_OK "):
                datos["filestore"] = linea[len("PCM_FILESTORE_OK "):]
            elif linea == "PCM_FILESTORE_SKIP":
                datos["filestore"] = ""
            elif linea.startswith("PCM_FILESTORE_FILES="):
                datos["filestore_files"] = linea.split("=", 1)[1].strip()
            elif linea.startswith("PCM_WARNING_ROLE"):
                datos["warning_role"] = linea[len("PCM_WARNING_ROLE"):].strip()
            elif linea.startswith("PCM_CHECK ") and "=" in linea:
                clave, valor = linea[len("PCM_CHECK "):].split("=", 1)
                datos.setdefault("checks", {})[clave.strip()] = valor.strip()
        return datos

    def _restore_borrar_origen(self, origen, account, region):
        """Borra la key de origen en S3 tras un restore EXITOSO.

        Sólo aplica a ``s3_uri``: una ruta del servidor la dejó el operador y
        no es un temporal nuestro. Ante un fallo NO se llama — la key tiene que
        seguir ahí para reintentar sin volver a subir varios GB.

        Returns:
            str: descripción de lo borrado, o el motivo por el que no se pudo
                (nunca levanta: el restore ya salió bien y no se va a marcar
                fallido por no poder limpiar).
        """
        self.ensure_one()
        if origen["type"] != "s3_uri":
            return _("no aplica (el origen es una ruta del servidor, la deja "
                     "el operador)")
        try:
            s3 = aws_s3.AwsS3Service(account._get_aws_service())
            s3.delete_object(origen["bucket"], origen["key"], region=region)
        except Exception as error:  # noqa: BLE001 - se informa, no se propaga
            return _("NO se pudo borrar %(uri)s: %(error)s",
                     uri=origen["uri"], error=error)
        return origen["uri"]

    def _restore_asegurar_retencion_pre_backup(self, bucket, region):
        """Regla de lifecycle propia para el prefijo ``pre-restore/``.

        Los pre-backups del restore van a S3 (no a disco) y hasta ahora
        quedaban SIN regla de expiración: se acumulaban copia tras copia. Se
        les aplica una retención CORTA propia —no la de la política, que puede
        ser de meses— configurable por ``pcm.pre_restore_retention_days``
        (default 7). Es plata en S3, no riesgo operativo, pero no hay motivo
        para pagarla.

        Nunca levanta: si la regla no se puede asegurar, el restore sigue.
        """
        self.ensure_one()
        dias = int(self.env["ir.config_parameter"].sudo().get_param(
            "pcm.pre_restore_retention_days", PRE_RESTORE_RETENTION_DAYS))
        if dias <= 0:
            return None
        try:
            s3 = aws_s3.AwsS3Service(self.account_id._get_aws_service())
            return s3.put_lifecycle_rule(
                bucket, "pre-restore/", dias, region=region)
        except Exception as error:  # noqa: BLE001 - se avisa, no bloquea
            _logger.warning(
                "No se pudo asegurar la retención de pre-restore/ en %s: %s",
                bucket, error)
            return None

    @api.model
    def _build_backup_restore_script(self, db_name, backup, has_filestore,
                                     odoo_instance, origin=None,
                                     origin_size_mb=0, neutralize_sql=None):
        """Script SSM del restore (orden aprobado en el diseño del Bloque 4).

        Valida TODO antes de dropear (espacio en disco, versión PostgreSQL);
        recién después: **stop Odoo → pg_terminate_backend → dropdb** →
        createdb → restore → filestore → start Odoo. ``PCM_DROP_STARTED``
        marca el punto de no retorno: si el script falla después, el job arma
        el mensaje de recuperación con la key del pre-backup.

        R4-B4: la unit (``service_name``), el dueño de la base (``pg_user``) y
        el filestore destino (``data_dir``) salen de ``odoo_instance`` — en
        multi se para/crea/restaura SOLO lo de esa instancia, jamás el Odoo
        vecino.

        **F1 (origen externo).** Sin ``origin`` el script es EL MISMO de antes,
        byte por byte: los cuatro llamadores del staging no pasan origen y no
        se les cambia el camino. Con ``origin`` el dump no viene del catálogo
        sino de una key de S3 o de una ruta del servidor, el formato se detecta
        por *magic* y se agregan el log a archivo y la limpieza garantizada.

        Args:
            db_name (str): base destino (el nombre FINAL, no el del origen).
            backup (recordset): registro del catálogo; se ignora con ``origin``.
            has_filestore (bool): si el backup del catálogo tiene filestore.
            odoo_instance (recordset): instancia Odoo destino (unit, pg_user,
                data_dir).
            origin (dict, optional): ``{"type": "s3_uri"|"server_path",
                "uri": str, "format": "auto"|"custom"|"plain"|"odoo_zip"}``.
            origin_size_mb (float): tamaño del objeto de origen, ya resuelto
                por el job (``head-object`` en S3 o ``stat`` en el servidor).
                Es la base del cálculo de espacio de la etapa 1.
            neutralize_sql (str, optional): SQL de neutralización ya
                renderizado. Si viene, se aplica DENTRO del script, con Odoo
                todavía parado, y el ``trap`` NO arranca la unit sobre una base
                recreada que no quedó neutralizada (ver
                ``_restore_lineas_neutralizacion``). Sin él (destino de
                producción) el script es el de siempre, byte por byte.

        Returns:
            str: el script, envuelto para correr bajo bash.
        """
        service = shlex.quote(odoo_instance.service_name or LEGACY_SERVICE)
        pg_user = shlex.quote(odoo_instance.pg_user or "odoo")
        data_dir = (odoo_instance.data_dir
                    or "/opt/odoo/.local/share/Odoo").rstrip("/")
        db = shlex.quote(db_name)
        if not origin:
            return self._build_restore_script_catalogo(
                db_name, backup, has_filestore, service, pg_user, data_dir, db,
                neutralize_sql=neutralize_sql)
        return self._build_restore_script_externo(
            db_name, origin, origin_size_mb, service, pg_user, data_dir, db,
            neutralize_sql=neutralize_sql)

    @staticmethod
    def _restore_lineas_neutralizacion(db, service, sql, say):
        """Piezas del restore que neutraliza ANTES de arrancar Odoo.

        El orden viejo —restore, arrancar la unit, y recién después un segundo
        comando SSM con la neutralización— dejaba a Odoo corriendo sobre la base
        restaurada SIN neutralizar: en el ensayo de Barraca Julia
        (2026-09-19) los crons vencidos corrieron al minuto de arrancar, con el
        SMTP real de producción activo, y si la neutralización fallaba, el
        servicio quedaba así indefinidamente.

        Ahora la neutralización es un paso del MISMO script, con la unit todavía
        parada, y el arranque pasa por ``pcm_start_unit``: si la base ya se
        dropeó y la neutralización no quedó aplicada —falló ella, o falló la
        carga antes de llegar—, la unit NO se arranca. Es la misma garantía del
        ``trap`` (cualquier salida pasa por ahí), con una condición: un Odoo
        caído se ve y se arregla; uno arriba con correo y crons de producción
        no se deshace.

        Si la salida es ANTES del drop, la base vieja sigue intacta y la unit
        arranca como siempre.

        Returns:
            dict: ``init`` (variables y la función de arranque), ``start`` (lo
            que reemplaza al ``systemctl start`` del trap), ``dropped`` (marca
            del punto de no retorno) y ``apply`` (el paso de neutralización).
        """
        # base64: el SQL viaja sin que ninguna comilla, `$` o línea suelta
        # interfiera con el heredoc de _wrap_bash ni con el shell.
        b64 = base64.b64encode(sql.encode("utf-8")).decode("ascii")
        return {
            "init": [
                "PCM_DROPPED=0",
                "PCM_NEUTRALIZED=0",
                "pcm_start_unit() { "
                'if [ "$PCM_DROPPED" = 1 ] && [ "$PCM_NEUTRALIZED" != 1 ]; then '
                '%s "PCM_UNIT_LEFT_STOPPED: la base se recreó y NO quedó '
                'neutralizada; no se arranca Odoo sobre ella"; '
                "else systemctl start %s || true; fi; }" % (say, service),
            ],
            "start": "pcm_start_unit;",
            "dropped": "PCM_DROPPED=1",
            "apply": [
                "if printf '%%s' %s | base64 -d | sudo -u postgres psql -q "
                "-v ON_ERROR_STOP=1 -d %s; then PCM_NEUTRALIZED=1; "
                '%s "PCM_NEUTRALIZED"; else %s "PCM_NEUTRALIZE_FAILED"; exit 1; fi'
                % (shlex.quote(b64), db, say, say),
            ] + PrimateCloudEnvironment._restore_lineas_checklist(
                db, say, PrimateCloudEnvironment._neutralized_url(sql)),
        }

    @staticmethod
    def _neutralized_url(sql):
        """La web.base.url que fija el SQL de neutralización ('' si no fija)."""
        match = re.search(
            r"SET value = '([^']*)' WHERE key = 'web\.base\.url'", sql or "")
        return match.group(1) if match else ""

    @staticmethod
    def _restore_lineas_checklist(db, say, expected_url):
        """Checklist de SELECTs del framework de migración, DESPUÉS de
        neutralizar y con Odoo todavía parado (2026-10-23).

        «Neutralizada» no se declara por haber corrido el SQL: se declara con
        los SELECTs en verde. Cada valor sale como ``PCM_CHECK clave=valor``
        (la bitácora lo muestra). Si algo da rojo —SMTP activo o con
        credenciales, crons activos, correo en cola, un website con dominio o
        una web.base.url que no es la esperada—, la neutralización cuenta como
        FALLIDA: PCM_NEUTRALIZED vuelve a 0 y la unit no arranca.

        Los conteos de módulos y adjuntos son informativos (el operador los
        compara contra el origen). Las tablas que dependen de módulos se miran
        con query_to_xml detrás de to_regclass: un SELECT directo sobre una
        tabla inexistente no compila aunque esté dentro de un CASE.
        """
        def opcional(tabla, consulta):
            return ("(case when to_regclass('public.%s') is null then 0 "
                    "else (xpath('/row/c/text()', query_to_xml('%s', false, "
                    "true, '')))[1]::text::int end)" % (tabla, consulta))

        sql = "\n".join([
            "select 'modules_installed=' || (select count(*) from "
            "ir_module_module where state = 'installed')",
            "union all select 'attachments_stored=' || (select count(*) from "
            "ir_attachment where store_fname is not null)",
            "union all select 'smtp_active=' || (select count(*) from "
            "ir_mail_server where active)",
            "union all select 'smtp_with_credentials=' || (select count(*) "
            "from ir_mail_server where coalesce(smtp_user, '') <> '' or "
            "coalesce(smtp_pass, '') <> '')",
            "union all select 'fetchmail_active=' || %s" % opcional(
                "fetchmail_server",
                "select count(*) as c from fetchmail_server where active"),
            "union all select 'crons_active=' || (select count(*) from "
            "ir_cron where active)",
            "union all select 'mail_pending=' || %s" % opcional(
                "mail_mail", "select count(*) as c from mail_mail where "
                "state in (''outgoing'', ''exception'')"),
            "union all select 'website_with_domain=' || %s" % opcional(
                "website", "select count(*) as c from website where "
                "domain is not null"),
            "union all select 'web_base_url=' || coalesce((select value from "
            "ir_config_parameter where key = 'web.base.url'), '')",
            ";",
        ])
        b64 = base64.b64encode(sql.encode("utf-8")).decode("ascii")
        rojos = ("smtp_active smtp_with_credentials fetchmail_active "
                 "crons_active mail_pending website_with_domain")
        return [
            "PCM_CHK=$(printf '%%s' %s | base64 -d | sudo -u postgres psql "
            "-tA -v ON_ERROR_STOP=1 -d %s) || { PCM_NEUTRALIZED=0; "
            '%s "PCM_NEUTRALIZE_FAILED checklist: los SELECTs no corrieron"; '
            "exit 1; }" % (shlex.quote(b64), db, say),
            'while IFS= read -r PCM_L; do [ -n "$PCM_L" ] && '
            '%s "PCM_CHECK $PCM_L"; done <<< "$PCM_CHK"' % say,
            "PCM_ROJO=$(printf '%%s\\n' \"$PCM_CHK\" | awk -F= -v r=%s "
            "'BEGIN { n = split(r, k, \" \"); for (i = 1; i <= n; i++) "
            "m[k[i]] = 1 } ($1 in m) && $2 != \"0\" { printf \"%%s \", $0 }')"
            % shlex.quote(rojos),
            'PCM_URL=$(printf \'%s\\n\' "$PCM_CHK" | sed -n '
            "'s/^web_base_url=//p')",
            'if [ "$PCM_URL" != %s ]; then PCM_ROJO="${PCM_ROJO}'
            'web_base_url=$PCM_URL "; fi' % shlex.quote(expected_url),
            'if [ -n "$PCM_ROJO" ]; then PCM_NEUTRALIZED=0; '
            '%s "PCM_NEUTRALIZE_FAILED checklist en rojo: $PCM_ROJO"; exit 1; fi'
            % say,
            '%s "PCM_CHECKLIST_OK"' % say,
        ]

    @api.model
    def _build_restore_script_catalogo(self, db_name, backup, has_filestore,
                                       service, pg_user, data_dir, db,
                                       neutralize_sql=None):
        """Camino histórico: el dump sale de un registro del catálogo.

        Se conserva TAL CUAL (incluidos el orden de las guardas y la forma del
        ``trap``) porque es el que usan los cuatro llamadores del staging, ya
        verificados en real. Cualquier mejora del camino nuevo que quiera
        aplicarse acá es un cambio aparte, con su propia prueba.

        Dos cambios INTENCIONALES (2026-09-19), que modifican este script:

        - ``pg_restore --no-owner --role``: sin esto las tablas quedaban a
          nombre del rol del ORIGEN. Con origen y destino en el MISMO servidor
          y usuarios PG distintos —el caso del staging, que es el default de
          «Crear staging»— el Odoo del destino no veía ninguna tabla y no podía
          ni cargar el registry. Es el mismo remedio que F1 aplicó a los dumps
          externos. Cuando los usuarios coinciden, es inocuo.
        - El chequeo de sanidad corre **como el usuario de la instancia
          destino** (``SET ROLE``), no como ``postgres``: un chequeo con otra
          identidad que la de la aplicación no prueba lo que dice probar (como
          se vio: pasó con las tablas de otro dueño).

        Excepción (2026-09-19): con ``neutralize_sql`` se agrega la
        neutralización antes del arranque (el staging es justamente quien más
        la necesita). Sin él, el script sigue igual byte por byte.
        """
        neu = (self._restore_lineas_neutralizacion(
            db, service, neutralize_sql, "echo") if neutralize_sql else None)
        # El dueño de las tablas del destino, en crudo (ver el docstring).
        rol = shlex.split(pg_user)[0] if pg_user else "odoo"
        set_role = shlex.quote('SET ROLE "%s"' % rol.replace('"', '""'))
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
            # Orden aprobado: 1) detener Odoo (SOLO la unit de esta instancia),
            # 2) terminar conexiones residuales, 3) recién ahí el drop.
            "systemctl stop %s" % service,
        ]
        if neu:
            lines += neu["init"]
        lines += [
            # Desde acá, CUALQUIER salida (éxito o fallo) re-arranca ESTA unit:
            # el servidor puede hospedar más Odoo y un restore fallido no puede
            # dejar el servicio abajo. El start vive en el trap EXIT.
            "trap '%s rm -rf \"$FNAME\" \"$FS_TAR\" "
            "\"$FS_TMP\" 2>/dev/null || true' EXIT"
            % (neu["start"] if neu else "systemctl start %s || true;" % service),
            'sudo -u postgres psql -tAc "SELECT pg_terminate_backend(pid) '
            "FROM pg_stat_activity WHERE datname = '%s' AND pid <> "
            'pg_backend_pid();" || true' % db_name,
            'echo "PCM_DROP_STARTED"',
        ]
        if neu:
            lines.append(neu["dropped"])
        lines += [
            "sudo -u postgres dropdb --if-exists %s" % db,
            "sudo -u postgres createdb -O %s %s" % (pg_user, db),
            'sudo -u postgres pg_restore --no-owner --role=%s -d %s "$FNAME" '
            "|| true" % (shlex.quote(rol), db),
            # pg_restore devuelve != 0 por avisos ignorables (owners, etc.):
            # la sanidad real es que la base restaurada sea un Odoo.
            'sudo -u postgres psql -d %s -c %s -tAc "SELECT count(*) FROM '
            'ir_module_module" >/dev/null' % (db, set_role),
        ]
        if has_filestore:
            filestore_key = shlex.quote(backup.s3_filestore_key)
            target_dir = "%s/filestore/%s" % (shlex.quote(data_dir), db)
            lines += [
                'aws s3 cp s3://%s/%s "$FS_TAR" --only-show-errors'
                % (bkt, filestore_key),
                'mkdir -p "$FS_TMP"',
                'tar -xzf "$FS_TAR" -C "$FS_TMP"',
                'mkdir -p "$(dirname %s)"' % target_dir,
                "rm -rf %s" % target_dir,
                'mv "$FS_TMP"/%s %s' % (origin_dir, target_dir),
                "chown -R odoo:odoo %s" % target_dir,
            ]
        if neu:
            # Con Odoo todavía parado: el trap sólo lo arranca si esto pasó.
            lines += neu["apply"]
        lines += [
            # El start NO va acá: lo hace el trap EXIT re-armado tras el stop,
            # para cubrir también todos los caminos de error.
            'echo "PCM_RESTORE_OK"',
        ]
        # Bajo bash (heredoc): usa pipefail/PIPESTATUS y arrays si hiciera falta;
        # SSM corre con dash, que no soporta pipefail.
        return self._wrap_bash("\n".join(lines))

    # ------------------------------------------------------------------
    # F1: restore desde un dump EXTERNO (no producido por PCM)
    # ------------------------------------------------------------------
    # CÁLCULO DEL ESPACIO NECESARIO, POR FORMATO (decidido 2026-09-19)
    #
    # - ``custom`` (pg_dump -Fc): tamaño del archivo **× 1.5**. Es la
    #   heurística histórica y se conserva: ``pg_restore`` lee el archivo
    #   comprimido, no lo expande en disco; el 1.5 cubre los temporales.
    # - ``plain`` (SQL): tamaño del archivo **× 1.5**. El archivo YA está
    #   descomprimido, así que su tamaño es el real; el 1.5 cubre los
    #   temporales de ``psql``.
    # - ``odoo_zip``: **NO se usa un múltiplo del tamaño del archivo.** El
    #   ``dump.sql`` de adentro comprime 5-10×, así que cualquier factor sobre
    #   el zip SUBESTIMA justo en las bases grandes. Se verifica en DOS
    #   etapas, porque el tamaño descomprimido no se conoce hasta tener el
    #   archivo en disco:
    #     * etapa 1, ANTES de bajarlo:  ``zip × 1.2``  (tamaño de head-object)
    #     * etapa 2, con el zip abajo:  ``total de unzip -l × 1.2``
    #   El pico real es ``zip + descomprimido``; en la etapa 2 el zip ya está
    #   contabilizado dentro del ``df``, así que la suma queda cubierta.
    #   La extracción del zip ocurre en esa misma etapa —ANTES de parar Odoo
    #   y dropear— para que un zip corrupto falle sin tocar la base.
    #
    # LIMPIEZA: no hay mecanismo nuevo — se extiende el ``trap`` que ya
    # garantizaba el re-arranque de la unit. Borra el descargado y el
    # directorio de extracción; NUNCA borra el archivo que el operador dejó en
    # el servidor (``server_path``), que no es un temporal nuestro.
    #
    # LOG: el detalle completo va a ``/var/tmp/pcm/logs/`` (ruta predecible) y
    # a stdout —lo que viaja a la Bitácora— sólo los marcadores y un tail
    # acotado. ``get_command_invocation`` trunca a 24.000 caracteres, así que
    # un restore verboso perdería justamente el final.
    @api.model
    def _build_restore_script_externo(self, db_name, origin, origin_size_mb,
                                      service, pg_user, data_dir, db,
                                      neutralize_sql=None):
        """Script del restore desde un dump externo (F1)."""
        neu = (self._restore_lineas_neutralizacion(
            db, service, neutralize_sql, "say") if neutralize_sql else None)
        # El dueño de la base destino, en crudo. Un dump externo suele venir
        # `--no-owner`: si se carga como `postgres`, TODAS las tablas quedan a
        # nombre de `postgres` y Odoo —que se conecta como `pg_user`, dueño de
        # la base pero no de sus tablas— no puede leerlas ("permission
        # denied"). Por eso la carga corre con el rol del dueño.
        rol = shlex.split(pg_user)[0] if pg_user else "odoo"
        set_role = shlex.quote('SET ROLE "%s"' % rol.replace('"', '""'))
        uri = origin.get("uri") or ""
        fmt = origin.get("format") or "auto"
        es_s3 = origin.get("type") == "s3_uri"
        q_uri = shlex.quote(uri)
        log_dir = RESTORE_LOG_DIR
        tail = RESTORE_TAIL_BYTES
        # Etapa 1 del espacio: lo único que se conoce antes de tener el archivo.
        factor = 1.2 if fmt in ("odoo_zip", "auto") else 1.5
        need_mb = int(max(origin_size_mb or 0, 1) * factor) + 1

        lines = [
            "set -euo pipefail",
            "command -v aws >/dev/null 2>&1 || { echo \"PCM_ERROR: aws CLI no está instalado en la instancia (el install de PCM lo trae vía snap; instalalo o re-aprovisioná)\" >&2; exit 1; }",
            # --- Log: todo el detalle al archivo, sólo marcadores a stdout ---
            "mkdir -p %s" % shlex.quote(log_dir),
            'LOG="%s/restore-%s-$(date -u +%%Y%%m%%d-%%H%%M%%S)-$$.log"'
            % (log_dir, db_name),
            "exec 3>&1",
            'say() { echo "$*" >&3; }',
            'say "PCM_LOG_PATH=$LOG"',
            'exec >>"$LOG" 2>&1',
            "set -x",
            # --- Temporales y limpieza garantizada (trap extendido) ---
            'WORK="/var/tmp/pcm/restore_$$"',
            'DL=""',  # sólo se setea si NOSOTROS descargamos el archivo
            "mkdir -p \"$WORK\"",
            'cleanup() { rc=$?; set +x; '
            'rm -rf "$WORK" ${DL:+"$DL"} 2>/dev/null || true; '
            'say "PCM_CLEANED work=$WORK descargado=${DL:-(ninguno)}"; '
            'tail -c %d "$LOG" >&3 2>/dev/null || true; exit $rc; }' % tail,
            "trap cleanup EXIT",
        ]

        # --- Origen: bajar de S3 (es nuestro temporal) o usar la ruta tal cual
        if es_s3:
            lines += [
                "NEED_MB=%d" % need_mb,
                "FREE_MB=$(df -Pm /var/tmp | awk 'NR==2 {print $4}')",
                'if [ "$FREE_MB" -lt "$NEED_MB" ]; then '
                'say "PCM_ERROR: espacio insuficiente en /var/tmp para bajar '
                'el dump (${FREE_MB} MB < ${NEED_MB} MB)"; exit 1; fi',
                'DL="$WORK/origen.bin"',
                'aws s3 cp %s "$DL" --only-show-errors' % q_uri,
                'FNAME="$DL"',
            ]
        else:
            lines += [
                # La ruta del operador NO es un temporal nuestro: se usa en el
                # lugar (sin copiarla: duplicaría varios GB) y jamás se borra.
                'FNAME=%s' % q_uri,
                'if [ ! -f "$FNAME" ]; then '
                'say "PCM_ERROR: no existe el archivo $FNAME en el servidor"; '
                "exit 1; fi",
            ]

        # --- Formato por magic (o forzado) ---
        if fmt == "auto":
            lines += [
                'MAGIC=$(head -c 5 "$FNAME" | tr -d "\\0")',
                "case \"$MAGIC\" in",
                "  PGDMP*) FMT=custom ;;",
                "  PK*)    FMT=odoo_zip ;;",
                "  *)      FMT=plain ;;",
                "esac",
            ]
        else:
            lines.append("FMT=%s" % shlex.quote(fmt))
        lines.append('say "PCM_FORMAT=$FMT"')

        # --- Etapa 2 del espacio + versión PG, ANTES de tocar el destino ---
        lines += [
            'if [ "$FMT" = "odoo_zip" ]; then',
            "  command -v unzip >/dev/null 2>&1 || { "
            'say "PCM_ERROR: falta unzip en la instancia"; exit 1; }',
            # rsync se usa DESPUÉS del dropdb (filestore): si falta, el
            # restore moriría con la base ya borrada. Se chequea acá.
            "  command -v rsync >/dev/null 2>&1 || { "
            'say "PCM_ERROR: falta rsync en la instancia"; exit 1; }',
            # unzip -l: la última línea trae el total de bytes descomprimidos.
            '  UNZ=$(unzip -l "$FNAME" | tail -1 | awk \'{print $1}\')',
            '  SQLB=$(unzip -l "$FNAME" | awk \'$4 == "dump.sql" {print $1}\')',
            '  FSB=$(( ${UNZ:-0} - ${SQLB:-0} ))',
            # Espacio en DOS medidas (2026-10-27, cierra el gate del BACKLOG):
            # (1) descomprimir: lo extraído +10 %, con el zip todavía en disco;
            # (2) el PICO del restore, con el zip ya borrado: mientras carga,
            #     lo extraído + la base (1,7 × dump.sql; Barraca Julia midió
            #     1,61); al copiar el filestore, filestore ×2 + base (el
            #     dump.sql ya se borró). +10 %. PCM monta todo en el volumen
            #     raíz: /var/tmp, PostgreSQL y el data_dir comparten disco.
            '  NEED2=$(( ${UNZ:-0} / 1048576 * 11 / 10 + 1 ))',
            '  P1=$(( ${UNZ:-0} + ${SQLB:-0} * 17 / 10 ))',
            '  P2=$(( FSB * 2 + ${SQLB:-0} * 17 / 10 ))',
            '  PICO=$(( (P1 > P2 ? P1 : P2) / 1048576 * 11 / 10 + 1 ))',
            '  ZIPMB=0',
            '  [ -n "$DL" ] && ZIPMB=$(( $(stat -c %s "$DL") / 1048576 ))',
            "  FREE2=$(df -Pm /var/tmp | awk 'NR==2 {print $4}')",
            '  say "PCM_SPACE libre=${FREE2}MB descomprimir=${NEED2}MB '
            'pico=${PICO}MB zip=${ZIPMB}MB"',
            '  if [ "$FREE2" -lt "$NEED2" ]; then '
            'say "PCM_ERROR: espacio insuficiente para descomprimir el zip '
            '(${FREE2} MB < ${NEED2} MB; descomprimido ${UNZ:-0} bytes)"; '
            "exit 1; fi",
            '  if [ $(( FREE2 + ZIPMB )) -lt "$PICO" ]; then '
            'say "PCM_ERROR: espacio insuficiente para el restore: el pico '
            '(descomprimido + base cargada + copia del filestore) necesita '
            '~${PICO} MB y hay $(( FREE2 + ZIPMB )) MB"; exit 1; fi',
            # Se EXTRAE y se VERIFICA acá, ANTES de parar Odoo y dropear:
            # un zip corrupto o que no es un backup de Odoo tiene que fallar
            # sin haber tocado la base. El pre-backup queda como red para los
            # fallos POSTERIORES (un psql que muere a mitad), no para un caso
            # evitable. El espacio ya se validó arriba con NEED2.
            '  unzip -q -o "$FNAME" -d "$WORK/x"',
            # Permisos normalizados: un zip armado sin atributos de archivo
            # (p. ej. con zipfile de Python) extrae todo en modo 000, y ni
            # postgres puede leer el dump ni Odoo el filestore (visto en el
            # ensayo de Barraca Julia del 2026-10-23: «Permission denied»).
            '  chmod -R u+rwX,go+rX "$WORK/x"',
            '  if [ ! -f "$WORK/x/dump.sql" ]; then '
            'say "PCM_ERROR: el zip no tiene dump.sql (no es un backup de '
            'Odoo)"; exit 1; fi',
            # Extraído y verificado: el zip bajado ya no hace falta (la key
            # de S3 sigue ahí hasta que el restore salga bien). Baja el pico
            # en el tamaño del zip (Barraca Julia: 10,5 GiB).
            '  if [ -n "$DL" ]; then rm -f "$DL"; say "PCM_ZIP_BORRADO"; fi',
            # Con el dump.sql ya extraído, la versión de origen sale de su
            # header sin volver a descomprimir.
            '  SRC_VER=$(head -n 200 "$WORK/x/dump.sql" | '
            "sed -n 's/.*Dumped from database version \\([0-9]*\\).*/\\1/p' "
            "| head -1)",
            'elif [ "$FMT" = "custom" ]; then',
            '  SRC_VER=$(sudo -u postgres pg_restore -l "$FNAME" | '
            "sed -n 's/.*dumped from database version \\([0-9]*\\).*/\\1/p' "
            "| head -1)",
            "else",
            '  SRC_VER=$(head -n 200 "$FNAME" | '
            "sed -n 's/.*Dumped from database version \\([0-9]*\\).*/\\1/p' "
            "| head -1)",
            "fi",
            'DST_VER=$(sudo -u postgres psql -tAc "show server_version" '
            "| cut -d. -f1)",
            'if [ -n "$SRC_VER" ] && [ "$SRC_VER" -gt "$DST_VER" ]; then '
            'say "PCM_ERROR_PG_MISMATCH origen=$SRC_VER destino=$DST_VER"; '
            "exit 1; fi",
            # La carga SQL corre con `SET ROLE <dueño>`. Si el dump trae sus
            # propios cambios de rol (algunos modos de pg_dump los emiten), el
            # SET ROLE inicial se pierde a mitad de camino y las tablas vuelven
            # a quedar a nombre de postgres. No se aborta —puede ser un dump
            # válido—, pero se avisa ANTES del stop y llega a la bitácora.
            'SQLF=""',
            '[ "$FMT" = "odoo_zip" ] && SQLF="$WORK/x/dump.sql"',
            '[ "$FMT" = "plain" ] && SQLF="$FNAME"',
            'if [ -n "$SQLF" ] && grep -qE "^(RESET ROLE|SET ROLE|SET SESSION '
            'AUTHORIZATION|RESET SESSION AUTHORIZATION)" "$SQLF"; then '
            'say "PCM_WARNING_ROLE el dump trae sentencias que cambian de rol: '
            'el SET ROLE inicial puede no alcanzar y parte de las tablas quedar '
            'a nombre de postgres. Revisar el dueño de las tablas."; fi',
        ]

        # --- Punto de no retorno: mismo orden aprobado que el camino histórico
        lines += [
            "systemctl stop %s" % service,
        ]
        if neu:
            lines += neu["init"]
        lines += [
            # Re-armado del trap: desde acá cualquier salida re-arranca la unit
            # (el servidor puede hospedar otros Odoo) y limpia igual. Con
            # neutralización, el arranque pasa por pcm_start_unit.
            'cleanup() { rc=$?; set +x; %s '
            'rm -rf "$WORK" ${DL:+"$DL"} 2>/dev/null || true; '
            'say "PCM_CLEANED work=$WORK descargado=${DL:-(ninguno)}"; '
            'tail -c %d "$LOG" >&3 2>/dev/null || true; exit $rc; }'
            % (neu["start"] if neu else "systemctl start %s || true;" % service,
               tail),
            'sudo -u postgres psql -tAc "SELECT pg_terminate_backend(pid) '
            "FROM pg_stat_activity WHERE datname = '%s' AND pid <> "
            'pg_backend_pid();" || true' % db_name,
            'say "PCM_DROP_STARTED"',
        ]
        if neu:
            lines.append(neu["dropped"])
        lines += [
            "sudo -u postgres dropdb --if-exists %s" % db,
            "sudo -u postgres createdb -O %s %s" % (pg_user, db),
            # --- Restore según formato ---
            'if [ "$FMT" = "custom" ]; then',
            # pg_restore devuelve != 0 por avisos ignorables (owners, etc.).
            # --no-owner --role: los objetos quedan a nombre del dueño de la
            # base, no de roles del origen que no existen acá (ni de postgres).
            '  sudo -u postgres pg_restore --no-owner --role=%s -d %s '
            '"$FNAME" || true' % (shlex.quote(rol), db),
            'elif [ "$FMT" = "odoo_zip" ]; then',
            # Ya extraído y verificado antes del stop: acá sólo se carga. El
            # SET ROLE va en la MISMA sesión que el -f (psql ejecuta -c y -f en
            # orden, en una sola conexión).
            '  sudo -u postgres psql -q -d %s -c %s -f "$WORK/x/dump.sql"'
            % (db, set_role),
            # Cargado: el dump.sql tampoco hace falta, y así no coincide con
            # la copia del filestore (Barraca Julia: 11,9 GiB menos de pico).
            '  rm -f "$WORK/x/dump.sql"',
            "else",
            '  sudo -u postgres psql -q -d %s -c %s -f "$FNAME"' % (db, set_role),
            "fi",
            # La sanidad real: que lo restaurado sea un Odoo, y visto CON LOS
            # OJOS DE LA APLICACIÓN (el usuario de la instancia), no con los de
            # postgres — que ve todo aunque los permisos estén mal.
            'sudo -u postgres psql -d %s -c %s -tAc "SELECT count(*) FROM '
            'ir_module_module" >/dev/null' % (db, set_role),
        ]

        # --- Filestore del zip: al nombre de la base DESTINO, no la del zip ---
        target_dir = "%s/filestore/%s" % (shlex.quote(data_dir), db)
        lines += [
            'if [ "$FMT" = "odoo_zip" ] && [ -d "$WORK/x/filestore" ]; then',
            '  mkdir -p %s' % target_dir,
            # rsync --delete: la base se acaba de recrear, el filestore que
            # corresponde es EXACTAMENTE el del dump. El destino lleva el
            # nombre FINAL de la base, no el que traía el zip.
            '  rsync -a --delete "$WORK/x/filestore/" %s/' % target_dir,
            "  chown -R odoo:odoo %s" % target_dir,
            '  say "PCM_FILESTORE_OK %s"' % target_dir,
            # Cuántos archivos quedaron: lo que el chatter cuenta, medido.
            '  say "PCM_FILESTORE_FILES=$(find %s -type f | wc -l | tr -d \' \')"'
            % target_dir,
            "else",
            '  say "PCM_FILESTORE_SKIP"',
            "fi",
        ]
        if neu:
            # Con Odoo todavía parado: el trap sólo lo arranca si esto pasó.
            lines += neu["apply"]
        lines += [
            # El start lo hace el trap EXIT re-armado, para cubrir los errores.
            'say "PCM_RESTORE_OK"',
        ]
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
        # R4-B1: la ruta del clone sale del addons_dir de la INSTANCIA (el
        # mixin resuelve la primaria si no vino explícita); las legacy llevan
        # el dir viejo en su campo → mismo path que antes.
        target = (self.env["primate.cloud.instance"].browse(
            vals.get("instance_id") or []) or self.primary_instance_id)
        addons_dir = (target.addons_dir or CUSTOM_ADDONS_DIR) if target \
            else CUSTOM_ADDONS_DIR
        repo = self.env["primate.cloud.repository"].create({
            "name": vals.get("name") or repo_name,
            "environment_id": self.id,
            "instance_id": target.id if target else False,
            "github_url": url,
            "organization": vals.get("organization") or False,
            "repo_type": vals.get("repo_type") or "custom_client",
            "configured_branch": vals.get("configured_branch") or False,
            "local_path": "%s/%s" % (addons_dir, repo_name),
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
        # R4-B2: el destino es LA instancia Odoo del repo (mixin R1); el
        # clone/ensure/restart usan SU addons_dir/conf/unit — por eso el
        # guard de addons se levantó en este mismo cambio.
        target = repo.instance_id or self.primary_instance_id
        # URL de clone: el token va por askpass (NUNCA en la URL persistida).
        token = repo._get_github_token()
        slug = repo._repo_slug()
        clone_url = "https://%sgithub.com/%s.git" % (
            "x-access-token@" if token else "", slug)
        cloned = instance.clone_addon(
            clone_url, repo.local_path, ref=repo.configured_branch or None,
            token=token, odoo_instance=target)
        if not cloned:
            repo._log("addon_add", result="failed", name=title,
                      error_message=_("El clone del repositorio falló."))
            repo.unlink()   # o-ninguna: ni registro ni clon
            return
        # Activar el addons_path (retroactivo, reusa B3) o reiniciar para cargar.
        ensure = instance._ensure_custom_addons_path(odoo_instance=target)
        if ensure in ("rolled_back", "error"):
            repo._log("addon_add", result="partial", name=title,
                      error_message=_(
                          "Clonado, pero el addons_path no quedó activo; el "
                          "addon NO está cargado (registrado como no verificado)."))
            repo.message_post(body=_("Addon clonado pero no cargado (addons_path)."))
            return   # registrado, no verificado — NO éxito mentiroso
        if ensure == "ready":
            # Ya estaba en el path → reiniciar SU unit y cargar.
            instance.restart_odoo(odoo_instance=target)
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
