# -*- coding: utf-8 -*-
"""R5-B1: fundación del reparto de costos.

Cubre lo que NO es el motor de reparto (eso es B2): la inmutabilidad de
``cost.share`` con su escape de regeneración, la captura de ``hosted_until`` al
archivar (dato del prorrateo, gratis ahora / irreconstruible después) y el
prerequisito ``project.partner_id`` required.
"""
import importlib.util
import os

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger

from odoo import fields


@tagged("post_install", "-at_install", "primate_cloud")
class TestR5B1Fundacion(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create({"name": "Cliente R5"})
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Proyecto R5", "account_id": self.account.id,
             "partner_id": self.partner.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "srv-r5", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        self.instance = self.server.primary_instance_id

    # --- project.partner_id required (prerequisito) ---------------------
    def test_project_partner_required(self):
        # El NOT NULL de required es un IntegrityError de psycopg2 que envenena
        # el cursor: el savepoint lo aísla para que el rollback lo recupere.
        with self.assertRaises(Exception), \
                mute_logger("odoo.sql_db"), self.env.cr.savepoint():
            self.env["primate.cloud.project"].create(
                {"name": "Sin cliente", "account_id": self.account.id})

    # --- hosted_until: captura en el choke point write ------------------
    def test_hosted_until_se_estampa_al_archivar(self):
        self.assertFalse(self.instance.hosted_until)
        self.instance.state = "active"
        self.assertFalse(self.instance.hosted_until)
        self.instance.write({"state": "archived"})
        self.assertTrue(self.instance.hosted_until, "debe estamparse al archivar")

    def test_hosted_until_idempotente_no_pisa_la_fecha_real(self):
        self.instance.write({"state": "archived"})
        primera = self.instance.hosted_until
        self.assertTrue(primera)
        # Re-archivar NO debe pisar la fecha real de archivado.
        self.instance.write({"state": "archived", "name": "otro nombre"})
        self.assertEqual(self.instance.hosted_until, primera)

    def test_hosted_until_se_limpia_al_reactivar(self):
        self.instance.write({"state": "archived"})
        self.assertTrue(self.instance.hosted_until)
        self.instance.write({"state": "active"})
        self.assertFalse(self.instance.hosted_until,
                         "reactivar limpia el sello")

    def test_hosted_until_explicito_manda(self):
        # Un hosted_until explícito en el mismo write no se pisa.
        momento = fields.Datetime.to_datetime("2026-01-15 10:00:00")
        self.instance.write({"state": "archived", "hosted_until": momento})
        self.assertEqual(self.instance.hosted_until, momento)

    # --- cost.share: inmutable con escape de regeneración ---------------
    def _make_share(self):
        return self.env["primate.cloud.cost.share"].create({
            "account_id": self.account.id,
            "period_start": "2026-01-01", "period_end": "2026-02-01",
            "environment_id": self.server.id,
            "environment_ref": self.server.pcm_ref,
            "instance_id": self.instance.id,
            "instance_ref": self.instance.pcm_ref,
            "amount": 10.0, "method": "equal",
        })

    def test_cost_share_write_bloqueado_desde_ui(self):
        share = self._make_share()
        with self.assertRaises(UserError):
            share.write({"amount": 999.0})

    def test_cost_share_unlink_bloqueado_desde_ui(self):
        share = self._make_share()
        with self.assertRaises(UserError):
            share.unlink()

    def test_cost_share_regeneracion_con_context_si_puede(self):
        share = self._make_share()
        # El motor de regeneración (context pcm_cost_regen) SÍ reescribe/borra.
        share.with_context(pcm_cost_regen=True).write({"amount": 5.0})
        self.assertEqual(share.amount, 5.0)
        share.with_context(pcm_cost_regen=True).unlink()
        self.assertFalse(share.exists())


@tagged("post_install", "-at_install", "primate_cloud")
class TestR5B1Migracion(TransactionCase):
    """Integración del pre-migrate de R5 (hermano del 'is_alias does not exist').

    El ``partner_id`` required con ``NOT NULL`` se rompe en el ``-u`` de una base
    REAL con proyectos huérfanos, NO en los tests normales (que nunca tienen
    huérfanos). El pre-migrate solo se ejercita actualizando una base que TIENE
    el problema. Este test carga el pre-migrate.py REAL y lo corre contra un
    huérfano simulado (partner en NULL por SQL, como estaría una base pre-R5).
    """

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Cliente Real"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "P", "account_id": self.account.id,
             "partner_id": self.partner.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "srv", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        # Aislamiento: limpiar el param por si otro test lo dejó seteado.
        self.env["ir.config_parameter"].sudo().set_param(
            "pcm.unassigned_partner_id", "")

    def _load_premigrate(self):
        path = os.path.join(os.path.dirname(__file__), "..", "migrations",
                            "19.0.1.2.0", "pre-migrate.py")
        spec = importlib.util.spec_from_file_location("pcm_r5_premigrate", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _make_orphan(self):
        """Deja el proyecto SIN partner por SQL (lo que required=True prohíbe por
        ORM) — el estado exacto de una base pre-R5 que el pre-migrate debe sanar.

        En pcm_test el NOT NULL de R5 YA está aplicado, así que primero se
        dropea (DDL transaccional: el rollback de TransactionCase lo restaura al
        cerrar el test) para poder reproducir el estado pre-migración.
        """
        self.env.cr.execute(
            "ALTER TABLE primate_cloud_project "
            "ALTER COLUMN partner_id DROP NOT NULL")
        self.env.cr.execute(
            "UPDATE primate_cloud_project SET partner_id = NULL WHERE id = %s",
            (self.project.id,))
        self.project.invalidate_recordset(["partner_id"])

    def test_premigrate_backfillea_al_centinela_visible(self):
        self._make_orphan()
        self._load_premigrate().migrate(self.env.cr, "19.0.1.1.0")
        self.project.invalidate_recordset(["partner_id"])
        sentinel = self.project.partner_id
        self.assertTrue(sentinel, "el proyecto huérfano debe quedar con partner")
        # VISIBLE y marcado (condición de R1): NO un placeholder que parezca
        # cliente real.
        self.assertTrue(sentinel.name.startswith("⚠"))
        self.assertIn("SIN CLIENTE", sentinel.name)
        # Registrado en el param rename-proof.
        param = self.env["ir.config_parameter"].sudo().get_param(
            "pcm.unassigned_partner_id")
        self.assertEqual(int(param or 0), sentinel.id)

    def test_premigrate_idempotente(self):
        self._make_orphan()
        mod = self._load_premigrate()
        mod.migrate(self.env.cr, "19.0.1.1.0")
        self.project.invalidate_recordset(["partner_id"])
        primero = self.project.partner_id
        # Segunda corrida: ya no hay huérfanos → no crea un segundo centinela.
        mod.migrate(self.env.cr, "19.0.1.1.0")
        self.assertEqual(self.env["res.partner"].search_count(
            [("name", "=", primero.name)]), 1)

    def test_premigrate_con_instancia_viva_no_emite_client_id_falso(self):
        """Caso extra: proyecto huérfano CON instancia viva. Tras el backfill,
        _cost_attribution_refs NO emite primate:client_id para el centinela: el
        'sin asignar' no es un cliente y CE no debe agruparlo como uno."""
        self.server.primary_instance_id.state = "active"
        self._make_orphan()
        self._load_premigrate().migrate(self.env.cr, "19.0.1.1.0")
        self.project.invalidate_recordset(["partner_id"])
        env_ref, client_ref = self.server._cost_attribution_refs()
        self.assertEqual(env_ref, self.server.pcm_ref)
        self.assertFalse(client_ref,
                         "no se emite client_id para el partner centinela")
