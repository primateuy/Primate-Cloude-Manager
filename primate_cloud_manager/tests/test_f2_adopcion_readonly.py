# -*- coding: utf-8 -*-
"""F2: modo adoptado / solo lectura para servidores importados.

Lo que se cubre, en orden de importancia:

1. **El bloqueo server-side**, en los SIETE puntos donde una acción de
   escritura se ejecuta. No alcanza con que el botón esté invisible: el job es
   alcanzable por vía programática y el wizard de comandos encola directo sobre
   la instancia. Si alguno de estos tests se cae, hay un camino que saltea el
   modo y el feature no cumple su propósito.
2. **Que la lectura siga permitida**: observar es para lo que existe el modo.
3. **La transición a ``managed``**: admin-only, y exige que cada instancia Odoo
   detectada tenga cliente real — la regla es por INSTANCIA, no por entorno,
   porque D6 deja el entorno sin proyecto a propósito en servidores compartidos.
4. **Que ``provisioned`` no se vea afectado**: lo que PCM creó sigue igual.
"""
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase, new_test_user


@tagged("post_install", "-at_install", "primate_cloud")
class TestF2AdopcionReadonly(TransactionCase):
    """Servidores importados: inofensivos por construcción."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "Cuenta Primate", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Cliente Real"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Cliente Real", "account_id": self.account.id,
            "partner_id": self.partner.id,
        })
        # El centinela de "sin clasificar" (patrón del partner centinela).
        self.centinela = self.env["primate.cloud.project"].create({
            "name": "⚠ SIN CLASIFICAR", "partner_id": self.partner.id,
        })
        self.env["ir.config_parameter"].sudo().set_param(
            "pcm.unclassified_project_id", str(self.centinela.id))

        # Servidor ADOPTADO (como lo dejaría el import de F2).
        self.env_adoptado = self.env["primate.cloud.environment"].create({
            "name": "Servidor Importado", "project_id": self.project.id,
            "state": "active",
        })
        self.adoptado = self.env["primate.cloud.ec2.instance"].create({
            "name": "prod-cliente-real", "account_id": self.account.id,
            "environment_id": self.env_adoptado.id,
            "aws_instance_id": "i-importada", "instance_state": "running",
            "region": "us-east-1",
        })
        self.adoptado.management_state = "adopted_readonly"

        # Servidor PROVISIONADO (lo creó PCM): no debe cambiar de comportamiento.
        self.env_pcm = self.env["primate.cloud.environment"].create({
            "name": "Servidor PCM", "project_id": self.project.id,
            "state": "active",
        })
        self.provisionado = self.env["primate.cloud.ec2.instance"].create({
            "name": "pcm-srv", "account_id": self.account.id,
            "environment_id": self.env_pcm.id,
            "aws_instance_id": "i-pcm", "instance_state": "running",
            "region": "us-east-1", "provisioned_by_pcm": True,
        })

    # ------------------------------------------------------------------
    # 1. El bloqueo, en los siete puntos de ejecución
    # ------------------------------------------------------------------
    def test_default_del_campo_es_provisioned(self):
        """Un EC2 creado sin decir nada es 'provisioned': no cambia nada."""
        self.assertEqual(self.provisionado.management_state, "provisioned")

    def test_stop_bloqueado(self):
        with self.assertRaises(UserError) as e:
            self.adoptado.action_stop()
        self.assertIn("solo lectura", str(e.exception))
        self.assertIn("prod-cliente-real", str(e.exception))

    def test_start_bloqueado(self):
        with self.assertRaises(UserError):
            self.adoptado.action_start()

    def test_restart_bloqueado(self):
        with self.assertRaises(UserError):
            self.adoptado.action_restart()

    def test_terminate_bloqueado(self):
        """Terminate es admin-only Y bloqueado por el modo."""
        with self.assertRaises(UserError):
            self.adoptado.with_user(
                self.env.ref("base.user_admin")).action_terminate()

    def test_job_lifecycle_bloqueado(self):
        """La vía programática también: el job es alcanzable sin el botón."""
        with self.assertRaises(UserError):
            self.adoptado.job_lifecycle("ec2_stop")

    def test_job_execute_command_bloqueado(self):
        """El punto inevadible: el wizard encola este job DIRECTO."""
        with self.assertRaises(UserError):
            self.adoptado.job_execute_command("systemctl stop odoo")

    def test_wizard_de_comando_no_puede_ejecutar(self):
        """Encolar desde el wizard tampoco esquiva el bloqueo."""
        wizard = self.env["primate.cloud.ec2.command.wizard"].create({
            "instance_id": self.adoptado.id, "command": "rm -rf /"})
        # El wizard encola; el job levanta. Se prueba el job, que es el que corre.
        with self.assertRaises(UserError):
            self.adoptado.job_execute_command(wizard.command)

    def test_restart_odoo_bloqueado(self):
        with self.assertRaises(UserError):
            self.adoptado.restart_odoo()

    def test_clone_addon_bloqueado(self):
        with self.assertRaises(UserError):
            self.adoptado.clone_addon("https://github.com/x/y.git", "/tmp/x")

    def test_deploy_impersonate_bloqueado(self):
        with self.assertRaises(UserError):
            self.adoptado.deploy_impersonate(["unadb"])

    def test_save_config_bloqueado(self):
        with self.assertRaises(UserError):
            self.adoptado.action_save_config({"workers": "4"}, "hash")

    def test_restore_sobre_servidor_adoptado_bloqueado(self):
        """Un restore apuntado a un servidor adoptado no puede correr."""
        with self.assertRaises(UserError):
            self.adoptado._exigir_gestion_habilitada("restore")

    # ------------------------------------------------------------------
    # 2. La lectura sigue permitida (es el propósito del modo)
    # ------------------------------------------------------------------
    def test_sync_de_estado_permitido(self):
        """Observar el estado en AWS es lectura: no se bloquea."""
        with mock.patch.object(
            type(self.adoptado), "with_delay"
        ) as delay:
            self.adoptado.action_sync_from_aws()
            delay.assert_called_once()

    def test_lectura_de_campos_permitida(self):
        self.assertEqual(self.adoptado.instance_state, "running")
        self.assertTrue(self.adoptado.aws_instance_id)

    # ------------------------------------------------------------------
    # 3. La transición a managed
    # ------------------------------------------------------------------
    def test_transicion_exige_admin(self):
        operador = new_test_user(
            self.env, login="pcm_operador_f2",
            groups="primate_cloud_manager.group_cloud_operator")
        with self.assertRaises(UserError) as e:
            self.adoptado.with_user(operador).action_habilitar_gestion()
        self.assertIn("Cloud Admin", str(e.exception))

    def test_transicion_exige_instancias_clasificadas(self):
        """La regla es por INSTANCIA: el centinela bloquea el pase."""
        self.env_adoptado.instance_ids.write({"project_id": self.centinela.id})
        with self.assertRaises(UserError) as e:
            self.adoptado.action_habilitar_gestion()
        self.assertIn("Clasificá primero", str(e.exception))

    def test_transicion_abre_el_wizard_si_esta_clasificado(self):
        accion = self.adoptado.action_habilitar_gestion()
        self.assertEqual(accion["res_model"],
                         "primate.cloud.server.manage.wizard")

    def test_wizard_exige_el_nombre_exacto(self):
        wizard = self.env["primate.cloud.server.manage.wizard"].create({
            "instance_id": self.adoptado.id,
            "confirm_server_name": "nombre-equivocado"})
        with self.assertRaises(UserError) as e:
            wizard.action_confirm()
        self.assertIn("nombre exacto", str(e.exception))

    def test_wizard_habilita_y_deja_bitacora(self):
        wizard = self.env["primate.cloud.server.manage.wizard"].create({
            "instance_id": self.adoptado.id,
            "confirm_server_name": self.adoptado.name})
        wizard.action_confirm()
        self.assertEqual(self.adoptado.management_state, "managed")
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "server_manage_enable"),
             ("resource_id", "=", self.adoptado.id)], limit=1)
        self.assertTrue(log, "la transición tiene que quedar en la bitácora")
        self.assertEqual(log.result, "success")

    def test_tras_habilitar_las_acciones_funcionan(self):
        """Habilitado el modo, el bloqueo desaparece."""
        self.adoptado.management_state = "managed"
        with mock.patch.object(type(self.adoptado), "with_delay") as delay:
            self.adoptado.action_stop()
            delay.assert_called_once()

    def test_wizard_muestra_las_instancias_detectadas(self):
        wizard = self.env["primate.cloud.server.manage.wizard"].create({
            "instance_id": self.adoptado.id})
        self.assertIn("Cliente Real", wizard.detected_instances)

    def test_no_se_puede_habilitar_dos_veces(self):
        self.adoptado.management_state = "managed"
        with self.assertRaises(UserError) as e:
            self.adoptado.action_habilitar_gestion()
        self.assertIn("no está adoptado", str(e.exception))

    # ------------------------------------------------------------------
    # 4. provisioned no se ve afectado
    # ------------------------------------------------------------------
    def test_provisionado_puede_operarse(self):
        with mock.patch.object(type(self.provisionado), "with_delay") as delay:
            self.provisionado.action_stop()
            delay.assert_called_once()

    def test_provisionado_no_pasa_por_la_transicion(self):
        with self.assertRaises(UserError):
            self.provisionado.action_habilitar_gestion()
