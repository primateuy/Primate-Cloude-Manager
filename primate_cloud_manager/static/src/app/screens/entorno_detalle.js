/** @odoo-module **/

import { Component, onWillStart, onWillUpdateProps, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { PcmStatusBadge } from "../components/status_badge";
import { runPcmModelAction } from "../pcm_actions";
import { DnsForm } from "../forms/dns_form";
import { DnsDeleteForm } from "../forms/dns_delete_form";
import { EnvForm } from "../forms/env_form";

/**
 * Hub del entorno: barra de acciones (aprovisionar/staging/sincronizar), sección
 * de instancias (servidor + sus bases, agrupadas por `ec2_instance_id`), DNS,
 * despliegues y trazabilidad por repositorio. Todo cablea métodos existentes;
 * agregar infra abre wizards en el drawer o forms nativos inline.
 * Datos de `primate.cloud.dashboard.get_environment_detail` (solo lectura).
 */
export class EntornoDetalle extends Component {
    static template = "primate_cloud_manager.EntornoDetalle";
    static components = { PcmStatusBadge };
    static props = {
        envId: { type: Number },
        onBack: { type: Function, optional: true },
        onManage: { type: Function, optional: true },
        onOpenRecord: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.model = "primate.cloud.environment";
        this.state = useState({ loading: true, data: {} });
        onWillStart(() => this.load(this.props.envId));
        onWillUpdateProps((next) => {
            if (next.envId !== this.props.envId) {
                this.load(next.envId);
            }
        });
    }

    async load(envId) {
        this.state.loading = true;
        this.state.data = await this.orm.call(
            "primate.cloud.dashboard", "get_environment_detail", [envId]
        );
        this.state.loading = false;
    }

    get d() {
        return this.state.data;
    }

    // Instancias = cada servidor + las bases que lo apuntan (server_id). Las bases
    // sin servidor asociado (RDS, sueltas) se agrupan aparte.
    get instances() {
        const d = this.state.data;
        const servers = d.servers || [];
        const databases = d.databases || [];
        return servers.map((server) => ({
            server,
            databases: databases.filter((db) => db.server_id === server.id),
        }));
    }

    get looseDatabases() {
        const d = this.state.data;
        return (d.databases || []).filter((db) => !db.server_id);
    }

    // --- Acciones sobre el entorno ---
    async runEnvMethod(method, { confirm } = {}) {
        const ran = await runPcmModelAction(
            this.env, this.orm, this.model, method, [this.props.envId], { confirm }
        );
        if (ran) {
            await this.load(this.props.envId);
        }
    }

    // Aprovisionar (Crear entorno): abre el formulario OWL en el drawer. El
    // botón sólo se muestra en estado draft/error, así que el guard de estado ya
    // está cubierto por visibilidad.
    provisionEnv() {
        this.env.pcm.openForm(EnvForm, {
            envId: this.props.envId,
            onSaved: () => this.load(this.props.envId),
        }, "Crear entorno");
    }

    // Sincronizar los servidores del entorno desde AWS (método existente de EC2).
    async syncServers() {
        const ids = (this.d.servers || []).map((s) => s.id);
        if (!ids.length) {
            this.env.pcm.notify("Este entorno no tiene servidores para sincronizar.",
                { type: "warning" });
            return;
        }
        await runPcmModelAction(
            this.env, this.orm, "primate.cloud.ec2.instance",
            "action_sync_from_aws", ids
        );
    }

    // Restaurar un backup: abre el wizard (con sus salvaguardas) en el drawer.
    async restoreBackup(backupId) {
        await runPcmModelAction(
            this.env, this.orm, "primate.cloud.backup", "action_restore", [backupId]
        );
    }

    async runRepoMethod(repoId, method) {
        await runPcmModelAction(
            this.env, this.orm, "primate.cloud.repository", method, [repoId]
        );
    }

    async runDeploymentMethod(depId, method, { confirm } = {}) {
        const ran = await runPcmModelAction(
            this.env, this.orm, "primate.cloud.deployment", method, [depId], { confirm }
        );
        if (ran) {
            await this.load(this.props.envId);
        }
    }

    // --- Agregar infraestructura (wizard en drawer o form nativo inline) ---
    newInstance() {
        this.env.pcm.openWizard("primate.cloud.ec2.create.wizard", {
            default_environment_id: this.props.envId,
            default_account_id: this.d.account_id || false,
        }, "Nueva instancia");
    }

    addRepo() {
        this.env.pcm.newRecord("primate.cloud.repository", {
            default_environment_id: this.props.envId,
        });
    }

    addDns() {
        // Formulario OWL de creación en el drawer. Prefija la zona desde un
        // registro existente del entorno, si hay.
        const zoneHint = (this.d.dns_records || [])
            .map((r) => r.hosted_zone_id).find(Boolean) || "";
        this.env.pcm.openForm(DnsForm, {
            mode: "create",
            accountId: this.d.account_id || false,
            environmentId: this.props.envId,
            hostedZoneId: zoneHint,
            onSaved: () => this.load(this.props.envId),
        }, "Nuevo registro DNS");
    }

    // Editar / borrar / verificar un registro DNS (forms OWL en drawer o job).
    editDns(recordId) {
        this.env.pcm.openForm(DnsForm, {
            mode: "edit", recordId,
            onSaved: () => this.load(this.props.envId),
        }, "Editar registro DNS");
    }

    deleteDns(recordId) {
        this.env.pcm.openForm(DnsDeleteForm, {
            recordId,
            onSaved: () => this.load(this.props.envId),
        }, "Eliminar registro DNS");
    }

    async checkDns(recordId) {
        await runPcmModelAction(
            this.env, this.orm, "primate.cloud.dns.record",
            "action_check_sync_state", [recordId]);
    }

    addDatabase() {
        this.env.pcm.newRecord("primate.cloud.database", {
            default_environment_id: this.props.envId,
            default_account_id: this.d.account_id || false,
        });
    }

    newDeployment() {
        this.env.pcm.newRecord("primate.cloud.deployment", {
            default_environment_id: this.props.envId,
        });
    }

    // --- Navegación ---
    back() {
        if (this.props.onBack) {
            this.props.onBack();
        }
    }

    manage() {
        if (this.props.onManage) {
            this.props.onManage(this.props.envId, this.state.data.name);
        }
    }

    openRecord(model, id, name) {
        if (this.props.onOpenRecord && id) {
            this.props.onOpenRecord(model, id, name);
        }
    }
}
