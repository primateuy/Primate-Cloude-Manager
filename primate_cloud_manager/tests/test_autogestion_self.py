# -*- coding: utf-8 -*-
"""Autogestión (mudanza de PCM a su propio servidor, 2026-11).

La instancia que hospeda a ESTE PCM se marca ``is_self`` y sobre ella —y su
servidor— lo destructivo/disruptivo se corta para TODO grupo, en los puntos
de EJECUCIÓN (acción, ORM y job): terminar, detener, reiniciar (máquina y
Odoo), restaurar encima, desmontar, cambiar dominio y ser origen de staging.
Arrancar la máquina queda PERMITIDO (camino de recuperación), igual que el
backup de sí mismo (lo exige la política de respaldos del punto 6).
"""
from unittest import mock

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "primate_cloud")
class TestAutogestionSelf(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "Primate", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "PrimateUY"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "PCM", "account_id": self.account.id,
            "partner_id": self.partner.id,
        })
        self.server = self.env["primate.cloud.environment"].create({
            "name": "pcm-self", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m-pcm", "account_id": self.account.id,
            "aws_instance_id": "i-pcmself", "instance_state": "running",
            "region": "us-east-1", "environment_id": self.server.id,
            "provisioned_by_pcm": True,
        })
        self.server.ec2_instance_id = self.machine
        self.server.state = "active"
        self.inst = self.server.primary_instance_id
        self.inst.is_self = True

    # ------------------------------------------------------------------
    # La marca
    # ------------------------------------------------------------------
    def test_self_es_unica(self):
        otro = self.env["primate.cloud.environment"].create({
            "name": "otro", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        with self.assertRaises(ValidationError):
            otro.primary_instance_id.is_self = True

    def test_marcar_es_solo_cloud_admin(self):
        operador = self.env["res.users"].create({
            "name": "Operador", "login": "op-self-test",
            "group_ids": [(4, self.env.ref(
                "primate_cloud_manager.group_cloud_operator").id)],
        })
        with self.assertRaises(UserError):
            self.inst.with_user(operador).is_self = False

    # ------------------------------------------------------------------
    # Máquina: stop/reboot/terminate bloqueados; start permitido
    # ------------------------------------------------------------------
    def test_lifecycle_maquina_bloqueado(self):
        for accion in ("action_stop", "action_restart"):
            with self.assertRaises(UserError, msg=accion):
                getattr(self.machine, accion)()
        with self.assertRaises(UserError):
            self.machine.action_terminate()
        with self.assertRaises(UserError):
            self.machine.action_open_terminate_wizard()

    def test_job_lifecycle_bloqueado_tambien_en_el_job(self):
        """El job es alcanzable por requeue/RPC: la guarda vive también ahí."""
        for tipo in ("ec2_stop", "ec2_restart", "ec2_terminate"):
            with self.assertRaises(UserError, msg=tipo):
                self.machine.job_lifecycle(tipo)

    def test_arrancar_no_esta_en_la_lista_bloqueada(self):
        """Iniciar la máquina es el camino de recuperación: queda permitido."""
        Machine = self.env["primate.cloud.ec2.instance"]
        self.assertNotIn("ec2_start", Machine._ACCIONES_SELF_MAQUINA)

    def test_restart_odoo_bloqueado(self):
        with self.assertRaises(UserError):
            self.machine.restart_odoo(odoo_instance=self.inst)

    # ------------------------------------------------------------------
    # Instancia: desmontar / eliminar / dominio
    # ------------------------------------------------------------------
    def test_desmontar_bloqueado(self):
        with self.assertRaises(UserError):
            self.inst.action_open_destroy_wizard()
        with self.assertRaises(UserError):
            self.inst._destruction_enqueue_destroy()
        with self.assertRaises(UserError):
            self.inst.job_destroy()

    def test_unlink_bloqueado(self):
        with self.assertRaises(UserError):
            self.inst.unlink()

    def test_cambiar_dominio_bloqueado(self):
        with self.assertRaises(UserError):
            self.inst.job_attach_domain("pcm.erp.primateuy.com")
        with self.assertRaises(UserError):
            self.inst.job_create_dns_and_switch(
                "pcm.erp.primateuy.com", "Z123", 300)

    # ------------------------------------------------------------------
    # Restore encima de PCM mismo
    # ------------------------------------------------------------------
    def test_restore_sobre_self_bloqueado(self):
        EnvModel = type(self.server)
        params = {
            "db_name": "pcm_produccion", "instance_id": self.machine.id,
            "odoo_instance_id": self.inst.id, "pre_backup": False,
            "origin": {"type": "s3_uri",
                       "uri": "s3://pcm-dumps-x/pcm.dump", "format": "auto"},
        }
        with mock.patch.object(
                EnvModel, "_restore_resolver_externo",
                return_value=("pcm-dumps-x", "us-east-1", None, False,
                              10, None)):
            with self.assertRaises(UserError):
                self.server.job_restore_backup(params)

    # ------------------------------------------------------------------
    # Staging con PCM mismo de origen
    # ------------------------------------------------------------------
    def _staging_de(self, origen):
        return self.env["primate.cloud.environment"].create({
            "name": "stg", "project_id": self.project.id,
            "env_type": "staging", "odoo_version": "19",
            "odoo_edition": "community",
            "origin_environment_id": origen.id,
        })

    def test_staging_origen_self_bloqueado(self):
        staging = self._staging_de(self.server)
        with self.assertRaises(UserError):
            staging.job_create_staging({})
        with self.assertRaises(UserError):
            staging.job_refresh_staging({})

    def test_staging_instancia_origen_self_bloqueado(self):
        destino = self.env["primate.cloud.environment"].create({
            "name": "srv2", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        with self.assertRaises(UserError):
            destino.job_create_staging_instance(
                destino.primary_instance_id.id,
                {"origin_environment_id": self.server.id})

    # ------------------------------------------------------------------
    # UI: el payload lleva las banderas
    # ------------------------------------------------------------------
    def test_payload_dashboard_lleva_banderas(self):
        Dash = self.env["primate.cloud.dashboard"]
        server_data = Dash.get_server_detail(self.machine.id)
        self.assertTrue(server_data["hosts_self"])
        env_data = Dash.get_environment_detail(self.server.id)
        self.assertTrue(env_data["hosts_self"])

    # ------------------------------------------------------------------
    # La bitácora audita el intento
    # ------------------------------------------------------------------
    def test_bloqueo_queda_en_bitacora(self):
        # try/except y no assertRaises: el assertRaises de Odoo envuelve en
        # un savepoint que al rollbackear se llevaría el registro (mismo
        # motivo que test_blindaje_readonly).
        Log = self.env["primate.cloud.operation.log"]
        antes = Log.search_count([("action_type", "=", "self_block")])
        try:
            self.machine.action_stop()
        except UserError:
            pass
        self.assertEqual(
            Log.search_count([("action_type", "=", "self_block")]), antes + 1)
