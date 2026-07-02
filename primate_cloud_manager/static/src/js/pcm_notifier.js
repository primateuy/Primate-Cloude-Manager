/** @odoo-module **/

import { Component, useState, xml } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

/**
 * Notificador en vivo de Primate Cloud Manager.
 *
 * Componente siempre montado (main_components) que escucha el bus del usuario y:
 *  - Muestra toasts en tiempo real de cualquier operación (éxito/error) y
 *    refresca la vista, sin que el usuario tenga que recargar a mano.
 *  - Durante el aprovisionamiento/staging muestra un overlay BLOQUEANTE con el
 *    progreso en vivo (paso a paso) que no se puede cerrar hasta que termina.
 *
 * El backend emite los eventos vía tools/bus.py (canal
 * "primate_cloud_manager.notification").
 */
export class PcmNotifier extends Component {
    static template = xml`
        <t t-if="state.active">
            <div class="o_pcm_overlay">
                <div class="o_pcm_card">
                    <div class="o_pcm_header">
                        <span t-if="!state.done" class="spinner-border o_pcm_spinner" role="status"/>
                        <i t-elif="state.ok" class="fa fa-check-circle o_pcm_ok"/>
                        <i t-else="" class="fa fa-times-circle o_pcm_err"/>
                        <span class="o_pcm_title" t-out="state.title"/>
                    </div>
                    <div class="o_pcm_steps">
                        <div t-foreach="state.steps" t-as="step" t-key="step_index"
                             class="o_pcm_step">
                            <i class="fa fa-angle-right me-1"/><t t-out="step"/>
                        </div>
                    </div>
                    <div t-if="!state.done" class="o_pcm_wait text-muted">
                        Por favor esperá: no cierres ni salgas de esta pantalla
                        hasta que termine de montarse el entorno.
                    </div>
                    <div t-if="state.done" class="o_pcm_footer">
                        <button class="btn btn-primary" t-on-click="close">Cerrar</button>
                    </div>
                </div>
            </div>
        </t>
    `;

    setup() {
        this.notification = useService("notification");
        this.action = useService("action");
        this.busService = useService("bus_service");
        this.state = useState({
            active: false, done: false, ok: true, title: "", steps: [],
        });
        this.busService.subscribe(
            "primate_cloud_manager.notification",
            (payload) => this._onMessage(payload)
        );
        this.busService.start();
    }

    _onMessage(p) {
        if (!p || !p.kind) {
            return;
        }
        if (p.kind === "toast") {
            this.notification.add(p.message, {
                title: p.title,
                type: p.type || "info",
                sticky: !!p.sticky,
            });
            if (p.reload) {
                this.action.doAction("soft_reload");
            }
        } else if (p.kind === "provision_start") {
            this.state.active = true;
            this.state.done = false;
            this.state.ok = true;
            this.state.title = p.title || "Aprovisionando entorno…";
            this.state.steps = [];
        } else if (p.kind === "provision_step") {
            if (this.state.active) {
                this.state.steps.push(p.step);
            }
        } else if (p.kind === "provision_done") {
            this.state.done = true;
            this.state.ok = !!p.ok;
            if (p.message) {
                this.state.steps.push(p.message);
            }
            this.notification.add(p.message, {
                title: this.state.title,
                type: p.ok ? "success" : "danger",
                sticky: !p.ok,
            });
            this.action.doAction("soft_reload");
        }
    }

    close() {
        this.state.active = false;
    }
}

registry.category("main_components").add("PcmNotifier", { Component: PcmNotifier });
