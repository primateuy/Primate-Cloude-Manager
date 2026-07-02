/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

/**
 * Panel de inicio de Primate Cloud Manager (client action OWL).
 *
 * Muestra KPIs (entornos activos, EC2 corriendo, cuentas conectadas, deploys de
 * hoy), los últimos despliegues y alertas. Cada tarjeta/ítem es clickeable y
 * abre la vista relacionada. Los datos vienen de
 * `primate.cloud.dashboard.get_dashboard_data`.
 */
export class PcmDashboard extends Component {
    static template = "primate_cloud_manager.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            loading: true, kpis: [], recent_deploys: [], alerts: [],
        });
        onWillStart(async () => {
            await this.load();
        });
    }

    async load() {
        this.state.loading = true;
        const data = await this.orm.call(
            "primate.cloud.dashboard", "get_dashboard_data", []
        );
        this.state.kpis = data.kpis || [];
        this.state.recent_deploys = data.recent_deploys || [];
        this.state.alerts = data.alerts || [];
        this.state.loading = false;
    }

    openList(model, domain, name) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: name,
            res_model: model,
            domain: domain || [],
            views: [[false, "list"], [false, "form"]],
            target: "current",
        });
    }

    openRecord(model, resId) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: model,
            res_id: resId,
            views: [[false, "form"]],
            target: "current",
        });
    }
}

registry.category("actions").add("primate_cloud_manager.dashboard", PcmDashboard);
