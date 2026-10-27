# -*- coding: utf-8 -*-
"""Secretos fuera de queue_job, Access Key ID enmascarada y RDS sin permisos
(decisiones 2026-10-27).

- Ningún job guarda un secreto en claro (args, kwargs, func_string, nombre):
  si alguno lo intenta, la guarda de queue.job lo rechaza.
- El job recupera el secreto cifrado al ejecutarse; un requeue también.
- La migración limpia los jobs viejos (reencolables → depósito; terminados →
  redactado).
- La Access Key ID se muestra enmascarada también a los admins, y guardar la
  ficha sin tocarla no pisa la credencial.
- RDS con la cuenta de solo aprovisionamiento se rechaza antes de crear nada.
"""
import importlib.util
import json

from odoo.exceptions import UserError
from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase, tagged

SECRETO_PG = "PgSecreto_9f3Kq2"
SECRETO_ADMIN = "AdminSecreto_7xQ"


@tagged("post_install", "-at_install", "primate_cloud")
class TestSecretosJobs(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "C-sec", "default_region": "us-east-1",
            "iam_access_key_id": "AKIAEJEMPLO12345WXYZ",
            "iam_secret_access_key": "sk-ejemplo",
        })
        partner = self.env["res.partner"].create({"name": "Cliente Sec"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Proyecto Sec", "account_id": self.account.id,
            "partner_id": partner.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "Servidor Sec", "project_id": self.project.id,
            "env_type": "staging", "odoo_version": "19",
            "odoo_edition": "community",
        })
        self.server.primary_instance_id.write({"slug": "sec"})
        self.Secret = self.env["primate.cloud.job.secret"]

    def _params(self, **extra):
        return dict({
            "region": "us-east-1", "instance_type": "t3.micro",
            "db_mode": "local_pg", "db_name": "sec_db",
            "db_password": SECRETO_PG, "admin_password": SECRETO_ADMIN,
        }, **extra)

    def _jobs(self):
        return self.env["queue.job"].search(
            [("model_name", "=", "primate.cloud.environment")])

    def _sin_secretos(self, job):
        texto = json.dumps([job.args, job.kwargs], default=str) + \
            (job.func_string or "") + (job.name or "")
        for secreto in (SECRETO_PG, SECRETO_ADMIN):
            self.assertNotIn(secreto, texto)

    # ---------------------------------------------------------- queue_job
    def test_montaje_encola_sin_secretos_en_claro(self):
        antes = self._jobs()
        self.server._enqueue_provision(self._params())
        job = self._jobs() - antes
        self.assertEqual(len(job), 1)
        self._sin_secretos(job)
        params = job.args[0]
        self.assertEqual(set(params["db_password"]), {"pcm_secret_ref"})
        # Al ejecutarse se recupera el secreto (y un requeue reusa la ref).
        claro = self.Secret.reveal(params)
        self.assertEqual(claro["db_password"], SECRETO_PG)
        self.assertEqual(claro["admin_password"], SECRETO_ADMIN)
        self.assertEqual(self.Secret.reveal(params)["db_password"], SECRETO_PG)

    def test_guarda_rechaza_un_job_con_secreto_en_claro(self):
        with self.assertRaises(UserError) as ctx:
            self.server.with_delay().job_install_instance(
                {"db_password": SECRETO_PG})
        self.assertIn("db_password", str(ctx.exception))
        self.assertNotIn(SECRETO_PG, str(ctx.exception))
        with self.assertRaises(UserError):
            self.server.with_delay().job_restore_backup(
                {"destruction": {"ticket_id": 1, "job_token": "tok-claro"}})

    def test_secreto_guardado_cifrado(self):
        ref = self.Secret.stash({"db_password": SECRETO_PG})["db_password"]
        rec = self.Secret.search([("ref", "=", ref["pcm_secret_ref"])])
        self.assertTrue(rec)
        self.assertNotIn(SECRETO_PG, rec.value_encrypted)

    def test_secreto_purgado_corta_con_mensaje(self):
        with self.assertRaises(UserError):
            self.Secret.reveal({"pcm_secret_ref": "no-existe"})

    def test_token_destructivo_por_referencia(self):
        Ticket = self.env["primate.cloud.destruction.ticket"]
        ref = self.Secret.stash({"job_token": "tok-123"})["job_token"]
        # burn_job_token recupera la referencia antes de comparar el hash.
        with self.assertRaises(UserError) as ctx:
            Ticket.burn_job_token(self.server, "x", 0, ref)
        self.assertIn("ticket inexistente", str(ctx.exception))

    # ---------------------------------------------------------- migración
    def _migracion(self):
        ruta = get_module_path("primate_cloud_manager") + \
            "/migrations/19.0.1.6.0/post-migrate.py"
        spec = importlib.util.spec_from_file_location("pcm_mig_1_6", ruta)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _job_viejo(self, state):
        """Un job como los de antes: con el secreto en claro (SQL directo,
        porque la guarda nueva ya no deja crearlo)."""
        params = self._params()
        self.env.cr.execute("""
            INSERT INTO queue_job (uuid, user_id, company_id, name,
                model_name, method_name, records, args, kwargs, func_string,
                state, priority, date_created, exc_info)
            VALUES (%s, %s, %s, 'viejo', 'primate.cloud.environment',
                'job_provision_server', %s, %s, '{}', %s, %s, 10, now(), %s)
            RETURNING id""", (
            "uuid-%s" % state, self.env.uid, self.env.company.id,
            json.dumps({"_type": "odoo_recordset",
                        "model": "primate.cloud.environment",
                        "ids": [self.server.id], "uid": self.env.uid}),
            json.dumps([params]),
            "primate.cloud.environment(%s,).job_provision_server(%r)" % (
                self.server.id, params),
            state, "psql: password %s rechazada" % SECRETO_PG))
        job = self.env["queue.job"].browse(self.env.cr.fetchone()[0])
        job.invalidate_recordset()
        return job

    def test_migracion_limpia_jobs_viejos(self):
        fallido = self._job_viejo("failed")
        hecho = self._job_viejo("done")
        self._migracion().migrate(self.env.cr, "19.0.1.5.0")
        for job in (fallido, hecho):
            job.invalidate_recordset()
            self._sin_secretos(job)
            self.assertNotIn(SECRETO_PG, job.exc_info or "")
        # El fallido se puede reencolar: su secreto quedó en el depósito.
        self.assertEqual(
            self.Secret.reveal(fallido.args[0])["db_password"], SECRETO_PG)
        self.assertEqual(hecho.args[0]["db_password"], "[redactado]")

    # ------------------------------------------------------ Access Key ID
    def test_access_key_id_enmascarada_y_no_se_pisa(self):
        self.account.invalidate_recordset()
        self.assertEqual(self.account.iam_access_key_id, "AKIA…WXYZ")
        cifrado = self.account.iam_access_key_id_encrypted
        # Guardar la ficha con la máscara (lo que devuelve el form) no cambia nada.
        self.account.write({"iam_access_key_id": "AKIA…WXYZ"})
        self.account.invalidate_recordset()
        self.assertEqual(self.account.iam_access_key_id_encrypted, cifrado)
        self.assertEqual(
            self.account._get_decrypted_credentials()["access_key_id"],
            "AKIAEJEMPLO12345WXYZ")
        # Una nueva, en cambio, sí se guarda.
        self.account.write({"iam_access_key_id": "AKIANUEVA00000001234"})
        self.account.invalidate_recordset()
        self.assertEqual(self.account.iam_access_key_id, "AKIA…1234")

    # ------------------------------------------------------------------ RDS
    def test_rds_con_cuenta_sin_permisos_se_rechaza_antes(self):
        self.account.provision_only = True
        antes = self._jobs()
        with self.assertRaises(UserError) as ctx:
            self.server._enqueue_provision(self._params(
                db_mode="rds", rds_identifier="x"))
        self.assertIn("RDS", str(ctx.exception))
        self.assertFalse(self._jobs() - antes)
        self.assertNotEqual(self.server.state, "provisioning")
