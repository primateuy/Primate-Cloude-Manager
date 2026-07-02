/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmStatusBadge } from "../components/status_badge";

const STATE_FILTERS = [
    { key: "all", label: "Todos" },
    { key: "active", label: "Activos" },
    { key: "provisioning", label: "Aprovisionando" },
    { key: "error", label: "Error" },
    { key: "draft", label: "Borrador" },
    { key: "archived", label: "Archivado" },
];

const TYPE_FILTERS = [
    { key: "all", label: "Todos" },
    { key: "production", label: "Producción" },
    { key: "staging", label: "Staging" },
    { key: "testing", label: "Testing" },
    { key: "development", label: "Desarrollo" },
];

/**
 * Pantalla Entornos: cards con badge de estado, búsqueda y filtros (client-side
 * sobre la lista serializada). Click en una card abre el detalle.
 */
export class Entornos extends Component {
    static template = "primate_cloud_manager.Entornos";
    static components = { PcmStatusBadge };
    static props = {
        onOpenEnv: { type: Function, optional: true },
        onManage: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.stateFilters = STATE_FILTERS;
        this.typeFilters = TYPE_FILTERS;
        this.state = useState({
            loading: true, all: [], search: "", filterState: "all", filterType: "all",
        });
        onWillStart(async () => {
            this.state.all = await this.orm.call(
                "primate.cloud.dashboard", "get_environments", []
            );
            this.state.loading = false;
        });
    }

    get filtered() {
        let items = this.state.all;
        const query = this.state.search.trim().toLowerCase();
        if (query) {
            items = items.filter((e) =>
                `${e.name} ${e.project} ${e.main_url}`.toLowerCase().includes(query)
            );
        }
        if (this.state.filterState !== "all") {
            items = items.filter((e) => e.state === this.state.filterState);
        }
        if (this.state.filterType !== "all") {
            items = items.filter((e) => e.env_type === this.state.filterType);
        }
        return items;
    }

    onSearch(ev) {
        this.state.search = ev.target.value;
    }

    newEnv() {
        this.env.pcm.newRecord("primate.cloud.environment", {});
    }

    openEnv(id, name) {
        if (this.props.onOpenEnv) {
            this.props.onOpenEnv(id, name);
        }
    }

    manage(id, name) {
        if (this.props.onManage) {
            this.props.onManage(id, name);
        }
    }
}
