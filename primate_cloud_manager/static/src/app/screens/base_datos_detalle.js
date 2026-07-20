/** @odoo-module **/

import { Component, onWillStart, onWillUpdateProps, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmStatusBadge } from "../components/status_badge";
import { DbForm } from "../forms/db_form";

/**
 * Detalle de una base de datos: datos generales, almacenamiento/RDS y
 * asociación (entorno, cuenta, servidor). Solo lectura, sin acciones.
 * Datos de `primate.cloud.dashboard.get_database_detail`.
 */
export class BaseDatosDetalle extends Component {
    static template = "primate_cloud_manager.BaseDatosDetalle";
    static components = { PcmStatusBadge };
    static props = {
        recordId: { type: Number },
        onOpenRecord: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({ loading: true, data: {} });
        onWillStart(() => this.load(this.props.recordId));
        onWillUpdateProps((next) => {
            if (next.recordId !== this.props.recordId) {
                this.load(next.recordId);
            }
        });
    }

    async load(id) {
        this.state.loading = true;
        this.state.data = await this.orm.call(
            "primate.cloud.dashboard", "get_database_detail", [id]
        );
        this.state.loading = false;
    }

    get d() {
        return this.state.data;
    }

    openRecord(model, id, name) {
        if (this.props.onOpenRecord && id) {
            this.props.onOpenRecord(model, id, name);
        }
    }

    // Editar la base en el form OWL (incluye el select de instancia para
    // asociarla; el constraint de coherencia entorno↔instancia valida server-side).
    editDb() {
        this.env.pcm.openForm(DbForm, {
            mode: "edit", recordId: this.props.recordId,
            onSaved: () => this.load(this.props.recordId),
        }, "Editar base de datos");
    }
}
