# -*- coding: utf-8 -*-
"""El restore del catálogo asigna las tablas al usuario destino (2026-09-19).

El staging del ensayo nació roto **con PCM reportando éxito**: la copia usa el
camino del CATÁLOGO, que restauraba con ``pg_restore -d`` sin
``--no-owner --role``, así que las 950 tablas quedaban a nombre del rol del
ORIGEN. El usuario de la instancia destino no veía **ninguna** tabla
(``information_schema`` sólo lista lo que el usuario puede ver), Odoo creía que
faltaba ``orm_signaling_registry``, intentaba crearla y PostgreSQL respondía
«already exists»: *Failed to load registry* y toda URL redirigiendo a sí misma.

Sólo pasa cuando origen y destino tienen usuarios PG distintos: el caso
multi-Odoo (producción y su staging en el mismo servidor), que es el **default**
de «Crear staging».

Y el chequeo de sanidad post-restore no lo detectó porque corría como
``postgres``, que ve todo. **Principio: todo chequeo de sanidad corre con la
identidad que va a usar la aplicación.**
"""
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "primate_cloud")
class TestDuenoEnRestoreDelCatalogo(TransactionCase):

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
            "aws_instance_id": "i-bj", "instance_state": "running", "region": "us-east-1"})
        # Producción: su propio usuario PG.
        self.prod = self.srv.primary_instance_id
        self.prod.write({"service_name": "odoo-bj", "pg_user": "odoo_bj",
                         "data_dir": "/opt/pcm/instances/bj/data"})
        self.db_prod = self.env["primate.cloud.database"].create({
            "name": "barraca_julia", "account_id": self.account.id,
            "environment_id": self.srv.id, "db_type": "local_pg",
            "ec2_instance_id": self.machine.id, "instance_id": self.prod.id})
        # Staging en el MISMO servidor: OTRO usuario PG (el caso que rompió).
        self.stg = self.srv._create_instance_with_ports({
            "name": "BJ (Staging)", "project_id": project.id, "slug": "bj-staging",
            "env_type": "staging", "service_name": "odoo-bj-staging",
            "pg_user": "odoo_bj_staging", "data_dir": "/opt/pcm/instances/bj-staging/data"})
        self.backup = self.env["primate.cloud.backup"].create({
            "name": "Backup bj", "environment_id": self.srv.id,
            "database_id": self.db_prod.id, "backup_type": "pcm_dump",
            "s3_bucket": "pcm-backups-test",
            "s3_key": "staging/bj/barraca_julia/x.dump"})
        self.backup.write({"state": "completed", "size_mb": 300.0})
        self.Env = self.env["primate.cloud.environment"]

    def _script(self, instancia, db="barraca_julia_staging"):
        return self.Env._build_backup_restore_script(
            db, self.backup, False, instancia)

    # ------------------------------------------------------------------
    # 1. El caso cruzado: origen y destino con usuarios PG distintos
    # ------------------------------------------------------------------
    def test_restaura_con_el_rol_del_destino(self):
        script = self._script(self.stg)
        self.assertIn("pg_restore --no-owner --role=odoo_bj_staging "
                      "-d barraca_julia_staging", script)
        # El rol del ORIGEN no aparece como destinatario de los objetos.
        self.assertNotIn("--role=odoo_bj ", script)

    def test_sanity_check_con_los_ojos_de_la_aplicacion(self):
        script = self._script(self.stg)
        self.assertIn("""psql -d barraca_julia_staging -c 'SET ROLE "odoo_bj_staging"' """
                      '-tAc "SELECT count(*) FROM ir_module_module"', script)
        # Ya no se verifica como postgres (que ve todo aunque los permisos
        # estén mal): ese era el chequeo que dejó pasar el staging roto.
        self.assertNotIn('psql -d barraca_julia_staging -tAc "SELECT count(*)',
                         script)

    # ------------------------------------------------------------------
    # 2. Mismo usuario: inocuo
    # ------------------------------------------------------------------
    def test_mismo_usuario_sigue_funcionando(self):
        script = self._script(self.prod, db="barraca_julia")
        self.assertIn("pg_restore --no-owner --role=odoo_bj -d barraca_julia",
                      script)
        self.assertIn("""psql -d barraca_julia -c 'SET ROLE "odoo_bj"' """
                      '-tAc "SELECT count(*) FROM ir_module_module"', script)

    # ------------------------------------------------------------------
    # 3. El orden no cambió (el resto del camino histórico sigue igual)
    # ------------------------------------------------------------------
    def test_el_orden_del_camino_historico_no_cambio(self):
        script = self._script(self.stg)
        orden = [script.index("df -Pm /var/tmp"),
                 script.index("systemctl stop"),
                 script.index("pg_terminate_backend"),
                 script.index("PCM_DROP_STARTED"),
                 script.index("dropdb --if-exists"),
                 script.index("createdb -O"),
                 script.index("pg_restore --no-owner"),
                 script.index("SELECT count(*) FROM ir_module_module"),
                 script.rindex("PCM_RESTORE_OK")]
        self.assertEqual(orden, sorted(orden), script)

    def test_el_dueno_de_la_base_sigue_siendo_el_destino(self):
        """`createdb -O` ya ponía el dueño de la BASE; lo que faltaba era el de
        las TABLAS que trae el dump."""
        self.assertIn("createdb -O odoo_bj_staging barraca_julia_staging",
                      self._script(self.stg))
