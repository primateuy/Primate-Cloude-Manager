/** @odoo-module **/

import { Component } from "@odoo/owl";

/**
 * Selector PCM: un <select> nativo (accesible) estilado con los tokens del
 * tema. Estructura única; el aspecto por tema sale del CSS. Emite el nuevo
 * valor por `onChange`.
 */
export class PcmSelect extends Component {
    static template = "primate_cloud_manager.PcmSelect";
    static props = {
        value: { type: [String, Number], optional: true },
        options: { type: Array }, // [{ value, label }]
        onChange: { type: Function },
        disabled: { type: Boolean, optional: true },
    };

    onChange(ev) {
        this.props.onChange(ev.target.value);
    }

    // La comparación va en JS (las plantillas OWL no tienen String en su ctx).
    isSelected(value) {
        return String(value) === String(this.props.value ?? "");
    }
}
