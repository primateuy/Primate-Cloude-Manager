# -*- coding: utf-8 -*-
"""Restore que neutraliza ANTES de arrancar Odoo (2026-09-19).

El orden viejo era: restore → el ``trap`` arranca la unit → un segundo
comando SSM neutraliza. En el ensayo de Barraca Julia la neutralización falló
(``website_domain_unique`` con 3 websites) y Odoo quedó corriendo sobre la base
restaurada con el SMTP real y 22 crons activos; los crons vencidos corrieron al
minuto de arrancar. Hubo que parar la unit a mano.

Lo que fija este archivo:

1. **El orden en el script**: la neutralización corre con la unit parada,
   después de la carga y antes de ``PCM_RESTORE_OK``; el arranque del ``trap``
   pasa por ``pcm_start_unit``, que NO arranca una base recreada sin
   neutralizar. Se prueba ejecutando la lógica en bash, no sólo leyendo texto.
2. **El job**: neutraliza por el script (ya no llama a ``_staging_neutralize``),
   exige el marcador y reporta el fallo como parcial con la unit detenida.
3. **Producción no cambia**: sin neutralización el script es el de siempre.
"""
import base64
import re
import shutil
import subprocess
import unittest
from unittest import mock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.primate_cloud_manager.services import aws_s3

SQL = ("\\set ON_ERROR_STOP on\nBEGIN;\n"
       "UPDATE ir_cron SET active = false; -- 'comillas' y $dolares\n"
       "COMMIT;\n")


@tagged("post_install", "-at_install", "primate_cloud")
class TestRestoreNeutralizaAntesDeArrancar(TransactionCase):

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
        self.env_srv = self.env["primate.cloud.environment"].create({
            "name": "BJ ensayo", "project_id": self.project.id,
            "env_type": "testing", "state": "active",
        })
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "bj-ensayo", "account_id": self.account.id,
            "environment_id": self.env_srv.id, "aws_instance_id": "i-bj",
            "instance_state": "running", "region": "us-east-1",
        })
        self.odoo_inst = self.env_srv.primary_instance_id
        self.odoo_inst.write({
            "service_name": "odoo-bj", "pg_user": "odoo_bj",
            "data_dir": "/opt/pcm/instances/bj/data", "env_type": "testing",
            "main_url": "bj-ensayo.primate.cloud",
        })
        self.db = self.env["primate.cloud.database"].create({
            "name": "barraca_julia", "account_id": self.account.id,
            "environment_id": self.env_srv.id, "db_type": "local_pg",
            "ec2_instance_id": self.machine.id,
            "instance_id": self.odoo_inst.id,
        })
        self.backup = self.env["primate.cloud.backup"].create({
            "name": "Backup bj", "environment_id": self.env_srv.id,
            "database_id": self.db.id, "backup_type": "pcm_dump",
            "s3_bucket": "pcm-backups-test", "s3_key": "pcm-backups/x.dump",
        })
        self.backup.write({"state": "completed", "size_mb": 10.0})
        self.Env = self.env["primate.cloud.environment"]
        self.origen = {"type": "s3_uri", "uri": "s3://pcm-backups-test/in/bj.zip",
                       "format": "auto", "bucket": "pcm-backups-test",
                       "key": "in/bj.zip"}

    def _scripts(self, sql=SQL):
        """(catálogo, externo) con neutralización."""
        return (
            self.Env._build_backup_restore_script(
                "barraca_julia", self.backup, False, self.odoo_inst,
                neutralize_sql=sql),
            self.Env._build_backup_restore_script(
                "barraca_julia", self.backup, False, self.odoo_inst,
                origin=self.origen, origin_size_mb=10.0, neutralize_sql=sql),
        )

    # ------------------------------------------------------------------
    # 1. El orden, en el texto de los dos caminos
    # ------------------------------------------------------------------
    def test_orden_neutraliza_despues_de_cargar_y_antes_del_ok(self):
        for script in self._scripts():
            # La línea de CARGA (el `[ ! -f .../dump.sql ]` de la
            # verificación previa también contiene "-f": se busca la última).
            carga = max(script.rfind("pg_restore -d"),
                        script.rfind('-f "$WORK/x/dump.sql"'))
            self.assertGreater(carga, 0)
            orden = [script.index("systemctl stop odoo-bj"),
                     script.index("pcm_start_unit() {"),
                     script.index("PCM_DROPPED=1"),
                     script.index("dropdb --if-exists"),
                     carga,
                     script.index("base64 -d"),
                     script.rindex("PCM_RESTORE_OK")]
            self.assertEqual(orden, sorted(orden), script)

    def test_el_trap_arranca_solo_por_pcm_start_unit(self):
        """El único ``systemctl start`` está dentro de pcm_start_unit."""
        for script in self._scripts():
            self.assertEqual(script.count("systemctl start"), 1)
            funcion = script[script.index("pcm_start_unit() {"):]
            funcion = funcion[:funcion.index("; }") + 3]
            self.assertIn("systemctl start odoo-bj", funcion)
        catalogo, externo = self._scripts()
        self.assertIn("trap 'pcm_start_unit; rm -rf", catalogo)
        self.assertIn("cleanup() { rc=$?; set +x; pcm_start_unit; rm -rf",
                      externo)

    def test_el_sql_viaja_intacto_en_base64(self):
        for script in self._scripts():
            b64 = re.search(r"printf '%s' (\S+) \| base64 -d", script).group(1)
            self.assertEqual(base64.b64decode(b64.strip("'")).decode(), SQL)

    def test_sin_neutralizacion_el_script_es_el_de_siempre(self):
        """Producción: ni la función ni el paso; el arranque de siempre."""
        for script in (
                self.Env._build_backup_restore_script(
                    "barraca_julia", self.backup, False, self.odoo_inst),
                self.Env._build_backup_restore_script(
                    "barraca_julia", self.backup, False, self.odoo_inst,
                    origin=self.origen, origin_size_mb=10.0)):
            for nuevo in ("pcm_start_unit", "PCM_DROPPED", "base64 -d",
                          "PCM_NEUTRALIZED"):
                self.assertNotIn(nuevo, script)
            self.assertIn("systemctl start odoo-bj || true;", script)

    # ------------------------------------------------------------------
    # 1b. La lógica del arranque, EJECUTADA en bash
    # ------------------------------------------------------------------
    def _bash(self, cuerpo, psql_ok=True):
        """Corre init + trap + ``cuerpo`` con systemctl/sudo simulados."""
        piezas = self.Env._restore_lineas_neutralizacion(
            "barraca_julia", "odoo-bj", SQL, "say")
        script = "\n".join([
            "set -euo pipefail",
            'say() { echo "$*"; }',
            'systemctl() { echo "ARRANCA $2"; }',
            # psql simulado: consume el SQL (y lo muestra) o falla como psql
            # con ON_ERROR_STOP (código 3).
            ("sudo() { echo \"SQL=$(cat | head -1)\"; }" if psql_ok
             else "sudo() { cat >/dev/null; return 3; }"),
        ] + piezas["init"] + [
            "trap '%s' EXIT" % piezas["start"],
            cuerpo.replace("APPLY", "\n".join(piezas["apply"])),
        ])
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, timeout=20)

    @unittest.skipUnless(shutil.which("bash") and shutil.which("base64"),
                         "requiere bash y base64")
    def test_bash_falla_antes_del_drop_arranca(self):
        """La base vieja sigue intacta: se arranca como siempre."""
        r = self._bash("exit 1")
        self.assertIn("ARRANCA odoo-bj", r.stdout)
        self.assertNotIn("PCM_UNIT_LEFT_STOPPED", r.stdout)

    @unittest.skipUnless(shutil.which("bash") and shutil.which("base64"),
                         "requiere bash y base64")
    def test_bash_falla_la_carga_despues_del_drop_no_arranca(self):
        r = self._bash("PCM_DROPPED=1\nfalse\nAPPLY")
        self.assertNotIn("ARRANCA", r.stdout)
        self.assertIn("PCM_UNIT_LEFT_STOPPED", r.stdout)
        self.assertNotEqual(r.returncode, 0)

    @unittest.skipUnless(shutil.which("bash") and shutil.which("base64"),
                         "requiere bash y base64")
    def test_bash_falla_la_neutralizacion_no_arranca(self):
        r = self._bash("PCM_DROPPED=1\nAPPLY\nsay PCM_RESTORE_OK",
                       psql_ok=False)
        self.assertIn("PCM_NEUTRALIZE_FAILED", r.stdout)
        self.assertIn("PCM_UNIT_LEFT_STOPPED", r.stdout)
        self.assertNotIn("ARRANCA", r.stdout)
        self.assertNotIn("PCM_RESTORE_OK", r.stdout)
        self.assertNotEqual(r.returncode, 0)

    @unittest.skipUnless(shutil.which("bash") and shutil.which("base64"),
                         "requiere bash y base64")
    def test_bash_neutraliza_y_recien_ahi_arranca(self):
        r = self._bash("PCM_DROPPED=1\nAPPLY\nsay PCM_RESTORE_OK")
        self.assertEqual(r.returncode, 0, r.stderr)
        salida = r.stdout
        self.assertIn("SQL=\\set ON_ERROR_STOP on", salida)
        self.assertLess(salida.index("PCM_NEUTRALIZED"),
                        salida.index("PCM_RESTORE_OK"))
        self.assertLess(salida.index("PCM_RESTORE_OK"),
                        salida.index("ARRANCA odoo-bj"))

    # ------------------------------------------------------------------
    # 2. El job
    # ------------------------------------------------------------------
    def _job(self, stdout, status="Success", **params):
        fake = mock.Mock()
        fake.run_script.return_value = {"status": status, "stdout": stdout,
                                        "stderr": ""}
        vals = {"backup_id": self.backup.id, "db_name": "barraca_julia",
                "instance_id": self.machine.id, "pre_backup": False}
        vals.update(params)
        with mock.patch.object(aws_s3.AwsS3Service, "head_object",
                               return_value={"key": "k", "size": 1}), \
                mock.patch.object(type(self.env_srv), "_staging_neutralize") \
                as viejo, \
                mock.patch.object(type(self.machine), "_get_ssm_service",
                                  return_value=fake):
            ok = self.env_srv.job_restore_backup(vals)
        viejo.assert_not_called()  # ya no hay segundo comando SSM
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "backup_restore"),
             ("resource_id", "=", self.env_srv.id)], limit=1, order="id desc")
        return ok, fake.run_script.call_args[0][1], log

    def test_job_neutraliza_dentro_del_script(self):
        ok, script, log = self._job(
            "PCM_DROP_STARTED\nPCM_NEUTRALIZED\nPCM_RESTORE_OK")
        self.assertTrue(ok)
        self.assertEqual(log.result, "success")
        self.assertIn("pcm_start_unit", script)
        b64 = re.search(r"printf '%s' (\S+) \| base64 -d", script).group(1)
        sql = base64.b64decode(b64.strip("'")).decode()
        self.assertIn("https://bj-ensayo.primate.cloud", sql)
        self.assertIn("UPDATE website SET domain = NULL", sql)
        self.assertIn("antes de arrancar Odoo",
                      self.env_srv.staging_neutralization_log)

    def test_job_sin_marcador_de_neutralizacion_no_es_exito(self):
        ok, _script, log = self._job("PCM_DROP_STARTED\nPCM_RESTORE_OK")
        self.assertFalse(ok)
        self.assertEqual(log.result, "partial")

    def test_job_neutralizacion_fallida_parcial_con_unit_detenida(self):
        ok, _script, log = self._job(
            "PCM_DROP_STARTED\nPCM_NEUTRALIZE_FAILED\nPCM_UNIT_LEFT_STOPPED",
            status="Failed")
        self.assertFalse(ok)
        self.assertEqual(log.result, "partial")
        self.assertIn("DETENIDO", log.error_message)

    def test_job_neutralize_false_no_neutraliza(self):
        ok, script, _log = self._job("PCM_DROP_STARTED\nPCM_RESTORE_OK",
                                     neutralize=False)
        self.assertTrue(ok)
        self.assertNotIn("pcm_start_unit", script)

    def test_job_produccion_no_neutraliza(self):
        self.odoo_inst.env_type = "production"
        ok, script, _log = self._job("PCM_DROP_STARTED\nPCM_RESTORE_OK")
        self.assertTrue(ok)
        self.assertNotIn("base64 -d", script)
        self.assertIn("systemctl start odoo-bj || true;", script)
