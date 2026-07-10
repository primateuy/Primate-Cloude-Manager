# -*- coding: utf-8 -*-
"""Tests del recableo R1 (D6): instancia de dos padres + delegación compat.

Cubren la mecánica NUEVA del modelo: la instancia como fuente de verdad de la
identidad del Odoo, el entorno que nace con su instancia primaria y la delega
(related store write-through), y la auto-resolución del vínculo en los
satélites. Los flujos NO cambian en R1 (lo verifica el resto de la suite).
"""
from psycopg2 import errors as pg_errors

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger


class TestR1Modelo(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.project = self.env["primate.cloud.project"].create(
            {"name": "Cliente X", "account_id": self.account.id}
        )

    def _new_env(self, **extra):
        vals = {"name": "Srv", "project_id": self.project.id,
                "env_type": "testing", "odoo_version": "19",
                "odoo_edition": "community", "main_url": "x.primate.cloud"}
        vals.update(extra)
        return self.env["primate.cloud.environment"].create(vals)

    # --- nacimiento entorno + instancia -------------------------------
    def test_env_create_auto_crea_instancia_primaria(self):
        env_rec = self._new_env()
        self.assertEqual(len(env_rec.instance_ids), 1)
        instance = env_rec.primary_instance_id
        self.assertEqual(instance, env_rec.instance_ids)
        # La identidad del Odoo vive EN la instancia (los dos ejes puestos).
        self.assertEqual(instance.project_id, self.project)
        self.assertEqual(instance.environment_id, env_rec)
        self.assertEqual(instance.env_type, "testing")
        self.assertEqual(instance.odoo_version, "19")
        self.assertEqual(instance.main_url, "x.primate.cloud")
        self.assertTrue(instance.slug)
        self.assertTrue(instance.pcm_ref.startswith("pcm_inst_"))
        # ... y el entorno la DELEGA (misma lectura por compat).
        self.assertEqual(env_rec.env_type, "testing")
        self.assertEqual(env_rec.odoo_version, "19")

    def test_env_sin_proyecto_sin_datos_odoo_no_crea_instancia(self):
        env_rec = self.env["primate.cloud.environment"].create({"name": "Solo srv"})
        self.assertFalse(env_rec.instance_ids)
        self.assertFalse(env_rec.env_type)

    def test_env_sin_proyecto_con_datos_odoo_corta_claro(self):
        with self.assertRaises(UserError):
            self.env["primate.cloud.environment"].create(
                {"name": "Huérfano", "env_type": "testing"})

    # --- delegación write-through -------------------------------------
    def test_delegacion_write_through_en_ambos_sentidos(self):
        env_rec = self._new_env()
        env_rec.write({"odoo_version": "18", "backup_compliance": "ok"})
        self.assertEqual(env_rec.primary_instance_id.odoo_version, "18")
        self.assertEqual(env_rec.primary_instance_id.backup_compliance, "ok")
        env_rec.primary_instance_id.write({"main_url": "y.primate.cloud"})
        self.assertEqual(env_rec.main_url, "y.primate.cloud")

    def test_env_type_buscable_via_delegacion(self):
        env_rec = self._new_env(env_type="staging", name="Srv stg")
        found = self.env["primate.cloud.environment"].search(
            [("env_type", "=", "staging"), ("id", "=", env_rec.id)])
        self.assertEqual(found, env_rec)

    # --- proyectos hospedados (servidor compartido) --------------------
    def test_project_ids_refleja_clientes_hospedados(self):
        env_rec = self._new_env()
        other = self.env["primate.cloud.project"].create(
            {"name": "Cliente Y", "account_id": self.account.id})
        self.env["primate.cloud.instance"].create({
            "name": "Odoo de Y", "project_id": other.id,
            "environment_id": env_rec.id, "http_port": 8079,
        })
        self.assertEqual(set(env_rec.project_ids.ids),
                         {self.project.id, other.id})

    # --- satélites: auto-resolución del vínculo ------------------------
    def test_satelite_desde_environment_resuelve_instancia(self):
        env_rec = self._new_env()
        database = self.env["primate.cloud.database"].create({
            "name": "bd", "account_id": self.account.id,
            "environment_id": env_rec.id, "db_type": "local_pg",
        })
        self.assertEqual(database.instance_id, env_rec.primary_instance_id)

    def test_satelite_desde_instancia_resuelve_environment(self):
        env_rec = self._new_env()
        instance = env_rec.primary_instance_id
        database = self.env["primate.cloud.database"].create({
            "name": "bd2", "account_id": self.account.id,
            "instance_id": instance.id, "db_type": "local_pg",
        })
        self.assertEqual(database.environment_id, env_rec)

    def test_satelite_sin_entorno_queda_suelto(self):
        # El inventario importa recursos sin entorno: sigue siendo válido.
        database = self.env["primate.cloud.database"].create({
            "name": "suelta", "account_id": self.account.id,
            "db_type": "rds",
        })
        self.assertFalse(database.instance_id)
        self.assertFalse(database.environment_id)

    # --- constraints ----------------------------------------------------
    @mute_logger("odoo.sql_db")
    def test_slug_unico_por_servidor(self):
        env_rec = self._new_env()
        with self.assertRaises(pg_errors.UniqueViolation), self.cr.savepoint():
            self.env["primate.cloud.instance"].create({
                "name": "Dup", "slug": env_rec.primary_instance_id.slug,
                "project_id": self.project.id, "environment_id": env_rec.id,
                "http_port": 8089,
            })

    @mute_logger("odoo.sql_db")
    def test_http_port_unico_por_servidor(self):
        env_rec = self._new_env()
        with self.assertRaises(pg_errors.UniqueViolation), self.cr.savepoint():
            self.env["primate.cloud.instance"].create({
                "name": "Dup puerto", "project_id": self.project.id,
                "environment_id": env_rec.id,
                "http_port": env_rec.primary_instance_id.http_port,
            })

    @mute_logger("odoo.sql_db")
    def test_borrar_entorno_con_instancias_bloqueado(self):
        env_rec = self._new_env()
        with self.assertRaises(pg_errors.ForeignKeyViolation), self.cr.savepoint():
            env_rec.unlink()

    @mute_logger("odoo.sql_db")
    def test_borrar_proyecto_con_instancias_bloqueado(self):
        self._new_env()
        with self.assertRaises(pg_errors.ForeignKeyViolation), self.cr.savepoint():
            self.project.unlink()

    # --- rutas legacy por default (convivencia hasta R3) ----------------
    def test_instancia_nace_con_layout_legacy(self):
        instance = self._new_env().primary_instance_id
        self.assertEqual(instance.service_name, "odoo")
        self.assertEqual(instance.conf_path, "/etc/odoo/odoo.conf")
        self.assertEqual(instance.http_port, 8069)
        self.assertEqual(instance.addons_dir, "/opt/odoo/custom-addons")
