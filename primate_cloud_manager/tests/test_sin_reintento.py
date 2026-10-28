# -*- coding: utf-8 -*-
"""Jobs que borran o pisan datos: sin relanzamiento automático + candado del
restore en el servidor (montaje real de bj-uat, 2026-10-28).

Lo que pasó: el servidor de PCM se reinició con el restore adentro, queue_job
lo relanzó («dead job») tres veces y en bj-uat quedaron cuatro scripts de
restore corriendo a la vez (el de SSM no muere con el job de PCM).
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.primate_cloud_manager.models import (
    primate_cloud_destruction as destruction_mod,
    primate_cloud_environment as env_mod,
)
from odoo.addons.queue_job.exception import FailedJobError


@tagged("post_install", "-at_install", "primate_cloud")
class TestSinReintentoAutomatico(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C-retry", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk"})
        partner = self.env["res.partner"].create({"name": "Cliente Retry"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Proyecto Retry", "account_id": self.account.id,
            "partner_id": partner.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "Servidor Retry", "project_id": self.project.id,
            "env_type": "staging", "odoo_version": "19",
            "odoo_edition": "community"})
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "m-retry", "account_id": self.account.id,
            "environment_id": self.server.id, "aws_instance_id": "i-retry",
            "instance_state": "running", "region": "us-east-1",
            "management_state": "provisioned"})
        self.server.ec2_instance_id = self.machine
        self.inst = self.server.primary_instance_id
        self.inst.write({"slug": "retry"})
        # El assertRaises de Odoo deshace en un savepoint lo que escribe la
        # bitácora (en real va en un cursor aparte): se registran las llamadas.
        self.bloqueos = []
        patcher = mock.patch.object(
            destruction_mod, "durable_log",
            side_effect=lambda env, vals: self.bloqueos.append(vals))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _job(self, retry):
        """Un queue.job real (encolado, sin correr) con ese ``retry``."""
        antes = self.env["queue.job"].search([])
        self.server.with_delay().job_check_backup_compliance()
        job = self.env["queue.job"].search([]) - antes
        job.with_context(
            _job_edit_sentinel=self.env["queue.job"].EDIT_SENTINEL
        ).sudo().write({"retry": retry})
        return job

    def _bloqueos(self):
        return [v for v in self.bloqueos if v["action_type"] == "job_no_retry"]

    def test_relanzamiento_del_restore_falla_sin_tocar_nada(self):
        job = self._job(retry=1)   # la 2ª vuelta tras un «dead job»
        Env = type(self.server)
        with mock.patch.object(Env, "_restore_origen_normalizado") as paso1:
            with self.assertRaises(FailedJobError) as ctx:
                self.server.with_context(job_uuid=job.uuid).job_restore_backup(
                    {"db_name": "x"})
        paso1.assert_not_called()          # no llegó a hacer nada
        self.assertIn("NO se relanza", str(ctx.exception))
        log = self._bloqueos()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["resource_id"], self.server.id)
        self.assertEqual(log[0]["result"], "failed")

    def test_primera_corrida_y_relanzamiento_manual_pasan(self):
        Env = type(self.server)
        for retry in (0,):   # 1ª corrida; «Reintentar» a mano vuelve a 0
            job = self._job(retry=retry)
            with mock.patch.object(Env, "_restore_origen_normalizado",
                                   side_effect=RuntimeError("siguió")):
                with self.assertRaises(RuntimeError):
                    self.server.with_context(
                        job_uuid=job.uuid).job_restore_backup({"db_name": "x"})
        # Sin job (llamada directa: tests, otro job) tampoco bloquea.
        with mock.patch.object(Env, "_restore_origen_normalizado",
                               side_effect=RuntimeError("siguió")):
            with self.assertRaises(RuntimeError):
                self.server.job_restore_backup({"db_name": "x"})
        self.assertFalse(self._bloqueos())

    def test_requeue_manual_vuelve_retry_a_cero(self):
        job = self._job(retry=3)
        # El job bloqueado termina FALLIDO; «Reintentar» lo pasa a pendiente
        # con retry 0, así que la corrida manual pasa la guarda.
        job.with_context(
            _job_edit_sentinel=self.env["queue.job"].EDIT_SENTINEL
        ).sudo().write({"state": "failed"})
        job.requeue()
        self.assertEqual(job.retry, 0)

    def test_todos_los_jobs_que_borran_o_pisan_estan_cubiertos(self):
        job = self._job(retry=1)
        ctx = {"job_uuid": job.uuid}
        casos = [
            (self.server, "job_restore_backup", ({"db_name": "x"},)),
            (self.server, "job_create_staging", ({},)),
            (self.server, "job_create_staging_instance", (self.inst.id, {})),
            (self.server, "job_refresh_staging", ({},)),
            (self.inst, "job_refresh_staging_instance", ({},)),
            (self.inst, "job_destroy", ()),
            (self.machine, "job_lifecycle", ("ec2_terminate",)),
            (self.machine, "job_save_config", ({}, "hash")),
        ]
        for rec, metodo, args in casos:
            with self.subTest(metodo=metodo):
                with self.assertRaises(FailedJobError):
                    getattr(rec.with_context(**ctx), metodo)(*args)
        self.assertEqual(len(self._bloqueos()), len(casos))

    def test_detener_o_reiniciar_si_se_pueden_reintentar(self):
        """Solo «Terminar» está bloqueado en job_lifecycle: detener o
        reiniciar repetido no borra nada."""
        job = self._job(retry=1)
        fake = mock.Mock()
        fake.stop_instance.return_value = "req"
        fake.get_instance.return_value = {"instance_state": "stopping"}
        with mock.patch.object(type(self.machine), "_get_ec2_service",
                               return_value=fake), \
                mock.patch.object(type(self.machine), "_notify_user_done"):
            ok = self.machine.with_context(job_uuid=job.uuid).job_lifecycle(
                "ec2_stop")
        self.assertTrue(ok)
        self.assertFalse(self._bloqueos())


FLOCK_SHIM = textwrap.dedent('''\
    #!%s
    # flock -n FD mínimo (util-linux no está en macOS): misma semántica, el
    # lock queda en la descripción de archivo compartida con el shell.
    import fcntl, sys
    fd = int(sys.argv[-1])
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(1)
    ''') % sys.executable


@tagged("post_install", "-at_install", "primate_cloud")
class TestCandadoRestore(TransactionCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="pcm_lock_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env_path = dict(os.environ)
        if not shutil.which("flock"):
            shim = os.path.join(self.tmp, "flock")
            with open(shim, "w") as fh:
                fh.write(FLOCK_SHIM)
            os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC)
            self.env_path["PATH"] = self.tmp + os.pathsep + self.env_path["PATH"]
        patcher = mock.patch.object(env_mod, "RESTORE_LOCK_DIR",
                                    os.path.join(self.tmp, "lock"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.Env = self.env["primate.cloud.environment"]

    def _bash(self, cuerpo):
        script = "set -euo pipefail\n%s\n%s\n" % (
            "\n".join(self.Env._restore_lineas_candado("bj_uat")), cuerpo)
        return subprocess.Popen(["bash", "-c", script], env=self.env_path,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)

    def test_segundo_restore_de_la_misma_base_aborta_sin_tocar_nada(self):
        primero = self._bash("echo TENGO_EL_LOCK; sleep 3")
        time.sleep(1)
        segundo = self._bash("echo NO_DEBERIA_CORRER")
        out, err = segundo.communicate(timeout=20)
        self.assertEqual(segundo.returncode, 75)
        self.assertIn("PCM_ERROR: ya hay un restore de la base bj_uat", out)
        self.assertNotIn("NO_DEBERIA_CORRER", out)
        out1, _err1 = primero.communicate(timeout=20)
        self.assertEqual(primero.returncode, 0)
        self.assertIn("TENGO_EL_LOCK", out1)
        # Terminado el primero, el lock queda libre.
        tercero = self._bash("echo LIBRE")
        out3, _ = tercero.communicate(timeout=20)
        self.assertEqual(tercero.returncode, 0)
        self.assertIn("LIBRE", out3)

    def test_un_hijo_vivo_del_restore_retiene_el_lock(self):
        """Matar el bash del restore no libera el lock si sigue un hijo
        (unzip/psql): justo el caso de las corridas huérfanas."""
        primero = self._bash("sleep 4 >/dev/null 2>&1 & exit 0")
        out, _ = primero.communicate(timeout=20)
        segundo = self._bash("echo NO_DEBERIA_CORRER")
        out2, _ = segundo.communicate(timeout=20)
        self.assertEqual(segundo.returncode, 75)
        self.assertNotIn("NO_DEBERIA_CORRER", out2)

    def test_el_candado_va_antes_que_el_trap_y_que_bajar_o_borrar(self):
        lineas = self.Env._restore_lineas_candado("bj_uat")
        self.assertIn("pcm-restore-bj_uat.lock", lineas[1])
        cuerpo = "\n".join(lineas)
        self.assertIn("exit 75", cuerpo)
        # En los dos scripts reales: candado → trap → aws s3 cp / dropdb.
        fuente = open(env_mod.__file__).read()
        for builder in ("_build_restore_script_catalogo",
                        "_build_restore_script_externo"):
            cuerpo_b = fuente[fuente.index("def %s(" % builder):]
            cuerpo_b = cuerpo_b[:cuerpo_b.index("\n    def ", 10)]
            i_lock = cuerpo_b.index("_restore_lineas_candado(db_name)")
            self.assertLess(i_lock, cuerpo_b.index("trap "))
            self.assertLess(i_lock, cuerpo_b.index("aws s3 cp"))
