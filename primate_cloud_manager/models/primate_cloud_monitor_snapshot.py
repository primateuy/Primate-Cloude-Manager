# -*- coding: utf-8 -*-
"""Snapshot de métricas de CloudWatch (EC2/RDS) — append-only (Fase 9).

Cada fila es una foto de las métricas de una instancia o una base en un
momento. El cron horario las acumula (historial + base de alertas) y el detalle
las refresca on-demand. Un valor ``None`` = métrica AUSENTE (p. ej. RAM/disco
sin agente CloudWatch), NO cero: la UI lo muestra como "requiere agente"/"sin
datos", nunca un gráfico en cero.
"""
import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Retención (Fase 9): las snapshots horarias son append-only y crecerían sin
# techo. Se conservan crudas los últimos RAW días; más viejas se downsamplean a
# UNA por (recurso, día); y hay un tope duro de MAX días.
SNAPSHOT_RAW_DAYS = 14
SNAPSHOT_MAX_DAYS = 365


class PrimateCloudMonitorSnapshot(models.Model):
    """Foto de métricas de una EC2 o RDS en un instante."""

    _name = "primate.cloud.monitor.snapshot"
    _description = "Snapshot de Monitoreo Cloud"
    _order = "snapshot_date desc, id desc"

    snapshot_date = fields.Datetime(
        string="Fecha", required=True, default=fields.Datetime.now, index=True,
    )
    ec2_instance_id = fields.Many2one(
        "primate.cloud.ec2.instance", string="Instancia EC2",
        ondelete="cascade", index=True,
    )
    database_id = fields.Many2one(
        "primate.cloud.database", string="Base de datos",
        ondelete="cascade", index=True,
    )
    environment_id = fields.Many2one(
        "primate.cloud.environment", string="Entorno",
        ondelete="cascade", index=True,
        help="Denormalizado para agrupar métricas por entorno sin recomputar.",
    )

    # --- Métricas EC2 (None = ausente, no cero) ---
    cpu = fields.Float(string="CPU %")
    network_in = fields.Float(string="Red entrante (bytes)")
    network_out = fields.Float(string="Red saliente (bytes)")
    status_check_failed = fields.Float(
        string="Status check fallido",
        help="≥1 = falló algún chequeo de estado de EC2 en la ventana (señal "
             "de error real).",
    )
    # --- Métricas RDS ---
    free_storage_gb = fields.Float(string="Almacenamiento libre (GB)")
    db_connections = fields.Float(string="Conexiones")
    read_iops = fields.Float(string="Read IOPS")
    write_iops = fields.Float(string="Write IOPS")

    @api.model
    def _cron_cleanup_snapshots(self):
        """Cron diario de retención (ver constantes arriba). SQL en bloque:

        1) Tope duro: borra todo lo más viejo que SNAPSHOT_MAX_DAYS.
        2) Downsample: en la franja [MAX, RAW) conserva una snapshot por
           (recurso, día) —la más nueva— y borra el resto.
        Las de los últimos RAW días quedan intactas (detalle horario reciente).
        """
        now = fields.Datetime.now()
        raw_cutoff = fields.Datetime.to_string(now - timedelta(days=SNAPSHOT_RAW_DAYS))
        max_cutoff = fields.Datetime.to_string(now - timedelta(days=SNAPSHOT_MAX_DAYS))
        # 1) Tope duro.
        self.env.cr.execute(
            "DELETE FROM primate_cloud_monitor_snapshot WHERE snapshot_date < %s",
            (max_cutoff,))
        capped = self.env.cr.rowcount
        # 2) Downsample a diario en la franja [max_cutoff, raw_cutoff).
        self.env.cr.execute("""
            DELETE FROM primate_cloud_monitor_snapshot s
            WHERE s.snapshot_date >= %s AND s.snapshot_date < %s
            AND s.id NOT IN (
                SELECT DISTINCT ON (
                    COALESCE(ec2_instance_id, 0), COALESCE(database_id, 0),
                    snapshot_date::date
                ) id
                FROM primate_cloud_monitor_snapshot
                WHERE snapshot_date >= %s AND snapshot_date < %s
                ORDER BY COALESCE(ec2_instance_id, 0), COALESCE(database_id, 0),
                         snapshot_date::date, snapshot_date DESC
            )
        """, (max_cutoff, raw_cutoff, max_cutoff, raw_cutoff))
        downsampled = self.env.cr.rowcount
        self.invalidate_model()
        if capped or downsampled:
            _logger.info("Retención de métricas: %s borradas (tope), %s "
                         "downsampleadas.", capped, downsampled)
        return {"capped": capped, "downsampled": downsampled}
