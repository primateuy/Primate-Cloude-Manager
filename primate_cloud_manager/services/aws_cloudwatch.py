# -*- coding: utf-8 -*-
"""Adaptador CloudWatch: métricas de EC2 y RDS (Fase 9).

Servicio Python **puro** (sin ORM). Usa ``get_metric_data``, que empaqueta
TODAS las métricas de una instancia en UN request (facturado por métrica-query)
→ un request por instancia por snapshot, costo marginal.

Distingue métrica AUSENTE (sin datos) de métrica en CERO: si CloudWatch no
devuelve puntos, el valor es ``None`` (la UI muestra "requiere agente"/"sin
datos", nunca un gráfico en cero). RAM/disco de EC2 solo existen con el agente
CloudWatch instalado; sin él, esas métricas vuelven ``None``.
"""

# Métricas EC2 disponibles SIN agente CloudWatch (las publica EC2 por defecto).
# RAM/disco NO están acá: requieren el agente (no instalado por PCM en v1).
EC2_METRICS = {
    "cpu": ("AWS/EC2", "CPUUtilization", "Average"),
    "network_in": ("AWS/EC2", "NetworkIn", "Average"),
    "network_out": ("AWS/EC2", "NetworkOut", "Average"),
    # Status checks: la señal de error REAL de EC2 (Maximum: 1 si falló algún
    # punto de la ventana).
    "status_check_failed": ("AWS/EC2", "StatusCheckFailed", "Maximum"),
}

# Métricas RDS: RDS las publica sin agente.
RDS_METRICS = {
    "cpu": ("AWS/RDS", "CPUUtilization", "Average"),
    "free_storage_gb": ("AWS/RDS", "FreeStorageSpace", "Average"),
    "db_connections": ("AWS/RDS", "DatabaseConnections", "Average"),
    "read_iops": ("AWS/RDS", "ReadIOPS", "Average"),
    "write_iops": ("AWS/RDS", "WriteIOPS", "Average"),
}


class AwsCloudWatchService:
    """Lectura de métricas de CloudWatch (EC2/RDS)."""

    def __init__(self, base):
        """Args:
            base (AwsBaseService): servicio base autenticado (inyección).
        """
        self._base = base

    def get_ec2_metrics(self, instance_id, region, start, end, period=300):
        """Métricas de una EC2 en la ventana ``[start, end]``.

        Returns:
            dict: ``{metric_key: valor|None}`` (None = métrica ausente, no cero).
        """
        return self._get_metrics(
            EC2_METRICS, {"Name": "InstanceId", "Value": instance_id},
            region, start, end, period)

    def get_rds_metrics(self, rds_identifier, region, start, end, period=300):
        """Métricas de una RDS en la ventana ``[start, end]``."""
        return self._get_metrics(
            RDS_METRICS, {"Name": "DBInstanceIdentifier", "Value": rds_identifier},
            region, start, end, period)

    def _get_metrics(self, metric_map, dimension, region, start, end, period):
        """Un solo ``get_metric_data`` con todas las métricas del mapa."""
        client = self._base.get_client("cloudwatch", region=region)
        queries = []
        order = list(metric_map.items())
        for i, (key, (namespace, name, stat)) in enumerate(order):
            queries.append({
                "Id": "m%d" % i,
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace, "MetricName": name,
                        "Dimensions": [dimension],
                    },
                    "Period": period, "Stat": stat,
                },
                "ReturnData": True,
            })
        response = client.get_metric_data(
            MetricDataQueries=queries, StartTime=start, EndTime=end)
        by_id = {r["Id"]: r.get("Values") or []
                 for r in response.get("MetricDataResults", [])}
        result = {}
        for i, (key, _spec) in enumerate(order):
            values = by_id.get("m%d" % i, [])
            # Sin datos → None (ausente, NO cero). Con datos, el más reciente.
            result[key] = values[0] if values else None
        return result
