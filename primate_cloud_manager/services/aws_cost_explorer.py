# -*- coding: utf-8 -*-
"""Adaptador Cost Explorer: consulta de costos (Fase 9).

Servicio Python **puro** (sin ORM, no escribe en BD). Cost Explorer es un
servicio **global** (el cliente ``ce`` se crea en ``us-east-1`` por convención,
sin importar la región de trabajo). Los datos tienen retardo (horas/hasta un
día) y la API se cobra por request; el modelo cachea en ``cost.entry``.
"""

# Región donde vive el endpoint de Cost Explorer (global; se fija us-east-1).
CE_REGION = "us-east-1"


class AwsCostExplorerService:
    """Consultas de costo agrupadas por tag estable y servicio."""

    def __init__(self, base):
        """Args:
            base (AwsBaseService): servicio base autenticado (inyección).
        """
        self._base = base

    def _client(self):
        return self._base.get_client("ce", region=CE_REGION)

    def get_cost_and_usage(self, start, end, granularity, group_by, metric="UnblendedCost"):
        """Costo agrupado en el rango ``[start, end)`` (fechas ``YYYY-MM-DD``).

        Args:
            start (str): fecha inicio inclusive (``YYYY-MM-DD``).
            end (str): fecha fin EXCLUSIVE (``YYYY-MM-DD``).
            granularity (str): ``DAILY`` / ``MONTHLY``.
            group_by (list[dict]): grupos boto3, máx 2. Ej.:
                ``[{"Type": "TAG", "Key": "primate:environment_id"},
                    {"Type": "DIMENSION", "Key": "SERVICE"}]``.
            metric (str): métrica de costo (``UnblendedCost`` por defecto).

        Returns:
            dict: ``{"total": float, "currency": str, "groups": [ {"keys":
            [str, ...], "amount": float, "currency": str}, ... ]}``. ``total``
            es la suma de TODOS los grupos del rango (para verificar cuadre).
        """
        client = self._client()
        groups = []
        total = 0.0
        currency = "USD"
        params = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": granularity,
            "Metrics": [metric],
            "GroupBy": group_by,
        }
        while True:
            response = client.get_cost_and_usage(**params)
            for period in response.get("ResultsByTime", []):
                for group in period.get("Groups", []):
                    amount_info = group.get("Metrics", {}).get(metric, {})
                    amount = float(amount_info.get("Amount") or 0.0)
                    currency = amount_info.get("Unit") or currency
                    groups.append({
                        "keys": list(group.get("Keys", [])),
                        "amount": amount,
                        "currency": currency,
                    })
                    total += amount
            token = response.get("NextPageToken")
            if not token:
                break
            params["NextPageToken"] = token
        return {"total": total, "currency": currency, "groups": groups}

    def get_cost_forecast(self, start, end, metric="UNBLENDED_COST"):
        """Proyección de costo del rango ``[start, end)`` (fin de mes).

        Returns:
            dict: ``{"amount": float, "currency": str}`` (0 si CE no puede
            proyectar, p. ej. sin histórico suficiente).
        """
        client = self._client()
        try:
            response = client.get_cost_forecast(
                TimePeriod={"Start": start, "End": end},
                Metric=metric, Granularity="MONTHLY",
            )
        except Exception:  # noqa: BLE001 - forecast puede fallar sin histórico
            return {"amount": 0.0, "currency": "USD"}
        total = response.get("Total", {})
        return {"amount": float(total.get("Amount") or 0.0),
                "currency": total.get("Unit") or "USD"}
