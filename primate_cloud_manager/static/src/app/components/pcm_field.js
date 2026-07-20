/** @odoo-module **/

import { Component } from "@odoo/owl";

/**
 * Campo de formulario PCM: etiqueta + control (slot) + ayuda/error inline.
 *
 * Estructura ÚNICA tokenizada: el aspecto por tema sale de las variables CSS
 * (--pcm-radius-control, --pcm-space-*, --pcm-fs-base...), nunca de markup
 * distinto. El control (input/textarea/PcmSelect) va en el slot default.
 *
 * El error se muestra en lugar de la ayuda (accionable, no "Missing required
 * fields"): quien lo pasa ya lo redactó orientado a la acción.
 */
export class PcmField extends Component {
    static template = "primate_cloud_manager.PcmField";
    static props = {
        label: { type: String, optional: true },
        help: { type: String, optional: true },
        error: { type: String, optional: true },
        required: { type: Boolean, optional: true },
        slots: { type: Object, optional: true },
    };
}
