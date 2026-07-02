/** @odoo-module **/

import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

/**
 * Mapa canónico ÚNICO estado -> bucket de color (Sagui).
 *
 * Fuente de verdad compartida por el widget de badge (list/form/kanban) y el
 * dashboard. Cada valor de estado de cada modelo cae en uno de 4 buckets:
 *   ok · warn · neutral · error
 *
 * `GLOBAL` cubre los valores no ambiguos; `OVERRIDES` corrige los que significan
 * cosas distintas según el modelo (ej.: "running" es OK en EC2 pero "en curso"
 * en un deploy; "draft" es neutral en un entorno pero "sin validar" en una cuenta).
 */
const GLOBAL = {
    // ok
    active: "ok", running: "ok", connected: "ok", available: "ok",
    success: "ok", updated: "ok", installed: "ok", compliant: "ok",
    ok: "ok", completed: "ok", synced: "ok",
    // warn / en progreso
    provisioning: "warn", pending: "warn", creating: "warn", installing: "warn",
    deploying: "warn", syncing: "warn", outdated: "warn", stale: "warn",
    partial: "warn", stopping: "warn", "shutting-down": "warn",
    to_upgrade: "warn", to_install: "warn", to_remove: "warn",
    in_progress: "warn", unverifiable: "warn",
    // neutral / inactivo
    draft: "neutral", archived: "neutral", stopped: "neutral", terminated: "neutral",
    unknown: "neutral", reverted: "neutral", deleted: "neutral", disabled: "neutral",
    uninstalled: "neutral", expired: "neutral", no_policy: "neutral",
    // error / alerta
    error: "error", failed: "error", divergent: "error", missing: "error",
    not_found: "error", non_compliant: "error",
};

const OVERRIDES = {
    "primate.cloud.account.connection_state": { draft: "warn" },
    "primate.cloud.dns.record.state": { draft: "warn" },
    // "running" es un deploy en curso (warn), pero una EC2 corriendo está OK.
    "primate.cloud.ec2.instance.instance_state": { running: "ok" },
};

// "running" por defecto (deployment) = en progreso.
GLOBAL.running = "warn";

/**
 * Resuelve el bucket de color para (modelo, campo, valor).
 * @returns {"ok"|"warn"|"neutral"|"error"}
 */
export function statusBucket(model, field, value) {
    const override = OVERRIDES[`${model}.${field}`];
    if (override && value in override) {
        return override[value];
    }
    return GLOBAL[value] || "neutral";
}

/**
 * Widget de campo Selection que muestra un badge de estado consistente
 * (punto + texto) con el color canónico. Uso: widget="pcm_status_badge".
 */
export class PcmStatusBadge extends Component {
    static template = "primate_cloud_manager.StatusBadge";
    static props = { ...standardFieldProps };

    get value() {
        return this.props.record.data[this.props.name];
    }

    get model() {
        const rec = this.props.record;
        return rec.resModel || (rec.model && rec.model.resModel) || "";
    }

    get bucket() {
        return statusBucket(this.model, this.props.name, this.value);
    }

    get label() {
        const field = this.props.record.fields[this.props.name];
        const option = (field.selection || []).find(([v]) => v === this.value);
        return option ? option[1] : this.value || "";
    }
}

registry.category("fields").add("pcm_status_badge", {
    component: PcmStatusBadge,
    supportedTypes: ["selection"],
});

/**
 * Widget de campo Selection que muestra un chip de CATEGORÍA (no de salud):
 * env_type, record_type, repo_type, db_type. Estilo navy/teal tenue.
 * Uso: widget="pcm_category_chip".
 */
export class PcmCategoryChip extends Component {
    static template = "primate_cloud_manager.CategoryChip";
    static props = { ...standardFieldProps };

    get label() {
        const value = this.props.record.data[this.props.name];
        const field = this.props.record.fields[this.props.name];
        const option = (field.selection || []).find(([v]) => v === value);
        return option ? option[1] : value || "";
    }
}

registry.category("fields").add("pcm_category_chip", {
    component: PcmCategoryChip,
    supportedTypes: ["selection"],
});
