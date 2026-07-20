/** @odoo-module **/

import { Component } from "@odoo/owl";

/**
 * Drawer lateral que hospeda un FORMULARIO OWL propio de PCM (no un wizard
 * nativo embebido). Reusa el chrome del drawer (.o_pcm_drawer*): mismo aspecto
 * y misma opacidad que el wizard nativo, cubierto por el mismo assert del smoke.
 *
 * El componente de formulario se renderiza dinámicamente (`props.component`) y
 * recibe `onClose`; cada form provee su propio footer y su propia validación
 * accionable. Así la app no se acopla a cada formulario concreto: el que abre
 * pasa la clase del componente.
 */
export class PcmFormDrawer extends Component {
    static template = "primate_cloud_manager.PcmFormDrawer";
    static props = {
        component: { type: Function },
        props: { type: Object, optional: true },
        title: { type: String, optional: true },
        onClose: { type: Function },
    };

    get childProps() {
        return { ...(this.props.props || {}), onClose: this.props.onClose };
    }
}
