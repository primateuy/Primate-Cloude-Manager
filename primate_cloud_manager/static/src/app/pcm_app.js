/** @odoo-module **/

import { Component, onWillStart, useState, useSubEnv } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";
import { browser } from "@web/core/browser/browser";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { View } from "@web/views/view";
import { PcmWizardDrawer } from "./components/wizard_drawer";
import { PcmFormDrawer } from "./components/pcm_form_drawer";
import { Inicio } from "./screens/inicio";
import { Costos } from "./screens/costos";
import { Entornos } from "./screens/entornos";
import { EntornoDetalle } from "./screens/entorno_detalle";
import { ServidorDetalle } from "./screens/servidor_detalle";
import { InstanciaDetalle } from "./screens/instancia_detalle";
import { BaseDatosDetalle } from "./screens/base_datos_detalle";
import { RepositorioDetalle } from "./screens/repositorio_detalle";
import { DespliegueDetalle } from "./screens/despliegue_detalle";
import { DnsDetalle } from "./screens/dns_detalle";
import { CuentaDetalle } from "./screens/cuenta_detalle";
import { ProyectoDetalle } from "./screens/proyecto_detalle";
import { Proyectos } from "./screens/proyectos";

// Presets de acento (name -> accent / ink-sobre-claro / tint-de-fondo).
export const ACCENT_PRESETS = {
    teal:    { accent: "#48B3A6", ink: "#0F5E4E", tint: "#E3F3EF" },
    naranja: { accent: "#FF8A00", ink: "#9A5400", tint: "#FDECD9" },
    azul:    { accent: "#2A78D6", ink: "#0C447C", tint: "#E6F1FB" },
    violeta: { accent: "#7F77DD", ink: "#3C3489", tint: "#EEEDFE" },
    verde:   { accent: "#639922", ink: "#27500A", tint: "#EAF3DE" },
    // Preset de marca (diseño de Daryl): violeta-índigo. Se AGREGA; el default
    // de acento sigue siendo el que ya estaba (no se cambia acá).
    indigo:  { accent: "#6459F5", ink: "#2E259E", tint: "#ECEAFE" },
};

// Temas visuales: capa ORTOGONAL al acento. Solo forma/densidad/tipografía —
// NUNCA color (los valores solo referencian tokens de paleta FIJOS). Un solo
// markup + estos tokens = las 3 estéticas. Los tokens IGUALES en A/B/C viven de
// default en el SCSS; acá solo los que difieren. Regla del §6.4: cada literal
// del SCSS es un token; si uno queda fijo, ese aspecto no cambia entre temas.
export const THEME_TOKENS = {
    // A — Consola técnica (default): denso, radios chicos, comandos en terminal
    // oscuro, tabs subrayadas. fs-base 13.5px (ajuste de contraste aprobado).
    a: {
        "radius-card": "8px", "radius-control": "6px", "radius-pill": "6px",
        "card-shadow": "none", "fs-base": "13.5px",
        "space-screen": "16px", "space-card": "12px 14px", "space-gap": "10px",
        "space-row": "6px 10px", "space-kpi-row": "10px",
        "font-display": "var(--pcm-font-ui)",
        "fs-hero": "1.3rem", "fs-cardtitle": "1.02rem", "fs-title": "1.05rem", "fw-title": "600",
        "fs-kpi": "1.4rem", "fw-kpi": "600", "title-tracking": "normal",
        "eyebrow-spacing": "0.04em", "eyebrow-weight": "600", "eyebrow-rule": "0",
        "tab-radius": "0", "tab-bg-active": "transparent",
        "tab-indicator-w": "2px", "tab-transform": "none", "tab-pad": "6px 10px",
        "cmd-bg": "var(--pcm-navy)", "cmd-ink": "#D8E6E4",
        "cmd-border": "none", "cmd-rule": "0",
        "btn-pad": "6px 12px", "chip-pad": "3px 8px",
    },
    // B — Panel de operaciones (DEFAULT): afinado al diseño de marca de Daryl.
    // Radios 8px, sombras muy sutiles, badges pill (20px), KPIs y títulos grandes
    // semibold con tracking negativo (apretado tipográfico del diseño).
    b: {
        "radius-card": "8px", "radius-control": "8px", "radius-pill": "20px",
        "card-shadow": "0 1px 2px rgba(16,20,30,.04), 0 1px 3px rgba(16,20,30,.05)",
        "fs-base": "14px",
        "space-screen": "24px", "space-card": "18px 20px", "space-gap": "16px",
        "space-row": "10px 14px", "space-kpi-row": "16px",
        "font-display": "var(--pcm-font-ui)",
        "fs-hero": "1.6rem", "fs-cardtitle": "1.05rem", "fs-title": "1.375rem", "fw-title": "600",
        "fs-kpi": "1.625rem", "fw-kpi": "600", "title-tracking": "-0.02em",
        "eyebrow-spacing": "0.06em", "eyebrow-weight": "600", "eyebrow-rule": "0",
        "tab-radius": "20px", "tab-bg-active": "var(--pcm-accent-tint)",
        "tab-indicator-w": "0", "tab-transform": "none", "tab-pad": "8px 16px",
        "cmd-bg": "var(--pcm-surface)", "cmd-ink": "var(--pcm-ink-text)",
        "cmd-border": "1px solid var(--pcm-line)", "cmd-rule": "0",
        "btn-pad": "9px 16px", "chip-pad": "5px 12px",
    },
    // C — Editorial cálido: serif de display (stack del sistema, v1) en títulos y
    // números, eyebrows en mayúsculas con línea de acento, mucho aire.
    c: {
        "radius-card": "10px", "radius-control": "8px", "radius-pill": "4px",
        "card-shadow": "none", "fs-base": "15px",
        "space-screen": "28px", "space-card": "22px 26px", "space-gap": "18px",
        "space-row": "12px 14px", "space-kpi-row": "20px",
        "font-display": "Georgia, 'Times New Roman', serif",
        "fs-hero": "1.9rem", "fs-cardtitle": "1.2rem", "fs-title": "1.6rem", "fw-title": "500",
        "fs-kpi": "2.3rem", "fw-kpi": "500", "title-tracking": "normal",
        "eyebrow-spacing": "0.12em", "eyebrow-weight": "700", "eyebrow-rule": "2px",
        "tab-radius": "0", "tab-bg-active": "transparent",
        "tab-indicator-w": "2px", "tab-transform": "uppercase", "tab-pad": "6px 12px",
        "cmd-bg": "var(--pcm-navy)", "cmd-ink": "var(--pcm-teal-tint)",
        "cmd-border": "none", "cmd-rule": "2px",
        "btn-pad": "8px 14px", "chip-pad": "4px 10px",
    },
};

// Etiqueta legible por modelo (para breadcrumb y títulos).
export const MODEL_LABELS = {
    "primate.cloud.environment": "Entorno",
    "primate.cloud.ec2.instance": "Servidor",
    "primate.cloud.instance": "Instancia",
    "primate.cloud.database": "Base de datos",
    "primate.cloud.repository": "Repositorio",
    "primate.cloud.deployment": "Despliegue",
    "primate.cloud.dns.record": "Registro DNS",
    "primate.cloud.account": "Cuenta",
    "primate.cloud.operation.log": "Registro",
    "primate.cloud.project": "Proyecto",
};

// Modelo -> tipo de pantalla de detalle a medida (para drill-through y hash).
export const DETAIL_TYPES = {
    "primate.cloud.environment": "envDetail",
    "primate.cloud.ec2.instance": "serverDetail",
    "primate.cloud.instance": "instanceDetail",
    "primate.cloud.database": "dbDetail",
    "primate.cloud.repository": "repoDetail",
    "primate.cloud.deployment": "depDetail",
    "primate.cloud.dns.record": "dnsDetail",
    "primate.cloud.account": "accountDetail",
    "primate.cloud.project": "projectDetail",
};

// Tipo de detalle -> prefijo corto para el hash de deep-link.
const DETAIL_HASH = {
    envDetail: "env", serverDetail: "srv", instanceDetail: "inst",
    dbDetail: "db", repoDetail: "repo",
    depDetail: "dep", dnsDetail: "dns", accountDetail: "acc", projectDetail: "prj",
};
const HASH_DETAIL = Object.fromEntries(
    Object.entries(DETAIL_HASH).map(([k, v]) => [v, k])
);
const DETAIL_MODEL = Object.fromEntries(
    Object.entries(DETAIL_TYPES).map(([m, t]) => [t, m])
);

// Navegación del sidebar en grupos colapsables, con paridad con el árbol de
// menús nativo (Inventario / Operaciones / Configuración). Nada queda accesible
// solo por la barra de Odoo. Cada ítem define su RUTA (o un wizard a abrir).
const NAV_GROUPS = [
    { key: "principal", label: null, items: [
        { key: "inicio", label: "Inicio", icon: "fa-home", route: { type: "inicio" } },
    ] },
    { key: "inventario", label: "Inventario", items: [
        { key: "entornos", label: "Entornos", icon: "fa-cubes",
          route: { type: "entornos" } },
        { key: "servidores", label: "Instancias EC2", icon: "fa-server",
          route: { type: "nativeList", model: "primate.cloud.ec2.instance" } },
        { key: "ec2_new", label: "Crear instancia EC2", icon: "fa-plus",
          route: { type: "wizard", model: "primate.cloud.ec2.create.wizard" } },
        { key: "db", label: "Bases de datos", icon: "fa-database",
          route: { type: "nativeList", model: "primate.cloud.database" } },
        { key: "repos", label: "Repositorios", icon: "fa-code-fork",
          route: { type: "nativeList", model: "primate.cloud.repository" } },
        { key: "dns", label: "Registros DNS", icon: "fa-globe",
          route: { type: "nativeList", model: "primate.cloud.dns.record" } },
    ] },
    { key: "operaciones", label: "Operaciones", items: [
        { key: "deploys", label: "Despliegues", icon: "fa-rocket",
          route: { type: "nativeList", model: "primate.cloud.deployment" } },
        { key: "costos", label: "Costos", icon: "fa-dollar",
          route: { type: "costos" } },
        { key: "log", label: "Bitácora", icon: "fa-list",
          route: { type: "nativeList", model: "primate.cloud.operation.log" } },
    ] },
    { key: "configuracion", label: "Configuración", items: [
        { key: "cuentas", label: "Cuentas AWS", icon: "fa-key",
          route: { type: "nativeList", model: "primate.cloud.account" } },
        { key: "proyectos", label: "Proyectos", icon: "fa-folder-o",
          route: { type: "proyectos" } },
    ] },
];

export class PcmApp extends Component {
    static template = "primate_cloud_manager.App";
    static components = {
        Inicio, Costos, Entornos, Proyectos, EntornoDetalle, ServidorDetalle, InstanciaDetalle,
        BaseDatosDetalle, RepositorioDetalle, DespliegueDetalle, DnsDetalle,
        CuentaDetalle, ProyectoDetalle, View, PcmWizardDrawer, PcmFormDrawer,
    };
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.dialog = useService("dialog");
        this.userName = user.name;
        this.navGroups = NAV_GROUPS;

        // Servicios de la app expuestos a los descendientes (evita prop drilling).
        useSubEnv({
            pcm: {
                openWizard: (model, ctx, title) => this.openWizardDrawer(model, ctx, title),
                openForm: (component, props, title) => this.openFormDrawer(component, props, title),
                notify: (message, opts) => this.notification.add(message, opts),
                doAction: (action) => this.action.doAction(action),
                openRecord: (model, resId, title) => this.openRecord(model, resId, title),
                openList: (model) => this.setRoot({ type: "nativeList", model }),
                newRecord: (model, ctx) => this.openNewRecord(model, ctx),
                confirm: (message) => this.confirm(message),
            },
        });
        this.accentList = Object.keys(ACCENT_PRESETS).map((name) => ({
            name, color: ACCENT_PRESETS[name].accent,
        }));
        this.themeList = [
            { name: "a", label: "Consola" },
            { name: "b", label: "Panel" },
            { name: "c", label: "Editorial" },
        ];
        this.state = useState({
            accent: "indigo", accentCustom: "", theme: "b",
            // Pila de navegación. Cada entrada: { type, model?, resId?, title }.
            stack: [{ type: "inicio", title: "Inicio" }],
            // Wizard activo en el drawer lateral (o null). { model, context, title }.
            wizard: null,
            // Formulario OWL activo en el drawer (o null). { component, props, title }.
            form: null,
            // Grupos del sidebar colapsados (por clave). Por defecto expandidos.
            collapsed: {},
        });
        // Deep link: restaurar la pila desde el hash de la URL (refresco sin perder lugar).
        this.restoreFromHash();

        onWillStart(async () => {
            const recs = await this.orm.read(
                "res.users", [user.userId],
                ["pcm_accent", "pcm_accent_custom", "pcm_theme"]
            );
            if (recs.length) {
                this.state.accent = recs[0].pcm_accent || "indigo";
                this.state.accentCustom = recs[0].pcm_accent_custom || "";
                this.state.theme = recs[0].pcm_theme || "b";
            }
        });
    }

    // ------------------------------------------------------------- Acento
    get accentTokens() {
        if (this.state.accentCustom) {
            const c = this.state.accentCustom;
            return { accent: c, ink: c, tint: c + "22" };
        }
        return ACCENT_PRESETS[this.state.accent] || ACCENT_PRESETS.teal;
    }

    // Estilo del root: acento + tema en la MISMA cadena inline. Cambiar
    // cualquiera de los dos re-emite las vars → cambio instantáneo, sin reload.
    get themeStyle() {
        const tokens = THEME_TOKENS[this.state.theme] || THEME_TOKENS.a;
        return Object.entries(tokens)
            .map(([k, v]) => `--pcm-${k}:${v};`)
            .join("");
    }

    get rootStyle() {
        const t = this.accentTokens;
        return `--pcm-accent:${t.accent};--pcm-accent-ink:${t.ink};`
            + `--pcm-accent-tint:${t.tint};${this.themeStyle}`;
    }

    isAccentActive(name) {
        return this.state.accent === name && !this.state.accentCustom;
    }

    async setAccent(name) {
        this.state.accent = name;
        this.state.accentCustom = "";
        await this.orm.write("res.users", [user.userId], {
            pcm_accent: name, pcm_accent_custom: false,
        });
    }

    // ------------------------------------------------------------- Tema
    isThemeActive(name) {
        return this.state.theme === name;
    }

    async setTheme(name) {
        this.state.theme = name;
        await this.orm.write("res.users", [user.userId], { pcm_theme: name });
    }

    // ------------------------------------------------- Pila de navegación
    get current() {
        return this.state.stack[this.state.stack.length - 1];
    }

    get breadcrumb() {
        return this.state.stack;
    }

    // Completa el título de una ruta si no viene dado.
    withTitle(route) {
        if (route.title) {
            return route;
        }
        const label = MODEL_LABELS[route.model] || route.model || "";
        let title = "";
        if (route.type === "inicio") {
            title = "Inicio";
        } else if (route.type === "costos") {
            title = "Costos";
        } else if (route.type === "entornos") {
            title = "Entornos";
        } else if (route.type === "proyectos") {
            title = "Proyectos";
        } else if (route.type === "nativeList") {
            const item = this.navItems.find(
                (n) => n.route.type === "nativeList" && n.route.model === route.model
            );
            title = item ? item.label : label;
        } else if (route.type === "nativeForm") {
            title = route.resId ? label : `Nuevo ${label.toLowerCase()}`;
        } else if (DETAIL_MODEL[route.type]) {
            title = MODEL_LABELS[DETAIL_MODEL[route.type]] || "";
        }
        return { ...route, title };
    }

    setRoot(route) {
        this.state.stack = [this.withTitle(route)];
        this.syncHash();
    }

    push(route) {
        this.state.stack = [...this.state.stack, this.withTitle(route)];
        this.syncHash();
    }

    pop() {
        if (this.state.stack.length > 1) {
            this.state.stack = this.state.stack.slice(0, -1);
            this.syncHash();
        }
    }

    goTo(index) {
        if (index < this.state.stack.length - 1) {
            this.state.stack = this.state.stack.slice(0, index + 1);
            this.syncHash();
        }
    }

    // ------------------------------------------------- Navegación (sidebar/pantallas)
    get navItems() {
        return this.navGroups.flatMap((g) => g.items);
    }

    onNav(item) {
        if (item.route.type === "wizard") {
            this.openWizardDrawer(item.route.model, {}, item.label);
            return;
        }
        this.setRoot({ ...item.route });
    }

    toggleGroup(key) {
        this.state.collapsed[key] = !this.state.collapsed[key];
    }

    isGroupCollapsed(key) {
        return !!this.state.collapsed[key];
    }

    isNavActive(item) {
        if (item.route.type === "wizard") {
            return false;
        }
        const root = this.state.stack[0];
        if (item.route.type !== root.type) {
            return false;
        }
        if (item.route.model) {
            return item.route.model === root.model;
        }
        return true;
    }

    // Abrir el detalle de un entorno (pushea sobre la pila).
    openEnvDetail(id, name) {
        this.push({
            type: "envDetail", model: "primate.cloud.environment",
            resId: id, title: name || "Entorno",
        });
    }

    // Gestionar un entorno con la vista nativa completa (form embebido).
    manageEnv(id, name) {
        this.push({
            type: "nativeForm", model: "primate.cloud.environment",
            resId: id, title: name ? `Gestionar ${name}` : "Gestión de entorno",
        });
    }

    // Desde una lista nativa: abrir/crear un registro como form inline.
    openNativeForm(resId) {
        this.push({ type: "nativeForm", model: this.current.model, resId });
    }

    // Drill-through universal: abre el detalle a medida por modelo (todos los
    // modelos de dominio tienen su pantalla); resto -> form nativo embebido.
    openRecord(model, resId, title) {
        const detailType = DETAIL_TYPES[model];
        if (detailType) {
            this.push({
                type: detailType, model, resId,
                title: title || MODEL_LABELS[model] || "",
            });
        } else {
            this.push({ type: "nativeForm", model, resId, title });
        }
    }

    // ------------------------------------------------- Wizard drawer / registros
    openWizardDrawer(model, context, title) {
        this.state.wizard = { model, context: context || {}, title: title || "" };
    }

    closeWizardDrawer() {
        this.state.wizard = null;
    }

    // Abrir un FORMULARIO OWL propio en el drawer (no un wizard nativo). El que
    // abre pasa la clase del componente y sus props; recibe `onClose`.
    openFormDrawer(component, props, title) {
        this.state.form = { component, props: props || {}, title: title || "" };
    }

    closeFormDrawer() {
        this.state.form = null;
    }

    // Crear un registro nuevo con el form nativo embebido (inline, sin modal).
    openNewRecord(model, context) {
        this.push({
            type: "nativeForm", model, resId: false, context: context || {},
            title: `Nuevo ${(MODEL_LABELS[model] || "").toLowerCase()}`.trim(),
        });
    }

    confirm(message) {
        return new Promise((resolve) => {
            this.dialog.add(ConfirmationDialog, {
                title: "Confirmar",
                body: message,
                confirmLabel: "Sí",
                cancelLabel: "Cancelar",
                confirm: () => resolve(true),
                cancel: () => resolve(false),
            });
        });
    }

    get nativeListProps() {
        const model = this.current.model;
        return {
            type: "list",
            resModel: model,
            selectRecord: (resId) => this.openNativeForm(resId),
            createRecord: () => this.openNativeForm(false),
        };
    }

    get nativeFormProps() {
        const props = {
            type: "form",
            resModel: this.current.model,
            resId: this.current.resId || false,
        };
        if (this.current.context) {
            props.context = this.current.context;
        }
        return props;
    }

    get isNativeScreen() {
        return this.current.type === "nativeList" || this.current.type === "nativeForm";
    }

    // ------------------------------------------------- Deep link (hash)
    // Cambia la tab activa del detalle actual (p. ej. el panel de instancia) y
    // la refleja en el hash, para que el deep-link a una tab sobreviva al F5.
    setDetailTab(tab) {
        const stack = this.state.stack.slice();
        stack[stack.length - 1] = { ...stack[stack.length - 1], tab };
        this.state.stack = stack;
        this.syncHash();
    }

    syncHash() {
        const parts = this.state.stack.map((entry) => {
            if (DETAIL_HASH[entry.type]) {
                const base = `${DETAIL_HASH[entry.type]}-${entry.resId}`;
                // La tab va como sufijo ".<tab>" (se omite la default "dashboard").
                return entry.tab && entry.tab !== "dashboard"
                    ? `${base}.${entry.tab}` : base;
            }
            if (entry.type === "nativeList") {
                return `list-${entry.model}`;
            }
            if (entry.type === "nativeForm") {
                return `form-${entry.model}-${entry.resId || "new"}`;
            }
            return entry.type;
        });
        browser.location.hash = "pcm=" + parts.join("~");
    }

    restoreFromHash() {
        const match = (browser.location.hash || "").match(/pcm=([^&]+)/);
        if (!match) {
            return;
        }
        const stack = [];
        for (const part of decodeURIComponent(match[1]).split("~")) {
            const dashIdx = part.indexOf("-");
            const prefix = dashIdx === -1 ? part : part.slice(0, dashIdx);
            if (part === "inicio" || part === "entornos" || part === "proyectos") {
                stack.push(this.withTitle({ type: part }));
            } else if (HASH_DETAIL[prefix]) {
                const type = HASH_DETAIL[prefix];
                // El resto puede traer la tab como sufijo: "<resId>.<tab>".
                const rest = part.slice(dashIdx + 1);
                const dotIdx = rest.indexOf(".");
                const resId = Number(dotIdx === -1 ? rest : rest.slice(0, dotIdx));
                const tab = dotIdx === -1 ? undefined : rest.slice(dotIdx + 1);
                stack.push(this.withTitle({
                    type, model: DETAIL_MODEL[type], resId, tab,
                }));
            } else if (part.startsWith("list-")) {
                stack.push(this.withTitle({ type: "nativeList", model: part.slice(5) }));
            } else if (part.startsWith("form-")) {
                const rest = part.slice(5);
                const idx = rest.lastIndexOf("-");
                const model = rest.slice(0, idx);
                const idStr = rest.slice(idx + 1);
                stack.push(this.withTitle({
                    type: "nativeForm", model,
                    resId: idStr === "new" ? false : Number(idStr),
                }));
            }
        }
        if (stack.length) {
            this.state.stack = stack;
        }
    }
}

registry.category("actions").add("primate_cloud_manager.app", PcmApp);
