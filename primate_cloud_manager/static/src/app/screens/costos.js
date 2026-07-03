/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

/**
 * Pantalla de Costos: lee `get_cost_overview` (que ya lee de `cost.entry`, nunca
 * llama a Cost Explorer en vivo). Muestra SIEMPRE "datos al <fecha>": el dato de
 * Cost Explorer tiene retardo, NO es tiempo real. Desglose por entorno, servicio
 * y cliente, con el bucket "Sin atribuir" incluido.
 */
export class Costos extends Component {
    static template = "primate_cloud_manager.Costos";
    static props = {};

    setup() {
        this.orm = useService("orm");
        this.state = useState({ loading: true, data: {} });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        this.state.data = await this.orm.call(
            "primate.cloud.dashboard", "get_cost_overview", [false]
        );
        this.state.loading = false;
    }

    get d() {
        return this.state.data;
    }
}
