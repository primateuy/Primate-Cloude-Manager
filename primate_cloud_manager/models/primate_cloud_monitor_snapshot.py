# -*- coding: utf-8 -*-
"""Snapshot de métricas de CloudWatch (EC2/RDS) — append-only (Fase 9).

Cada fila es una foto de las métricas de una instancia o una base en un
momento. El cron horario las acumula (historial + base de alertas) y el detalle
las refresca on-demand. Un valor ``None`` = métrica AUSENTE (p. ej. RAM/disco
sin agente CloudWatch), NO cero: la UI lo muestra como "requiere agente"/"sin
datos", nunca un gráfico en cero.
"""
import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


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
