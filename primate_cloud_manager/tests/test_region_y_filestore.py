# -*- coding: utf-8 -*-
"""Región del servidor destino y mensaje de filestore (2026-09-19).

1. **Región.** El staging en el MISMO servidor falló con «InvalidInstanceId»:
   el formulario de staging trae su propio campo región (default: la de la
   cuenta, us-east-1) y ese valor pisaba a la región de la máquina (us-east-2)
   en ``job_add_instance`` y ``job_create_staging_instance``. Sólo se ve con
   servidores fuera de la región default de la cuenta — justo lo que trae la
   importación de servidores reales.
2. **Filestore.** Tras un restore desde dump externo, el chatter decía «sin
   filestore» (nota del camino del catálogo) aunque se hubieran restaurado
   2.470 archivos. Ahora se informa desde los marcadores del script.
"""
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests import Form, tagged
from odoo.tests.common import TransactionCase

from odoo.addons.primate_cloud_manager.services import aws_ssm


@tagged("post_install", "-at_install", "primate_cloud")
class TestRegionDelServidorDestino(TransactionCase):

    def setUp(self):
        super().setUp()
        # Cuenta en us-east-1, servidor en us-east-2: el caso que explotó.
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Barraca Julia"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Barraca Julia", "account_id": self.account.id,
            "partner_id": self.partner.id,
        })
        self.srv = self.env["primate.cloud.environment"].create({
            "name": "Barraca Julia", "project_id": self.project.id,
            "account_id": self.account.id, "env_type": "testing", "state": "active",
        })
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "bj-ensayo", "account_id": self.account.id,
            "environment_id": self.srv.id, "aws_instance_id": "i-bj",
            "instance_state": "running", "region": "us-east-2",
        })

    def _instancia(self):
        return self.srv._create_instance_with_ports({
            "name": "Barraca Julia (Staging)", "project_id": self.project.id,
            "slug": "barraca-julia-staging", "env_type": "staging"})

    def test_agregar_instancia_usa_la_region_de_la_maquina(self):
        vista = {}

        def cortar(ssm, machine, region):
            vista["region"] = region
            raise UserError("corte del test")

        inst = self._instancia()
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=mock.Mock()), \
                mock.patch.object(aws_ssm, "AwsSsmService"), \
                mock.patch.object(type(self.srv), "_ensure_server_bootstrap",
                                  side_effect=cortar):
            ok = self.srv.job_add_instance(inst.id, {
                "region": "us-east-1",   # lo que mandaba el formulario
                "db_mode": "local_pg", "db_name": "barraca_julia_staging",
                "db_password": "abc"})
        self.assertFalse(ok)
        self.assertEqual(vista["region"], "us-east-2")

    def test_staging_respalda_el_origen_en_la_region_de_su_maquina(self):
        vista = {}

        def cortar(origin, origin_instance, origin_db, region, **kw):
            vista["region"] = region
            raise UserError("corte del test")

        inst = self._instancia()
        with mock.patch.object(type(self.srv), "job_add_instance", return_value=True), \
                mock.patch.object(type(self.srv), "_staging_source_backup",
                                  side_effect=cortar):
            ok = self.srv.job_create_staging_instance(inst.id, {
                "region": "us-east-1", "origin_environment_id": self.srv.id,
                "origin_instance_id": self.machine.id,
                "db_name": "barraca_julia_staging",
                "transfer_bucket": "pcm-backups-test"})
        self.assertFalse(ok)
        self.assertEqual(vista["region"], "us-east-2")

    def test_formulario_toma_la_region_del_servidor_destino(self):
        form = Form(self.env["primate.cloud.staging.create.wizard"].with_context(
            default_origin_environment_id=self.srv.id))
        self.assertEqual(form.target_server_id, self.srv)
        self.assertEqual(form.region, "us-east-2")

    def test_region_readonly_con_servidor_destino(self):
        vista = self.env.ref(
            "primate_cloud_manager.view_staging_create_wizard_form",
            raise_if_not_found=False) or self.env["ir.ui.view"].search([
                ("model", "=", "primate.cloud.staging.create.wizard"),
                ("type", "=", "form")], limit=1)
        self.assertIn('name="region" readonly="target_server_id"', vista.arch)


@tagged("post_install", "-at_install", "primate_cloud")
class TestMensajeFilestore(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        partner = self.env["res.partner"].create({"name": "Barraca Julia"})
        project = self.env["primate.cloud.project"].create({
            "name": "Barraca Julia", "account_id": self.account.id,
            "partner_id": partner.id})
        self.srv = self.env["primate.cloud.environment"].create({
            "name": "BJ", "project_id": project.id, "account_id": self.account.id,
            "env_type": "testing", "state": "active"})
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "bj", "account_id": self.account.id, "environment_id": self.srv.id,
            "aws_instance_id": "i-bj", "instance_state": "running", "region": "us-east-2"})
        self.srv.primary_instance_id.write({
            "service_name": "odoo-bj", "pg_user": "odoo_bj",
            "data_dir": "/opt/pcm/instances/bj/data"})

    def _restore(self, stdout):
        fake = mock.Mock()
        fake.run_script.return_value = {"status": "Success", "stdout": stdout, "stderr": ""}
        resuelto = ("pcm-backups-test", "us-east-2", self.account, False, 320.0, None)
        with mock.patch.object(type(self.srv), "_restore_resolver_externo",
                               return_value=resuelto), \
                mock.patch.object(type(self.machine), "_get_ssm_service", return_value=fake):
            ok = self.srv.job_restore_backup({
                "db_name": "barraca_julia", "instance_id": self.machine.id,
                "pre_backup": False, "delete_source": False,
                "origin": {"type": "s3_uri", "uri": "s3://pcm-backups-test/in/bj.zip",
                           "format": "auto"}})
        chatter = self.srv.message_ids[:1].body or ""
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "backup_restore"), ("resource_id", "=", self.srv.id)],
            limit=1, order="id desc")
        return ok, str(chatter), log.error_message or ""

    def test_filestore_restaurado_con_su_cantidad(self):
        ok, chatter, log = self._restore(
            "PCM_FORMAT=odoo_zip\nPCM_DROP_STARTED\nPCM_FILESTORE_OK /opt/x\n"
            "PCM_FILESTORE_FILES=2470\nPCM_NEUTRALIZED\nPCM_CHECKLIST_OK\nPCM_RESTORE_OK")
        self.assertTrue(ok)
        for texto in (chatter, log):
            self.assertIn("Filestore restaurado (2470 archivos)", texto)
            self.assertNotIn("sin filestore", texto)

    def test_sin_filestore_en_el_origen(self):
        ok, chatter, log = self._restore(
            "PCM_FORMAT=custom\nPCM_DROP_STARTED\nPCM_FILESTORE_SKIP\n"
            "PCM_NEUTRALIZED\nPCM_CHECKLIST_OK\nPCM_RESTORE_OK")
        self.assertTrue(ok)
        self.assertIn("Sin filestore en el origen", chatter)
        self.assertNotIn("el backup no tenía", chatter)

    def test_el_script_cuenta_los_archivos(self):
        script = self.env["primate.cloud.environment"]._build_backup_restore_script(
            "barraca_julia", self.env["primate.cloud.backup"], False,
            self.srv.primary_instance_id,
            origin={"type": "s3_uri", "uri": "s3://pcm-backups-test/in/bj.zip",
                    "format": "auto", "bucket": "pcm-backups-test", "key": "in/bj.zip"},
            origin_size_mb=320.0)
        self.assertIn("PCM_FILESTORE_FILES=$(find", script)
        self.assertLess(script.index("PCM_FILESTORE_OK"),
                        script.index("PCM_FILESTORE_FILES"))
