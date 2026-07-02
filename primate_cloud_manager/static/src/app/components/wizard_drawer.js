/** @odoo-module **/

import { Component, useSubEnv } from "@odoo/owl";
import { View } from "@web/views/view";

// Contador de instancias: el id del drawer es el ancla del portal del footer del
// form embebido (Layout portalea sus botones a `#<dialogId> .modal-footer`).
let SEQ = 0;

// Botones auxiliares del wizard que NO confirman ni deben cerrar el drawer
// (rellenan un campo y siguen). El resto de botones "object" con nombre que
// resuelven sin error se consideran confirmación y cierran el drawer.
const STAY_OPEN_BUTTONS = new Set(["action_resolve_ami"]);

/**
 * Drawer (panel lateral) que embebe un wizard REAL de Odoo dentro de la app PCM,
 * sin abrir un diálogo modal stock. Reutiliza el wizard completo (sus campos, su
 * default_get/precarga y su método de confirmación) — cero lógica nueva.
 *
 * Mecánica:
 *  - Provee a los descendientes el contrato de diálogo (`inDialog`, `dialogId`,
 *    `dialogData`) que espera el form controller: así el `<footer>` del wizard se
 *    portalea a `.modal-footer` de este drawer y sus botones funcionan.
 *  - `Cancelar` (special=cancel) cierra vía `dialogData.close` (nativo).
 *  - `Confirmar` (botón object) guarda + ejecuta el método del wizard. Para cerrar
 *    SOLO en éxito, se envuelve el servicio `action`: si `doActionButton` resuelve
 *    sin excepción, se cierra; si lanza (UserError de validación), NO se cierra y
 *    los datos quedan intactos.
 */
export class PcmWizardDrawer extends Component {
    static template = "primate_cloud_manager.WizardDrawer";
    static components = { View };
    static props = {
        model: { type: String },
        title: { type: String, optional: true },
        context: { type: Object, optional: true },
        onClose: { type: Function },
    };

    setup() {
        this.id = `pcm_wizard_drawer_${SEQ++}`;

        // Envolver `action` para cerrar el drawer solo cuando el botón del wizard
        // se ejecuta con éxito (sin excepción). Preserva los datos ante error.
        const realAction = this.env.services.action;
        const wrappedAction = {
            ...realAction,
            doActionButton: (params, ...rest) => {
                // Cierra solo si es un botón de confirmación (con nombre, que no
                // sea auxiliar) y resuelve sin excepción. `special` (Cancelar) lo
                // cierra useViewButtons vía dialogData.close; los auxiliares
                // (Resolver AMI) recargan el form y NO deben cerrar.
                const shouldClose =
                    params && params.name && !STAY_OPEN_BUTTONS.has(params.name);
                return realAction.doActionButton(params, ...rest).then((res) => {
                    if (shouldClose) {
                        this.props.onClose();
                    }
                    return res;
                });
            },
        };

        useSubEnv({
            inDialog: true,
            dialogId: this.id,
            dialogData: {
                id: this.id,
                close: () => this.props.onClose(),
                dismiss: () => this.props.onClose(),
                isActive: true,
                scrollToOrigin: () => {},
            },
            services: { ...this.env.services, action: wrappedAction },
        });

        this.viewProps = {
            type: "form",
            resModel: this.props.model,
            resId: false,
            context: this.props.context || {},
            display: { controlPanel: false },
        };
    }
}
