# -*- coding: utf-8 -*-
"""Acciones de UI que faltaban (2026-09-19).

El mapeo de pantallas para el manual de usuario encontró dos features sin
ningún click que llegara a ellas:

1. **Restaurar desde un dump externo (F1)**: el asistente sólo se abría desde
   "Restaurar…" de un backup del catálogo, siempre con ``backup_id``.
2. **Crear un proyecto**: la pantalla Proyectos sólo listaba.

Acá se fija el lado servidor de la acción nueva (qué abre y con qué) y que los
dos botones estén en sus pantallas llamando a lo que corresponde.
"""
from unittest import mock

from odoo.tests import Form, tagged
from odoo.tests.common import TransactionCase
from odoo.tools import file_open

URI = "s3://pcm-backups-test/incoming/bj/bj.zip"


@tagged("post_install", "-at_install", "primate_cloud")
class TestAccionesUI(TransactionCase):

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
        self.srv = self.env["primate.cloud.environment"].create({
            "name": "BJ", "project_id": self.project.id,
            "env_type": "testing", "state": "active",
        })
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "bj-srv", "account_id": self.account.id,
            "environment_id": self.srv.id, "aws_instance_id": "i-bj",
            "instance_state": "running", "region": "us-east-1",
        })
        self.inst = self.srv.primary_instance_id

    def _base(self, name):
        return self.env["primate.cloud.database"].create({
            "name": name, "account_id": self.account.id,
            "environment_id": self.srv.id, "db_type": "local_pg",
            "ec2_instance_id": self.machine.id, "instance_id": self.inst.id,
        })

    # ------------------------------------------------------------------
    # 1. Restaurar desde un dump externo
    # ------------------------------------------------------------------
    def test_accion_abre_el_asistente_sin_backup_con_destino(self):
        action = self.srv.action_restore_external_dump()
        self.assertEqual(action["type"], "ir.actions.act_window")
        self.assertEqual(action["res_model"],
                         "primate.cloud.backup.restore.wizard")
        self.assertEqual(action["target"], "new")
        ctx = action["context"]
        self.assertEqual(ctx["default_target_environment_id"], self.srv.id)
        self.assertEqual(ctx["default_origin_type"], "s3_uri")
        self.assertNotIn("default_backup_id", ctx)

    def test_base_destino_precargada_solo_si_hay_una(self):
        self.assertNotIn("default_target_db_name",
                         self.srv.action_restore_external_dump()["context"])
        self._base("barraca_julia")
        self.assertEqual(
            self.srv.action_restore_external_dump()["context"]
            ["default_target_db_name"], "barraca_julia")
        self._base("otra")
        self.assertNotIn("default_target_db_name",
                         self.srv.action_restore_external_dump()["context"])

    def test_formulario_abierto_por_la_accion(self):
        """Lo que ve el operador: destino y servidor precargados, sin backup,
        el borrado de la key y el pre-backup en sus defaults."""
        self._base("barraca_julia")
        ctx = self.srv.action_restore_external_dump()["context"]
        form = Form(self.env["primate.cloud.backup.restore.wizard"]
                    .with_context(**ctx))
        self.assertEqual(form.target_environment_id, self.srv)
        self.assertEqual(form.target_instance_id, self.machine)  # onchange
        self.assertEqual(form.target_db_name, "barraca_julia")
        self.assertEqual(form.origin_type, "s3_uri")
        self.assertFalse(form.backup_id)
        self.assertTrue(form.pre_backup)
        self.assertTrue(form.delete_source)

    def test_de_punta_a_punta_encola_el_restore_externo(self):
        self._base("barraca_julia")
        ctx = self.srv.action_restore_external_dump()["context"]
        form = Form(self.env["primate.cloud.backup.restore.wizard"]
                    .with_context(**ctx))
        form.origin_uri = URI
        form.delete_source = False
        wizard = form.save()
        with mock.patch.object(type(self.srv), "with_delay") as delay:
            wizard.action_restore()
        params = delay.return_value.job_restore_backup.call_args[0][0]
        self.assertEqual(params["origin"],
                         {"type": "s3_uri", "uri": URI, "format": "auto"})
        self.assertFalse(params["delete_source"])
        self.assertNotIn("backup_id", params)
        self.assertEqual(params["db_name"], "barraca_julia")
        self.assertEqual(params["instance_id"], self.machine.id)

    def test_boton_en_respaldos_del_entorno(self):
        with file_open("primate_cloud_manager/static/src/app/screens/"
                       "entorno_detalle.xml") as fh:
            xml = fh.read()
        respaldos = xml[xml.index("<h2>Respaldos</h2>"):]
        respaldos = respaldos[:respaldos.index("</div>\n                </div>")]
        self.assertIn("action_restore_external_dump", respaldos)
        self.assertIn("Restaurar desde un dump…", respaldos)

    # ------------------------------------------------------------------
    # 2. Nuevo proyecto
    # ------------------------------------------------------------------
    def test_boton_nuevo_proyecto(self):
        with file_open("primate_cloud_manager/static/src/app/screens/"
                       "proyectos.xml") as fh:
            xml = fh.read()
        with file_open("primate_cloud_manager/static/src/app/screens/"
                       "proyectos.js") as fh:
            js = fh.read()
        self.assertIn("Nuevo proyecto", xml)
        self.assertIn("this.newProject()", xml)
        self.assertIn('newRecord("primate.cloud.project"', js)

    def test_el_formulario_de_proyecto_crea_con_lo_minimo(self):
        """Lo que el botón abre: el formulario nativo del proyecto. Con nombre
        y cliente alcanza (lo mismo que pide el manual)."""
        form = Form(self.env["primate.cloud.project"])
        form.name = "Cliente nuevo"
        form.partner_id = self.partner
        proyecto = form.save()
        self.assertEqual(proyecto.partner_id, self.partner)


@tagged("post_install", "-at_install", "primate_cloud")
class TestBundleScss(TransactionCase):
    """El SCSS de la app COMPILA (2026-09-19).

    Un error de compilación del SCSS no rompe la instalación ni ningún test: el
    servidor lo deja como WARNING en el log y sirve el bundle sin las reglas
    nuevas (o con el CSS anterior). Pasó con un ``min(920px, 60vw)`` que libsass
    toma como función propia: el bundle ENTERO dejó de actualizarse y nada
    falló. Este test compila el bundle de verdad y exige cero errores.
    """

    def test_bundle_web_compila_sin_errores(self):
        bundle = self.env["ir.qweb"]._get_asset_bundle(
            "web.assets_web", js=False)
        css = bundle.preprocess_css()
        self.assertFalse(bundle.css_errors,
                         "El SCSS del bundle no compila:\n%s"
                         % "\n".join(bundle.css_errors))
        # Y lo compilado trae las reglas de la app (no un bundle vacío o viejo).
        self.assertIn("o_pcm_drawer.modal", css)
        self.assertIn("o_pcm_native", css)
