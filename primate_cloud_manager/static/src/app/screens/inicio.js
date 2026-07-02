/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmStatusBadge } from "../components/status_badge";

/**
 * Pantalla Inicio de la app PCM: saludo, fila de KPIs y entornos recientes.
 * Los datos vienen de `primate.cloud.dashboard.get_home_data` (solo lectura).
 */
export class Inicio extends Component {
    static template = "primate_cloud_manager.Inicio";
    static components = { PcmStatusBadge };
    static props = {
        userName: { type: String, optional: true },
        onOpenEnv: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({ loading: true, kpis: [], recent: [] });
        onWillStart(async () => {
            const data = await this.orm.call("primate.cloud.dashboard", "get_home_data", []);
            this.state.kpis = data.kpis || [];
            this.state.recent = data.recent_environments || [];
            this.state.loading = false;
        });
    }

    openEnv(id, name) {
        if (this.props.onOpenEnv) {
            this.props.onOpenEnv(id, name);
        }
    }
}
