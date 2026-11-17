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

    def get_agent_metrics(self, instance_id, region, start, end, period=300):
        """RAM y disco del agente CloudWatch (namespace ``CWAgent``).

        ``mem_used_percent`` publica con la dimensión ``InstanceId`` sola
        (append_dimensions de la config de INCO). ``disk_used_percent`` suma
        ``device/fstype/path`` automáticamente, así que primero se DESCUBREN
        sus dimensiones reales con ``list_metrics`` y se consulta con ese set
        exacto (sin aggregation_dimensions, el query por InstanceId pelado
        devuelve vacío — causa del «requiere agente» eterno del panel).

        Returns:
            dict: ``{"ram": pct|None, "disk": pct|None}`` (None = sin datos).
        """
        client = self._base.get_client("cloudwatch", region=region)
        result = {"ram": None, "disk": None}
        queries = [{
            "Id": "ram",
            "MetricStat": {"Metric": {
                "Namespace": "CWAgent", "MetricName": "mem_used_percent",
                "Dimensions": [{"Name": "InstanceId", "Value": instance_id}]},
                "Period": period, "Stat": "Average"},
            "ReturnData": True,
        }]
        disk_dims = None
        try:
            listado = client.list_metrics(
                Namespace="CWAgent", MetricName="disk_used_percent",
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}])
            metricas = listado.get("Metrics") or []
            # Si hay varios discos, el de path "/" (la config de INCO solo
            # publica "/", pero un agente ajeno puede publicar más).
            for m in metricas:
                dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
                if dims.get("path", "/") == "/":
                    disk_dims = m["Dimensions"]
                    break
            if disk_dims is None and metricas:
                disk_dims = metricas[0]["Dimensions"]
        except Exception:  # noqa: BLE001 — sin ListMetrics, el disco degrada
            disk_dims = None
        if disk_dims:
            queries.append({
                "Id": "disk",
                "MetricStat": {"Metric": {
                    "Namespace": "CWAgent",
                    "MetricName": "disk_used_percent",
                    "Dimensions": disk_dims},
                    "Period": period, "Stat": "Average"},
                "ReturnData": True,
            })
        response = client.get_metric_data(
            MetricDataQueries=queries, StartTime=start, EndTime=end)
        for r in response.get("MetricDataResults", []):
            values = r.get("Values") or []
            if r["Id"] in result:
                result[r["Id"]] = values[0] if values else None
        return result

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
