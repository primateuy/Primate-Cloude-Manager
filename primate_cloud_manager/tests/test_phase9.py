# -*- coding: utf-8 -*-
"""Tests de Fase 9 — Bloque 1, paso (a): identificador estable pcm_ref.

Opción A: clave de atribución de costos inmutable. `environment.pcm_ref` se
genera por registro en create; `res.partner.pcm_ref` se genera lazy y NO se
pisa. El backfill (migración) llena solo vacíos — se ejercita su lógica acá.
"""
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "primate_cloud")
class TestPcmRefPhase9(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.partner = self.env["res.partner"].create({"name": "Cliente X"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Forum", "account_id": self.account.id,
            "partner_id": self.partner.id,
        })

    def _env(self, name="E", **vals):
        base = {"name": name, "project_id": self.project.id,
                "env_type": "production", "state": "active"}
        base.update(vals)
        return self.env["primate.cloud.environment"].create(base)

    # --- environment.pcm_ref ---
    def test_env_genera_ref_en_create(self):
        env = self._env()
        self.assertTrue(env.pcm_ref)
        self.assertTrue(env.pcm_ref.startswith("pcm_env_"))

    def test_env_refs_unicos_en_lote(self):
        """Create en LOTE: cada registro obtiene un ref distinto (no colisiona
        con la constraint de unicidad, que es el riesgo de un default lambda)."""
        envs = self.env["primate.cloud.environment"].create([
            {"name": "A", "project_id": self.project.id, "env_type": "testing",
             "state": "active"},
            {"name": "B", "project_id": self.project.id, "env_type": "testing",
             "state": "active"},
            {"name": "D", "project_id": self.project.id, "env_type": "testing",
             "state": "active"},
        ])
        refs = envs.mapped("pcm_ref")
        self.assertEqual(len(set(refs)), 3)
        self.assertTrue(all(r for r in refs))

    def test_env_ref_inmutable_al_renombrar(self):
        env = self._env()
        ref = env.pcm_ref
        env.name = "Nombre Nuevo"
        self.assertEqual(env.pcm_ref, ref)  # no cambia al renombrar

    def test_env_ref_explicito_se_respeta(self):
        env = self._env(pcm_ref="pcm_env_fijo")
        self.assertEqual(env.pcm_ref, "pcm_env_fijo")

    def test_env_ref_no_se_copia(self):
        """copy=False: un duplicado NO hereda el ref (lo generaría nuevo)."""
        env = self._env()
        clon = env.copy({"name": "Clon"})
        self.assertTrue(clon.pcm_ref)
        self.assertNotEqual(clon.pcm_ref, env.pcm_ref)

    # --- res.partner.pcm_ref (lazy, no pisa) ---
    def test_partner_ref_lazy_no_default(self):
        """Un partner nuevo NO recibe ref por default (solo al pedirlo)."""
        p = self.env["res.partner"].create({"name": "Contacto suelto"})
        self.assertFalse(p.pcm_ref)

    def test_partner_ensure_genera_y_es_idempotente(self):
        ref1 = self.partner._ensure_pcm_ref()
        self.assertTrue(ref1.startswith("pcm_cli_"))
        ref2 = self.partner._ensure_pcm_ref()
        self.assertEqual(ref1, ref2)  # no pisa

    def test_partner_ensure_no_pisa_existente(self):
        self.partner.sudo().pcm_ref = "pcm_cli_preexistente"
        self.assertEqual(self.partner._ensure_pcm_ref(), "pcm_cli_preexistente")

    # --- Lógica del backfill (solo llena vacíos) ---
    def test_backfill_llena_solo_vacios(self):
        """Replica el WHERE de la migración: un entorno sin ref se llena, uno
        con ref NO se toca."""
        con_ref = self._env(name="ConRef", pcm_ref="pcm_env_yatengo")
        # Simular un registro legado sin ref (bypass del create) por SQL.
        sin_ref = self._env(name="SinRef")
        self.env.cr.execute(
            "UPDATE primate_cloud_environment SET pcm_ref = NULL WHERE id = %s",
            (sin_ref.id,))
        sin_ref.invalidate_recordset()
        # El WHERE de la migración selecciona SOLO el vacío.
        self.env.cr.execute(
            "SELECT id FROM primate_cloud_environment "
            "WHERE (pcm_ref IS NULL OR pcm_ref = '') AND id IN %s",
            (tuple([con_ref.id, sin_ref.id]),))
        ids = [r[0] for r in self.env.cr.fetchall()]
        self.assertEqual(ids, [sin_ref.id])
        self.assertEqual(con_ref.pcm_ref, "pcm_env_yatengo")  # intacto
