/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

/**
 * Pantalla Proyectos = el eje CLIENTE (R6). Cards por proyecto con nº de
 * instancias y costo del mes. El proyecto centinela "⚠ SIN CLIENTE" se muestra
 * distinto (bandeja de pendientes, no un cliente). El método y la fecha del
 * costo viven en el detalle (acá es el número de scan).
 * Datos de `primate.cloud.dashboard.get_projects`.
 */
export class Proyectos extends Component {
    static template = "primate_cloud_manager.Proyectos";
    static props = {
        onOpenProject: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({ loading: true, all: [], search: "" });
        onWillStart(async () => {
            this.state.all = await this.orm.call(
                "primate.cloud.dashboard", "get_projects", []
            );
            this.state.loading = false;
        });
    }

    get filtered() {
        const query = this.state.search.trim().toLowerCase();
        if (!query) {
            return this.state.all;
        }
        return this.state.all.filter((p) =>
            `${p.name} ${p.partner_name}`.toLowerCase().includes(query)
        );
    }

    onSearch(ev) {
        this.state.search = ev.target.value;
    }

    open(id, name) {
        if (this.props.onOpenProject) {
            this.props.onOpenProject(id, name);
        }
    }

    money(p) {
        return `${(p.cost_total || 0).toFixed(2)} ${p.cost_currency}`;
    }
}
