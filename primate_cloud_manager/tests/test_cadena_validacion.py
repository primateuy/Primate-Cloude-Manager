# -*- coding: utf-8 -*-
"""La configuración se valida ANTES de crear nada en AWS (2026-09-19).

En la grabación del ensayo de Barraca Julia el entorno se creó sin versión de
Odoo: PCM lanzó la EC2 (facturable), la bootstrapeó y recién ahí falló la
instalación. Y el camino de recuperación no andaba: el job de instalación
captura su error y termina `done`, y el reintento sólo buscaba `failed`.

Mismo principio que el semáforo de red («cadena completa verde antes de
aprovisionar»), aplicado a la configuración. Lo que fija este archivo:

1. Versión y edición obligatorias EN EL MODELO, con default 19 / Community.
2. La cadena completa (versión, edición, slug, base, contraseña PG, bucket de
   transferencia) se valida al encolar, y otra vez al arrancar el job, ANTES
   de RunInstances — en aprovisionar, agregar instancia y staging.
3. «Reintentar instalación» encuentra el install fallido aunque haya
   terminado `done`, y reinstala sobre la EC2 existente (no lanza otra).
"""
import inspect
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger


@tagged("post_install", "-at_install", "primate_cloud")
class TestCadenaValidadaAntesDeAws(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Barraca Julia"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Barraca Julia", "account_id": self.account.id,
            "partner_id": self.partner.id,
        })
        self.Env = self.env["primate.cloud.environment"]

    def _entorno(self, **vals):
        base = {"name": "Barraca Julia", "project_id": self.project.id,
                "account_id": self.account.id, "env_type": "testing"}
        base.update(vals)
        return self.Env.create(base)

    def _params(self, **extra):
        base = {"region": "us-east-1", "domain": "bj.test", "db_mode": "local_pg",
                "db_name": "barraca_julia", "instance_type": "t3.small"}
        base.update(extra)
        return base

    # ------------------------------------------------------------------
    # 1. Versión y edición obligatorias, con default
    # ------------------------------------------------------------------
    def test_entorno_nuevo_nace_con_19_community(self):
        env = self._entorno()
        self.assertEqual(env.primary_instance_id.odoo_version, "19")
        self.assertEqual(env.primary_instance_id.odoo_edition, "community")
        self.assertEqual(env.odoo_version, "19")

    def test_false_explicito_no_pisa_el_default(self):
        """Lo que manda el formulario con los campos vacíos."""
        env = self._entorno(odoo_version=False, odoo_edition=False)
        self.assertEqual(env.odoo_version, "19")
        self.assertEqual(env.odoo_edition, "community")

    def test_la_version_elegida_se_respeta(self):
        env = self._entorno(odoo_version="18", odoo_edition="enterprise")
        self.assertEqual(env.primary_instance_id.odoo_version, "18")
        self.assertEqual(env.primary_instance_id.odoo_edition, "enterprise")

    def test_modelo_instancia_exige_version(self):
        campos = self.env["primate.cloud.instance"]._fields
        self.assertTrue(campos["odoo_version"].required)
        self.assertTrue(campos["odoo_edition"].required)
        env = self._entorno()
        with mute_logger("odoo.sql_db"), self.assertRaises(Exception):
            with self.env.cr.savepoint():
                env.primary_instance_id.write({"odoo_version": False})
                env.primary_instance_id.flush_recordset()

    # ------------------------------------------------------------------
    # 2. La cadena completa se valida antes de tocar AWS
    # ------------------------------------------------------------------
    def test_helper_lista_todo_lo_que_falta(self):
        env = self._entorno()
        with self.assertRaises(UserError) as e:
            env._exigir_cadena_de_instalacion(
                {"db_name": "barraca julia", "db_mode": "local_pg", "db_password": "a'b"},
                version=False, edition=False, slug="Mal Slug",
                bucket="otro-bucket", exigir_bucket=True)
        msg = str(e.exception)
        for esperado in ("versión de Odoo", "edición de Odoo", "slug", "nombre de la base",
                         "contraseña de PostgreSQL", "pcm-*", "No se creó nada en AWS"):
            self.assertIn(esperado, msg)

    def test_encolar_con_base_invalida_no_encola_ni_cambia_estado(self):
        env = self._entorno()
        with mock.patch.object(type(env), "with_delay") as delay, \
                self.assertRaises(UserError):
            env._enqueue_provision(self._params(db_name="barraca julia"))
        delay.assert_not_called()
        self.assertEqual(env.state, "draft")

    def test_encolar_sin_proyecto_explica_que_falta(self):
        env = self.Env.create({"name": "Suelto", "account_id": self.account.id})
        self.assertFalse(env.primary_instance_id)
        with self.assertRaises(UserError) as e:
            env._enqueue_provision(self._params())
        self.assertIn("proyecto", str(e.exception))

    def test_encolar_valido_encola(self):
        env = self._entorno()
        with mock.patch.object(type(env), "with_delay") as delay:
            env._enqueue_provision(self._params())
        delay.return_value.job_provision_server.assert_called_once()
        self.assertEqual(env.state, "provisioning")

    def test_job_con_config_invalida_falla_antes_de_la_ec2(self):
        """Defensa: un job que llegue con params inválidos no crea la EC2."""
        env = self._entorno()
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(type(env), "_provision_ec2") as ec2, \
                mock.patch.object(type(env), "_fill_network_from_discovery") as red:
            ok = env.job_provision_server(self._params(db_name="con espacio"))
        self.assertFalse(ok)
        ec2.assert_not_called()
        red.assert_not_called()
        self.assertEqual(env.state, "error")

    def test_agregar_instancia_con_base_invalida_no_encola(self):
        env = self._entorno(state="active")
        inst = env._create_instance_with_ports({
            "name": "Otra", "project_id": self.project.id, "slug": "otra"})
        with mock.patch.object(type(env), "with_delay") as delay, \
                self.assertRaises(UserError):
            env._enqueue_add_instance(inst, {"db_name": "otra base"})
        delay.assert_not_called()

    def test_staging_con_bucket_ajeno_no_crea_nada(self):
        origen = self._entorno(state="active")
        instancias = self.env["primate.cloud.instance"].search_count([])
        with mock.patch.object(type(origen), "_resolve_staging_origin",
                               return_value=(self.env["primate.cloud.ec2.instance"],
                                             self.env["primate.cloud.database"])), \
                self.assertRaises(UserError) as e:
            origen._enqueue_staging({"name": "Barraca Julia (Staging)",
                                     "transfer_bucket": "mi-bucket",
                                     "target_server_id": origen.id})
        self.assertIn("pcm-*", str(e.exception))
        self.assertEqual(self.env["primate.cloud.instance"].search_count([]), instancias)

    def test_staging_nombre_de_base_por_default_es_valido(self):
        origen = self._entorno()
        self.assertEqual(origen._staging_db_name_default(), "barraca_julia_staging")
        raro = self._entorno(name="SMOKE UI (borrar)")
        self.assertEqual(raro._staging_db_name_default(), "smoke_ui_borrar_staging")

    def test_wizard_propone_nombre_de_base_valido(self):
        env = self._entorno(name="SMOKE UI (borrar)")
        wiz = self.env["primate.cloud.provision.wizard"].new({
            "environment_id": env.id, "account_id": self.account.id})
        wiz._onchange_environment_id()
        self.assertEqual(wiz.db_name, "smoke_ui_borrar")

    # ------------------------------------------------------------------
    # 3. El reintento encuentra el install fallido y no lanza otra EC2
    # ------------------------------------------------------------------
    def _con_maquina_en_error(self):
        env = self._entorno(state="error")
        self.env["primate.cloud.ec2.instance"].create({
            "name": "bj", "account_id": self.account.id, "environment_id": env.id,
            "aws_instance_id": "i-bj", "instance_state": "stopped",
            "region": "us-east-1"})
        return env

    def test_reintento_encuentra_install_terminado_done(self):
        """El caso real: el job capturó el error y quedó `done`."""
        env = self._con_maquina_en_error()
        job = env.with_delay().job_install_instance(self._params())
        job.db_record().write({"state": "done"})
        jobs_antes = self.env["queue.job"].search_count([])
        env.action_retry_install()
        self.assertEqual(job.db_record().state, "pending")
        self.assertEqual(env.state, "provisioning")
        # Se re-encoló el MISMO job: no apareció ningún job nuevo (ni otro
        # aprovisionamiento de servidor).
        self.assertEqual(self.env["queue.job"].search_count([]), jobs_antes)

    def test_reintento_sin_maquina_viva_guia_al_wizard(self):
        env = self._entorno(state="error")
        job = env.with_delay().job_install_instance(self._params())
        job.db_record().write({"state": "done"})
        with self.assertRaises(UserError):
            env.action_retry_install()

    def test_reintento_no_aplica_si_el_entorno_no_esta_en_error(self):
        env = self._con_maquina_en_error()
        env.state = "active"
        job = env.with_delay().job_install_instance(self._params())
        job.db_record().write({"state": "done"})
        with self.assertRaises(UserError):
            env.action_retry_install()

    def test_el_install_reintentado_no_crea_ec2(self):
        """job_install_instance trabaja sobre la máquina existente: no llama a
        la creación de EC2 en ningún camino."""
        fuente = inspect.getsource(type(self.Env).job_install_instance)
        self.assertNotIn("_provision_ec2", fuente)
        self.assertNotIn("run_instances", fuente)
