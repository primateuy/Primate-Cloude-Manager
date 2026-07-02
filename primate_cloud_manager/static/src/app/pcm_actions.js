/** @odoo-module **/

/**
 * Normaliza el resultado de un método de acción de un modelo PCM y lo despacha
 * por los canales de la app (drawer de wizard / toast), sin abrir diálogos stock
 * ni cerrar la app. Se apoya en `env.pcm` (provisto por PcmApp).
 *
 * - act_window target=new (un wizard)  -> drawer lateral embebido.
 * - ir.actions.client display_notification -> toast (servicio notification).
 *   No se hace doAction para NO ejecutar su `next: act_window_close`, que en la
 *   app embebida cerraría el cliente.
 * - cualquier otra acción -> se delega al servicio de acción global.
 */
export function handlePcmActionResult(env, res) {
    if (!res) {
        return;
    }
    if (res.type === "ir.actions.act_window") {
        // Registro concreto (res_id) -> navegar por la pila de la app (detalle a
        // medida o form embebido), nunca un modal stock.
        if (res.res_id) {
            env.pcm.openRecord(res.res_model, res.res_id, res.name || "");
            return;
        }
        // Wizard (target new / sin registro) -> drawer lateral.
        env.pcm.openWizard(res.res_model, res.context || {}, res.name || "");
        return;
    }
    if (res.type === "ir.actions.client" && res.tag === "display_notification") {
        const p = res.params || {};
        env.pcm.notify(p.message || "", {
            type: p.type || "info",
            title: p.title,
            sticky: p.sticky || false,
        });
        return;
    }
    env.pcm.doAction(res);
}

/**
 * Llama a un método de acción de un modelo (opcionalmente tras confirmar) y
 * despacha su resultado. Devuelve true si se ejecutó, false si se canceló.
 */
export async function runPcmModelAction(env, orm, model, method, ids, { confirm } = {}) {
    if (confirm && !(await env.pcm.confirm(confirm))) {
        return false;
    }
    const res = await orm.call(model, method, [ids]);
    handlePcmActionResult(env, res);
    return true;
}
