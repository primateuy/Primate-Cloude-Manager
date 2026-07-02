/** @odoo-module **/

import { Component } from "@odoo/owl";
import { statusBucket } from "@primate_cloud_manager/js/pcm_status";

/**
 * Badge de estado para las pantallas OWL de la app.
 *
 * Reutiliza el MISMO mapa canónico (`statusBucket`) y las MISMAS clases de badge
 * (`.o_pcm_badge_*`, self-scoped bajo `.o_pcm`) que el widget de las vistas
 * nativas, así el estado se ve idéntico en todo el módulo y el color no depende
 * del acento.
 */
export class PcmStatusBadge extends Component {
    static template = "primate_cloud_manager.AppStatusBadge";
    static props = {
        value: { type: String, optional: true },
        model: { type: String, optional: true },
        field: { type: String, optional: true },
        label: { type: String, optional: true },
    };

    get bucket() {
        return statusBucket(this.props.model || "", this.props.field || "", this.props.value || "");
    }

    get text() {
        return this.props.label || this.props.value || "";
    }
}
