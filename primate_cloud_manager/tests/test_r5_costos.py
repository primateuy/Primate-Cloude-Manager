# -*- coding: utf-8 -*-
"""R5 — reparto de costos.

B1 (fundación): inmutabilidad de ``cost.share`` con escape de regeneración,
captura de ``hosted_until`` al archivar, prerequisito ``project.partner_id``
required + su migración.
B2 (motor): splitter al centavo con residuo determinístico, prorrateo por días,
servidor sin instancias (Q2), regeneración idempotente, método 'usage'
deshabilitado.
"""
import importlib.util
import os
from datetime import date
from unittest import mock

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger

from odoo import fields

from ..services import aws_ec2


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


@tagged("post_install", "-at_install", "primate_cloud")
class TestR5B2Motor(TransactionCase):
    """Motor de reparto: splitter al centavo, prorrateo por días, Q2 e idempotencia."""

    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create({"name": "Cliente R5-B2"})
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Proyecto R5-B2", "account_id": self.account.id,
             "partner_id": self.partner.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "srv-b2", "project_id": self.project.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        self.p_start = date(2026, 1, 1)
        self.p_end = date(2026, 2, 1)   # exclusivo → enero = 31 días
        self.Share = self.env["primate.cloud.cost.share"]

    # --- helpers -------------------------------------------------------
    def _entry(self, amount, service="AmazonEC2"):
        return self.env["primate.cloud.cost.entry"].create({
            "account_id": self.account.id, "period_start": self.p_start,
            "period_end": self.p_end, "granularity": "monthly",
            "environment_ref": self.server.pcm_ref, "environment_name": "srv-b2",
            "service": service, "amount": amount})

    def _set_created(self, inst, dt):
        """create_date es magic/readonly: se fuerza por SQL (y se INVALIDA la
        caché ORM, si no el prorrateo ve la fecha vieja) para controlar el
        solapamiento del período (hosted_from = create_date)."""
        self.env.cr.execute(
            "UPDATE primate_cloud_instance SET create_date=%s WHERE id=%s",
            (dt, inst.id))
        inst.invalidate_recordset(["create_date"])

    def _instance(self, name, created=None, until=None, weight=1.0):
        # Vía _create_instance_with_ports: asigna el slot de puertos (crear
        # directo choca con el unique (environment_id, http_port) de la primaria).
        inst = self.server._create_instance_with_ports(
            {"name": name, "project_id": self.project.id})
        inst.cost_weight = weight
        if created is not None:
            self._set_created(inst, created)
        if until is not None:
            inst.hosted_until = until
        return inst

    def _shares(self):
        return self.Share.search([("environment_id", "=", self.server.id),
                                  ("period_start", "=", self.p_start)])

    # --- splitter puro -------------------------------------------------
    def test_split_cents_equal_invariante_y_residuo(self):
        out = self.Share._split_cents(10.0, [1, 1, 1])
        self.assertEqual(sum(out), 10.0)              # invariante al centavo
        self.assertEqual(sorted(out), [3.33, 3.33, 3.34])
        # residuo determinístico: el centavo va al PRIMERO del orden recibido.
        self.assertEqual(out[0], 3.34)

    def test_split_cents_por_peso(self):
        out = self.Share._split_cents(10.0, [2, 1, 1])
        self.assertEqual(out, [5.0, 2.5, 2.5])

    def test_split_cents_numero_feo_cuadra_exacto(self):
        out = self.Share._split_cents(100.0, [1] * 7)
        self.assertEqual(round(sum(out), 2), 100.0)   # cuadra al centavo
        self.assertEqual(len(out), 7)

    def test_split_cents_sin_pesos_vacio(self):
        self.assertEqual(self.Share._split_cents(10.0, []), [])

    # --- overlap de días ----------------------------------------------
    def test_hosted_days_overlap(self):
        # Instancia creada el 16/ene, viva todo el resto → 16 días (16..31).
        inst = self._instance("A", created="2026-01-16 00:00:00")
        self.assertEqual(inst._hosted_days_in_period(self.p_start, self.p_end), 16)
        # Archivada el 11/ene → 10 días (1..10).
        inst2 = self._instance("B", created="2025-12-01 00:00:00",
                               until="2026-01-11 00:00:00")
        self.assertEqual(inst2._hosted_days_in_period(self.p_start, self.p_end), 10)
        # Creada después del período → 0.
        inst3 = self._instance("C", created="2026-03-01 00:00:00")
        self.assertEqual(inst3._hosted_days_in_period(self.p_start, self.p_end), 0)

    # --- regeneración: equal, invariante ------------------------------
    def test_regenera_equal_invariante_al_centavo(self):
        # 2 instancias permanentes (la primaria + una más), total feo.
        self.server.primary_instance_id.write({"name": "prim"})
        self._set_created(self.server.primary_instance_id, "2025-12-01 00:00:00")
        self._instance("segunda", created="2025-12-01 00:00:00")
        self._entry(10.01)
        self.account._regenerate_cost_shares(self.p_start, self.p_end, "monthly")
        shares = self._shares()
        self.assertEqual(len(shares), 2)
        self.assertEqual(round(sum(shares.mapped("amount")), 2), 10.01)  # invariante 2

    # --- regeneración: prorrateo por días -----------------------------
    def test_regenera_prorratea_por_dias(self):
        # A permanente (31 días), B solo la 1ª mitad (archivada el 16 → 15 días).
        self._set_created(self.server.primary_instance_id, "2025-12-01 00:00:00")
        self.server.primary_instance_id.write({"name": "A"})
        self._instance("B", created="2025-12-01 00:00:00",
                       until="2026-01-16 00:00:00")   # 15 días
        self._entry(46.0)   # 46 / (31+15=46) = $1/día → A=31, B=15
        self.account._regenerate_cost_shares(self.p_start, self.p_end, "monthly")
        by_name = {s.instance_name: s.amount for s in self._shares()}
        self.assertEqual(round(sum(by_name.values()), 2), 46.0)  # invariante
        self.assertEqual(by_name["A"], 31.0)
        self.assertEqual(by_name["B"], 15.0)

    # --- Q2: servidor sin instancias en el período --------------------
    def test_regenera_servidor_sin_instancias_unattributed(self):
        # La primaria "nace" después del período → 0 días → nada que atribuir.
        self.env.cr.execute(
            "UPDATE primate_cloud_instance SET create_date=%s WHERE id=%s",
            ("2026-03-01 00:00:00", self.server.primary_instance_id.id))
        self.server.primary_instance_id.invalidate_recordset(["create_date"])
        self._entry(30.0)
        self.account._regenerate_cost_shares(self.p_start, self.p_end, "monthly")
        shares = self._shares()
        self.assertEqual(len(shares), 1)
        self.assertTrue(shares.unattributed)
        self.assertFalse(shares.instance_id)
        self.assertEqual(shares.amount, 30.0)   # invariante: no se pierde plata

    # --- idempotencia --------------------------------------------------
    def test_regenera_idempotente(self):
        self._set_created(self.server.primary_instance_id, "2025-12-01 00:00:00")
        self._instance("segunda", created="2025-12-01 00:00:00")
        self._entry(10.0)
        self.account._regenerate_cost_shares(self.p_start, self.p_end, "monthly")
        primera = {s.instance_name: s.amount for s in self._shares()}
        # Segunda corrida: mismo estado → mismas shares (borra+recrea).
        self.account._regenerate_cost_shares(self.p_start, self.p_end, "monthly")
        segunda = {s.instance_name: s.amount for s in self._shares()}
        self.assertEqual(primera, segunda)
        self.assertEqual(len(self._shares()), 2)

    # --- usage deshabilitado ------------------------------------------
    def test_usage_deshabilitado_no_se_puede_guardar(self):
        with self.assertRaises(ValidationError):
            self.server.cost_split_method = "usage"


@tagged("post_install", "-at_install", "primate_cloud")
class TestR5B3Retag(TransactionCase):
    """Re-tag dedicado/compartido: quién queda con primate:client_id."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner_x = self.env["res.partner"].create({"name": "Cliente X"})
        self.partner_y = self.env["res.partner"].create({"name": "Cliente Y"})
        self.sentinel = self.env["res.partner"].create(
            {"name": "⚠ SIN CLIENTE (asignar)"})
        self.env["ir.config_parameter"].sudo().set_param(
            "pcm.unassigned_partner_id", str(self.sentinel.id))
        self.proj_x = self.env["primate.cloud.project"].create(
            {"name": "PX", "account_id": self.account.id,
             "partner_id": self.partner_x.id})
        self.proj_y = self.env["primate.cloud.project"].create(
            {"name": "PY", "account_id": self.account.id,
             "partner_id": self.partner_y.id})
        self.proj_sent = self.env["primate.cloud.project"].create(
            {"name": "PS", "account_id": self.account.id,
             "partner_id": self.sentinel.id})
        # Servidor de X (su primaria nace en proj_x) + máquina EC2.
        self.server = self.env["primate.cloud.environment"].create({
            "name": "srv", "project_id": self.proj_x.id,
            "env_type": "production", "odoo_version": "19",
            "odoo_edition": "community",
        })
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m", "account_id": self.account.id, "aws_instance_id": "i-1",
            "instance_state": "running", "region": "us-east-1",
            "environment_id": self.server.id, "provisioned_by_pcm": True,
        })
        self.server.ec2_instance_id = self.machine

    def _add(self, project, name):
        return self.server._create_instance_with_ports(
            {"name": name, "project_id": project.id})

    # --- _dedicated_client_partner (cálculo puro) ----------------------
    def test_dedicado_un_solo_cliente(self):
        self.assertEqual(self.server._dedicated_client_partner(), self.partner_x)

    def test_compartido_dos_clientes_no_dedicado(self):
        self._add(self.proj_y, "B")
        self.assertFalse(self.server._dedicated_client_partner())

    def test_todas_centinela_no_dedicado(self):
        self.server.primary_instance_id.project_id = self.proj_sent
        self._add(self.proj_sent, "B")
        self.assertFalse(self.server._dedicated_client_partner())

    def test_cliente_real_mas_centinela_no_dedicado(self):
        # X + una sin asignar: taggear X sobre-atribuiría → NO dedicado.
        self._add(self.proj_sent, "sinasignar")
        self.assertFalse(self.server._dedicated_client_partner())

    def test_archivar_el_otro_cliente_devuelve_dedicado(self):
        # Requisito 1: archivar cambia la condición (compartido → dedicado).
        b = self._add(self.proj_y, "B")
        self.assertFalse(self.server._dedicated_client_partner())   # compartido
        b.write({"state": "archived"})
        self.assertEqual(self.server._dedicated_client_partner(),   # dedicado a X
                         self.partner_x)

    # --- trigger en el choke point (add + archive) ---------------------
    def test_agregar_instancia_encola_retag(self):
        with mock.patch.object(type(self.server),
                               "_enqueue_client_tag_sync") as enq:
            self._add(self.proj_y, "B")
        enq.assert_called()

    def test_archivar_encola_retag(self):
        b = self._add(self.proj_y, "B")
        with mock.patch.object(type(self.server),
                               "_enqueue_client_tag_sync") as enq:
            b.write({"state": "archived"})
        enq.assert_called()

    # --- _job_sync_client_tag (AWS mockeado) ---------------------------
    def _patch_ec2(self, fake):
        return (mock.patch.object(type(self.account), "_get_aws_service",
                                  return_value=mock.Mock()),
                mock.patch.object(aws_ec2, "AwsEc2Service", return_value=fake))

    def test_job_dedicado_pone_client_id(self):
        fake = mock.Mock()
        p1, p2 = self._patch_ec2(fake)
        with p1, p2:
            self.server._job_sync_client_tag()
        fake.create_tags.assert_called_once()
        tags = {t["Key"]: t["Value"] for t in fake.create_tags.call_args[0][1]}
        self.assertIn("primate:client_id", tags)
        fake.delete_tags.assert_not_called()

    def test_job_compartido_quita_client_id(self):
        self._add(self.proj_y, "B")
        fake = mock.Mock()
        p1, p2 = self._patch_ec2(fake)
        with p1, p2:
            self.server._job_sync_client_tag()
        fake.delete_tags.assert_called_once()
        keys = fake.delete_tags.call_args[0][1]
        self.assertIn("primate:client_id", keys)
        fake.create_tags.assert_not_called()

    def test_job_no_rompe_si_aws_falla(self):
        # Requisito 3: si el tag falla (permiso, etc.) NO rompe la operación.
        fake = mock.Mock()
        fake.create_tags.side_effect = Exception("AccessDenied")
        p1, p2 = self._patch_ec2(fake)
        with p1, p2, mute_logger(
                "odoo.addons.primate_cloud_manager.models.primate_cloud_environment"):
            self.server._job_sync_client_tag()   # no debe levantar

    def test_job_maquina_terminada_no_op(self):
        self.machine.instance_state = "terminated"
        fake = mock.Mock()
        p1, p2 = self._patch_ec2(fake)
        with p1, p2:
            self.server._job_sync_client_tag()
        fake.create_tags.assert_not_called()
        fake.delete_tags.assert_not_called()


@tagged("post_install", "-at_install", "primate_cloud")
class TestR6ProyectoAxis(TransactionCase):
    """Eje proyecto (R6-B1): get_project_detail / get_projects — la vista del cliente."""

    def setUp(self):
        super().setUp()
        self.dash = self.env["primate.cloud.dashboard"]
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Cliente R6"})
        self.other = self.env["res.partner"].create({"name": "Otro Cliente"})
        self.sentinel = self.env["res.partner"].create(
            {"name": "⚠ SIN CLIENTE (asignar)"})
        self.env["ir.config_parameter"].sudo().set_param(
            "pcm.unassigned_partner_id", str(self.sentinel.id))
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Proyecto R6", "account_id": self.account.id,
             "partner_id": self.partner.id})
        self.other_project = self.env["primate.cloud.project"].create(
            {"name": "Otro", "account_id": self.account.id,
             "partner_id": self.other.id})
        self.sent_project = self.env["primate.cloud.project"].create(
            {"name": "Bandeja", "account_id": self.account.id,
             "partner_id": self.sentinel.id})
        # Servidor A dedicado al cliente; Servidor B compartido (cliente + otro).
        self.srv_a = self.env["primate.cloud.environment"].create({
            "name": "A", "project_id": self.project.id, "env_type": "production",
            "odoo_version": "19", "odoo_edition": "community"})
        self.srv_b = self.env["primate.cloud.environment"].create({
            "name": "B", "project_id": self.project.id, "env_type": "production",
            "odoo_version": "19", "odoo_edition": "community"})
        self.inst_a = self.srv_a.primary_instance_id
        self.inst_b = self.srv_b.primary_instance_id
        # instancia de OTRO cliente en B → B compartido
        self.srv_b._create_instance_with_ports(
            {"name": "OtroOdoo", "project_id": self.other_project.id})
        self.month_start = fields.Date.context_today(self.dash).replace(day=1)

    def _share(self, server, instance, amount):
        return self.env["primate.cloud.cost.share"].create({
            "account_id": self.account.id,
            "period_start": self.month_start,
            "period_end": self.month_start,   # irrelevante para la vista
            "granularity": "monthly",
            "environment_id": server.id, "environment_ref": server.pcm_ref,
            "environment_name": server.name,
            "instance_id": instance.id, "instance_ref": instance.pcm_ref,
            "instance_name": instance.name,
            "project_id": self.project.id, "partner_id": self.partner.id,
            "method": "equal", "amount": amount,
        })

    def test_detail_instancias_cruzan_servidores(self):
        d = self.dash.get_project_detail(self.project.id)
        names = {i["name"]: i for i in d["instances"]}
        # las instancias del cliente aparecen con SU servidor (cruce de ejes)
        self.assertIn(self.inst_a.display_name, names)
        self.assertIn(self.inst_b.display_name, names)
        self.assertEqual(names[self.inst_a.display_name]["server_name"], "A")
        self.assertEqual(names[self.inst_b.display_name]["server_name"], "B")

    def test_detail_total_suma_las_mismas_shares(self):
        # Precisión (a): total = suma de las líneas listadas (mismo recordset),
        # no un cálculo aparte. 3.33+3.33+3.34 = 10.00.
        self._share(self.srv_a, self.inst_a, 3.33)
        self._share(self.srv_b, self.inst_b, 3.34)
        # una 3ª share sobre A (otra instancia del cliente para sumar)
        extra = self.srv_a._create_instance_with_ports(
            {"name": "Extra", "project_id": self.project.id})
        self._share(self.srv_a, extra, 3.33)
        d = self.dash.get_project_detail(self.project.id)
        suma_lineas = round(sum(cl["amount"] for cl in d["cost_lines"]), 2)
        self.assertEqual(round(d["cost_total"], 2), suma_lineas)
        self.assertEqual(round(d["cost_total"], 2), 10.0)

    def test_detail_marca_shared_server(self):
        self._share(self.srv_a, self.inst_a, 5.0)   # A dedicado
        self._share(self.srv_b, self.inst_b, 5.0)   # B compartido
        d = self.dash.get_project_detail(self.project.id)
        by_srv = {cl["server_name"]: cl for cl in d["cost_lines"]}
        self.assertFalse(by_srv["A"]["shared_server"])
        self.assertTrue(by_srv["B"]["shared_server"])

    def test_detail_marca_staging(self):
        self.inst_b.env_type = "staging"
        d = self.dash.get_project_detail(self.project.id)
        names = {i["name"]: i for i in d["instances"]}
        self.assertTrue(names[self.inst_b.display_name]["is_staging"])

    def test_detail_honestidad_datos_al(self):
        # sin pull → "datos al" vacío; con pull → fecha presente
        d = self.dash.get_project_detail(self.project.id)
        self.assertEqual(d["cost_pulled_at"], "")
        self.account.cost_pulled_at = fields.Datetime.now()
        d2 = self.dash.get_project_detail(self.project.id)
        self.assertTrue(d2["cost_pulled_at"])

    def test_projects_centinela_flag_y_ultimo(self):
        out = self.dash.get_projects()
        by_id = {p["id"]: p for p in out}
        self.assertTrue(by_id[self.sent_project.id]["is_unassigned"])
        self.assertFalse(by_id[self.project.id]["is_unassigned"])
        # el centinela va al final
        self.assertTrue(out[-1]["is_unassigned"])

    def test_projects_cuenta_instancias(self):
        out = self.dash.get_projects()
        proj = next(p for p in out if p["id"] == self.project.id)
        # A (primary) + B (primary) + Extra? no; solo primarias A y B del cliente
        self.assertEqual(proj["instance_count"], 2)


@tagged("post_install", "-at_install", "primate_cloud")
class TestR6Crossing(TransactionCase):
    """El cruce (R6-B2): porción de costo en la instancia + crudo/reparto en el servidor."""

    def setUp(self):
        super().setUp()
        self.dash = self.env["primate.cloud.dashboard"]
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Cliente R6X"})
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Proyecto R6X", "account_id": self.account.id,
             "partner_id": self.partner.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "srv", "project_id": self.project.id, "env_type": "production",
            "odoo_version": "19", "odoo_edition": "community"})
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m", "account_id": self.account.id, "aws_instance_id": "i-1",
            "instance_state": "running", "region": "us-east-1",
            "environment_id": self.server.id, "provisioned_by_pcm": True})
        self.server.ec2_instance_id = self.machine
        self.inst = self.server.primary_instance_id
        self.month_start = fields.Date.context_today(self.dash).replace(day=1)

    def _entry(self, amount):
        return self.env["primate.cloud.cost.entry"].create({
            "account_id": self.account.id, "period_start": self.month_start,
            "period_end": self.month_start, "granularity": "monthly",
            "environment_ref": self.server.pcm_ref, "environment_name": "srv",
            "service": "AmazonEC2", "amount": amount})

    def _share(self, amount):
        return self.env["primate.cloud.cost.share"].create({
            "account_id": self.account.id, "period_start": self.month_start,
            "period_end": self.month_start, "granularity": "monthly",
            "environment_id": self.server.id, "environment_ref": self.server.pcm_ref,
            "environment_name": "srv",
            "instance_id": self.inst.id, "instance_ref": self.inst.pcm_ref,
            "instance_name": self.inst.name,
            "project_id": self.project.id, "partner_id": self.partner.id,
            "client_name": self.partner.name, "method": "equal", "amount": amount})

    # --- instancia: su porción, con contexto (precisión 2) -------------
    def test_instance_detail_porcion_con_contexto(self):
        self._share(10.0)
        self.account.cost_pulled_at = fields.Datetime.now()
        d = self.dash.get_odoo_instance_detail(self.inst.id)
        self.assertTrue(d["cost"])
        self.assertEqual(d["cost"]["amount"], 10.0)
        self.assertTrue(d["cost"]["method_label"])      # método presente
        self.assertTrue(d["cost"]["pulled_at"])         # "datos al" presente
        self.assertFalse(d["cost"]["shared_server"])    # servidor dedicado

    def test_instance_detail_env_type_es_de_la_instancia(self):
        # Precisión 1: el env_type sale de la INSTANCIA, no del servidor.
        self.inst.env_type = "staging"
        d = self.dash.get_odoo_instance_detail(self.inst.id)
        self.assertEqual(d["env_type"], "staging")
        self.assertFalse(d["is_production"])

    # --- servidor: crudo (máquina) vs reparto (precisión 3) ------------
    def test_server_detail_crudo_es_la_maquina(self):
        self._entry(30.0)          # AWS factura $30 por la máquina
        self._share(30.0)          # se reparte (una instancia acá → todo a ella)
        self.account.cost_pulled_at = fields.Datetime.now()
        d = self.dash.get_server_detail(self.machine.id)
        self.assertTrue(d["cost"]["has_data"])
        self.assertEqual(d["cost"]["crudo"], 30.0)              # la máquina entera
        self.assertEqual(d["cost"]["shares_total"], 30.0)      # = repartido (invariante R5)
        self.assertEqual(len(d["cost"]["shares"]), 1)
        self.assertTrue(d["cost"]["pulled_at"])

    def test_server_detail_sin_costo_no_rompe(self):
        d = self.dash.get_server_detail(self.machine.id)
        self.assertFalse(d["cost"]["has_data"])
        self.assertEqual(d["cost"]["crudo"], 0.0)
