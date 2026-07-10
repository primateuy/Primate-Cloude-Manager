# -*- coding: utf-8 -*-
"""Base de datos PostgreSQL (local o RDS) asociada a un entorno."""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# Mapeo de estados de RDS (DBInstanceStatus) a la selección del modelo.
# Lo no reconocido (backing-up, modifying, etc.) se marca como 'error' para
# que se note que no está en estado nominal.
RDS_STATE_MAP = {
    "available": "available",
    "creating": "creating",
    "stopped": "stopped",
}

# Versiones PostgreSQL soportadas en la selección. Otras quedan en False.
SUPPORTED_PG_VERSIONS = {"13", "14", "15", "16"}


class PrimateCloudDatabase(models.Model):
    """Base PostgreSQL local (en EC2) o administrada (RDS)."""

    _name = "primate.cloud.database"
    _description = "Base de Datos Cloud"
    _inherit = ["primate.cloud.instance.linked"]
    _order = "name"

    name = fields.Char(string="Nombre", required=True)
    account_id = fields.Many2one(
        "primate.cloud.account",
        string="Cuenta AWS",
        required=True,
        ondelete="cascade",
        index=True,
    )
    environment_id = fields.Many2one(
        "primate.cloud.environment",
        string="Entorno",
        ondelete="set null",
        index=True,
    )
    db_type = fields.Selection(
        [("local_pg", "PostgreSQL local"), ("rds", "Amazon RDS")],
        string="Modalidad",
        required=True,
        default="rds",
    )
    ec2_instance_id = fields.Many2one(
        "primate.cloud.ec2.instance",
        string="Instancia EC2",
        ondelete="set null",
        help="EC2 donde reside (si la modalidad es PostgreSQL local).",
    )
    rds_identifier = fields.Char(string="Identificador RDS", index=True)
    rds_endpoint = fields.Char(string="Endpoint RDS")
    pg_version = fields.Selection(
        [("13", "13"), ("14", "14"), ("15", "15"), ("16", "16")],
        string="Versión PostgreSQL",
    )
    rds_instance_class = fields.Char(string="Clase RDS")
    rds_storage_gb = fields.Integer(string="Almacenamiento (GB)")
    rds_multi_az = fields.Boolean(string="Multi-AZ")
    backup_retention_days = fields.Integer(string="Retención de backup (días)")
    last_backup_date = fields.Datetime(string="Último backup", readonly=True)
    storage_used_gb = fields.Float(string="Almacenamiento usado (GB)", readonly=True)
    active_connections = fields.Integer(string="Conexiones activas", readonly=True)
    state = fields.Selection(
        [
            ("available", "Disponible"),
            ("creating", "Creando"),
            ("stopped", "Detenida"),
            ("error", "Error"),
        ],
        string="Estado",
    )
    last_sync_date = fields.Datetime(string="Última sincronización", readonly=True)

    # --- Métricas RDS (cache de la última snapshot, Fase 9) ---
    last_cpu = fields.Float(string="CPU % (última)", readonly=True)
    last_free_storage_gb = fields.Float(string="Storage libre GB (última)", readonly=True)
    last_metric_date = fields.Datetime(string="Métricas al", readonly=True)

    _rds_identifier_uniq = models.Constraint(
        "UNIQUE(account_id, rds_identifier)",
        "Esa base RDS ya existe para la cuenta.",
    )

    def _take_metrics_snapshot(self, cw):
        """Snapshot de métricas RDS (solo bases RDS con identificador)."""
        self.ensure_one()
        if self.db_type != "rds" or not self.rds_identifier:
            return self.env["primate.cloud.monitor.snapshot"]
        from datetime import timedelta
        end = fields.Datetime.now()
        start = end - timedelta(hours=1)
        metrics = cw.get_rds_metrics(
            self.rds_identifier, self.account_id.default_region, start, end)
        vals = {"database_id": self.id,
                "environment_id": self.environment_id.id}
        for key in ("cpu", "free_storage_gb", "db_connections",
                    "read_iops", "write_iops"):
            if metrics.get(key) is not None:
                vals[key] = metrics[key]
        snapshot = self.env["primate.cloud.monitor.snapshot"].sudo().create(vals)
        cache = {"last_metric_date": end}
        if metrics.get("cpu") is not None:
            cache["last_cpu"] = metrics["cpu"]
        if metrics.get("free_storage_gb") is not None:
            # FreeStorageSpace viene en bytes; a GB para el cache legible.
            cache["last_free_storage_gb"] = metrics["free_storage_gb"] / (1024.0 ** 3)
        self.write(cache)
        return snapshot

    @api.constrains("ec2_instance_id", "environment_id")
    def _check_instance_environment_coherence(self):
        """La EC2 vinculada debe pertenecer al mismo entorno que la base.

        Solo aplica cuando ambos entornos están definidos: el sync de AWS puede
        traer instancias o bases todavía sin entorno asignado.
        """
        for database in self:
            instance_env = database.ec2_instance_id.environment_id
            if (
                database.environment_id
                and instance_env
                and instance_env != database.environment_id
            ):
                raise ValidationError(
                    _(
                        "La base '%(db)s' pertenece al entorno '%(db_env)s' pero la "
                        "instancia '%(instance)s' pertenece a '%(instance_env)s'.",
                        db=database.name,
                        db_env=database.environment_id.display_name,
                        instance=database.ec2_instance_id.display_name,
                        instance_env=instance_env.display_name,
                    )
                )

    def action_assign_instance(self, instance_id):
        """Vincula esta base a una instancia EC2 (para bases sueltas, desde el hub).

        Valida la coherencia de entorno antes de escribir; si la base no tiene
        entorno, adopta el de la instancia (así deja de estar suelta).

        Args:
            instance_id (int): id de la ``primate.cloud.ec2.instance`` destino.
        """
        self.ensure_one()
        instance = self.env["primate.cloud.ec2.instance"].browse(instance_id).exists()
        if not instance:
            raise UserError(_("La instancia EC2 indicada no existe."))
        vals = {"ec2_instance_id": instance.id}
        if not self.environment_id and instance.environment_id:
            vals["environment_id"] = instance.environment_id.id
        # El constraint _check_instance_environment_coherence valida el cruce.
        self.write(vals)
        return True

    def _sync_rds_from_aws(self, account, databases):
        """Crea/actualiza bases RDS PostgreSQL desde datos normalizados de AWS.

        Solo sincroniza motores PostgreSQL (el módulo gobierna Odoo). Hace upsert
        por (cuenta, identificador RDS).

        Args:
            account (recordset): cuenta AWS de origen.
            databases (list[dict]): salida de ``AwsRdsService.list_instances``.

        Returns:
            dict: ``{"created": int, "updated": int, "skipped": int}``.
        """
        now = fields.Datetime.now()
        created = updated = skipped = 0
        for data in databases:
            if not (data.get("engine") or "").startswith("postgres"):
                skipped += 1
                continue
            major = (data.get("engine_version") or "").split(".")[0]
            vals = {
                "name": data.get("name"),
                "db_type": "rds",
                "rds_instance_class": data.get("rds_instance_class"),
                "rds_storage_gb": data.get("rds_storage_gb") or 0,
                "rds_multi_az": data.get("rds_multi_az") or False,
                "rds_endpoint": data.get("endpoint") or False,
                "backup_retention_days": data.get("backup_retention_days") or 0,
                "pg_version": major if major in SUPPORTED_PG_VERSIONS else False,
                "state": RDS_STATE_MAP.get(data.get("status"), "error"),
                "last_sync_date": now,
            }
            existing = self.search(
                [
                    ("account_id", "=", account.id),
                    ("rds_identifier", "=", data["rds_identifier"]),
                ],
                limit=1,
            )
            if existing:
                existing.write(vals)
                updated += 1
            else:
                vals.update(
                    {
                        "account_id": account.id,
                        "rds_identifier": data["rds_identifier"],
                    }
                )
                self.create(vals)
                created += 1
        return {"created": created, "updated": updated, "skipped": skipped}
