# -*- coding: utf-8 -*-
"""Correcciones al contrato del modo adoptado (d57d918): de 7 a 9 puntos.

Tres caminos permitían operar un servidor adoptado sin pasar por
``_exigir_gestion_habilitada``:

1. ``_sync_from_aws`` creaba los EC2 nuevos en ``provisioned`` (el default), así
   que cualquier servidor que entrara por «Sincronizar recursos» quedaba
   operable. La migración 19.0.1.4.0 sólo cubría a los que ya estaban.
2. ``_job_sync_client_tag`` —lo dispara registrar una instancia— escribía tags
   en la EC2 (``create_tags``/``delete_tags``). Punto 8.
3. «Re-etiquetar recursos» tomaba todo EC2 con entorno. Punto 9.

Cada test de bloqueo lleva su CONTROL (el mismo camino sobre un servidor
``provisioned`` sí actúa): sin él, un test que sólo verifica «no se llamó»
pasaría también si el camino estuviera roto para todos.
"""
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

SVC = "odoo.addons.primate_cloud_manager.services"


def ec2(iid, name, state="running"):
    return {"aws_instance_id": iid, "name": name, "instance_state": state,
            "instance_type": "t3.small", "public_ip": None, "private_ip": None,
            "region": "us-east-1", "tags": {}, "volume_ids": [],
            "created_at": None}


@tagged("post_install", "-at_install", "primate_cloud")
class TestF2ContratoFix(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.Ec2 = self.env["primate.cloud.ec2.instance"]
        partner = self.env["res.partner"].create({"name": "Cliente"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Cliente", "account_id": self.account.id,
            "partner_id": partner.id})

    def _servidor(self, iid, modo):
        """Un servidor con entorno e instancia, en el modo pedido."""
        entorno = self.env["primate.cloud.environment"].create({
            "name": "Srv %s" % iid, "project_id": self.project.id,
            "state": "active"})
        maquina = self.Ec2.create({
            "name": iid, "account_id": self.account.id,
            "environment_id": entorno.id, "aws_instance_id": iid,
            "instance_state": "running", "region": "us-east-1"})
        maquina.management_state = modo
        entorno.ec2_instance_id = maquina
        return entorno, maquina

    # 1. El sync -----------------------------------------------------------
    def test_el_sync_crea_en_solo_lectura(self):
        self.Ec2._sync_from_aws(self.account, [ec2("i-9", "suelto")])
        maquina = self.Ec2.search([("aws_instance_id", "=", "i-9")])
        self.assertEqual(maquina.management_state, "adopted_readonly")

    def test_el_sync_no_revoca_ni_otorga_una_habilitacion(self):
        """El re-sync no cambia la marca de origen. (Regla 2026-10-22: un
        importado ya no puede pasar a managed — se afirma que sigue
        adopted_readonly tras re-sincronizar, y que nadie lo cambia.)"""
        self.Ec2._sync_from_aws(self.account, [ec2("i-9", "suelto")])
        maquina = self.Ec2.search([("aws_instance_id", "=", "i-9")])
        with self.assertRaises(UserError):
            maquina.management_state = "managed"
        self.Ec2._sync_from_aws(self.account, [ec2("i-9", "suelto")])
        self.assertEqual(maquina.management_state, "adopted_readonly")

    def test_el_contrato_de_retorno_del_sync_no_cambia(self):
        res = self.Ec2._sync_from_aws(self.account, [ec2("i-9", "suelto")])
        self.assertEqual(res, {"created": 1, "updated": 0})

    def test_register_provisioned_gana_la_carrera(self):
        """El sync lo vio antes (adoptado) y después PCM lo registra."""
        self.Ec2._sync_from_aws(self.account, [ec2("i-7", "nueva")])
        maquina = self.Ec2._register_provisioned(self.account, ec2("i-7", "nueva"))
        self.assertEqual(maquina.management_state, "provisioned")

    # 2. Punto 8: el re-tag de client_id -----------------------------------
    def _tags(self, entorno):
        with mock.patch(SVC + ".aws_ec2.AwsEc2Service.create_tags") as crear, \
                mock.patch(SVC + ".aws_ec2.AwsEc2Service.delete_tags") as borrar:
            entorno._job_sync_client_tag()
        return crear.called or borrar.called

    def test_tag_sync_no_toca_aws_en_adoptados(self):
        entorno, _m = self._servidor("i-ado", "adopted_readonly")
        self.assertFalse(self._tags(entorno))

    def test_tag_sync_control_provisioned_si_actua(self):
        entorno, _m = self._servidor("i-pcm", "provisioned")
        self.assertTrue(self._tags(entorno))

    # 3. Punto 9: el re-etiquetado masivo -----------------------------------
    def _retag(self):
        with mock.patch(SVC + ".aws_ec2.AwsEc2Service.list_instances",
                        return_value=[ec2("i-ado", "a"), ec2("i-pcm", "p")]), \
                mock.patch.object(type(self.account), "_retag_one") as uno:
            self.account.job_retag_resources(dry_run=True)
        return {c.args[1].aws_instance_id for c in uno.call_args_list}

    def test_retag_masivo_saltea_adoptados_y_no_a_los_demas(self):
        self._servidor("i-ado", "adopted_readonly")
        self._servidor("i-pcm", "provisioned")
        tocados = self._retag()
        self.assertNotIn("i-ado", tocados)
        self.assertIn("i-pcm", tocados, "control: el provisioned sí se re-etiqueta")
