# -*- coding: utf-8 -*-
"""Blindaje (regla dura 2026-10-22): importado = solo lectura SIN excepción;
lo destructivo de PCM = grupo manual + triple confirmación server-side.

Qué se afirma, en orden de importancia:
1. Cada acción que modifica, intentada sobre un recurso IMPORTADO, falla y
   el proveedor recibe CERO llamadas — también con el grupo destructivo, y
   también por backend/job/cron (no solo desde la UI).
2. El cerrojo de frontera (cliente boto envuelto) aborta mutaciones sobre
   importados antes de salir a AWS, y deja pasar lecturas y el documento
   SSM de solo lectura.
3. Sin el grupo destructivo no hay destrucción (ni ver ni ejecutar).
4. Con el grupo: falta de un paso, token vencido, reusado o de otro recurso
   → rechazo; el flujo completo ejecuta y deja auditoría paso a paso.
5. Lo creado por PCM sigue funcionando normal en lo no destructivo.
"""
from datetime import timedelta
from unittest import mock

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from odoo.addons.primate_cloud_manager.models.primate_cloud_readonly_guard import (
    ReadonlyGuard,
)
from odoo.addons.primate_cloud_manager.services import (
    aws_base,
    aws_route53,
    aws_ssm,
)

GROUP_DESTRUCTIVE = "primate_cloud_manager.group_cloud_destructive"


class ProveedorQueCuenta:
    """Base AWS falsa: CUALQUIER uso del proveedor queda registrado.

    Se inyecta en ``_get_aws_service`` y en los constructores de servicios;
    el test exige ``calls == []`` tras cada intento sobre un importado."""

    def __init__(self):
        self.calls = []
        self.guard = None

    def get_client(self, service_name, region=None, config=None):
        self.calls.append(("get_client", service_name))
        return mock.Mock(name="cliente-%s" % service_name)


class SsmQueCuenta:
    def __init__(self, registro):
        self.registro = registro

    def __getattr__(self, name):
        def _call(*args, **kwargs):
            self.registro.calls.append(("ssm", name))
            return {"status": "Success", "stdout": ""}
        return _call


@tagged("post_install", "-at_install", "primate_cloud")
class TestBlindajeImportados(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "Prod importada", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk"})
        partner = self.env["res.partner"].create({"name": "Cliente real"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Cliente real", "account_id": self.account.id,
            "partner_id": partner.id})
        self.env_imp = self.env["primate.cloud.environment"].create({
            "name": "Prod cliente (importado)", "project_id": self.project.id,
            "account_id": self.account.id, "env_type": "production",
            "odoo_version": "17", "odoo_edition": "enterprise",
            "state": "active"})
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "prod-cliente", "account_id": self.account.id,
            "environment_id": self.env_imp.id, "aws_instance_id": "i-imp",
            "instance_state": "running", "region": "us-east-1",
            "public_ip": "9.9.9.9"})
        self.env_imp.ec2_instance_id = self.machine
        # La marca de origen la pone la importación (contexto de sync).
        self.machine.with_context(pcm_import_sync=True).sudo().write(
            {"management_state": "adopted_readonly"})
        self.inst = self.env_imp.primary_instance_id
        self.inst.with_context(pcm_import_sync=True).write({
            "slug": "prod-cliente", "main_url": "erp.cliente.com",
            "state": "active", "http_port": 8069, "gevent_port": 8072})
        self.dns = self.env["primate.cloud.dns.record"].create({
            "name": "erp.cliente.com", "account_id": self.account.id,
            "environment_id": self.env_imp.id, "hosted_zone_id": "ZREAL",
            "record_type": "A", "record_value": "9.9.9.9",
            "state": "active"})
        self.db = self.env["primate.cloud.database"].create({
            "name": "cliente_prod", "account_id": self.account.id,
            "environment_id": self.env_imp.id, "db_type": "local_pg",
            "ec2_instance_id": self.machine.id})
        self.repo = self.env["primate.cloud.repository"].create({
            "name": "addons-cliente", "environment_id": self.env_imp.id,
            "local_path": "/opt/odoo/addons-cliente"})
        # Usuario con TODO, incluido el grupo destructivo: igual no puede.
        self.poderoso = new_test_user(
            self.env, login="poderoso_blindaje",
            groups="base.group_user,primate_cloud_manager.group_cloud_admin,"
                   + GROUP_DESTRUCTIVE)
        self.proveedor = ProveedorQueCuenta()

    def _sin_proveedor(self):
        """Contexto: todo acceso a AWS cuenta; además with_delay corre inline
        (así el camino job también se ejercita)."""
        stack = mock.patch.multiple(
            type(self.account), _get_aws_service=mock.Mock(
                return_value=self.proveedor))
        return stack

    def _intentar(self, nombre, fn):
        """Corre ``fn`` esperando UserError/AccessError y CERO llamadas."""
        self.proveedor.calls.clear()
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=self.proveedor), \
                mock.patch.object(aws_ssm, "AwsSsmService",
                                  side_effect=lambda *a, **k: SsmQueCuenta(
                                      self.proveedor)), \
                mock.patch.object(aws_route53, "AwsRoute53Service",
                                  side_effect=lambda *a, **k: SsmQueCuenta(
                                      self.proveedor)):
            try:
                fn()
            except (UserError, AccessError):
                pass
            else:
                self.fail("%s: NO se bloqueó sobre un importado" % nombre)
        self.assertEqual(self.proveedor.calls, [],
                         "%s: llamó al proveedor %s" % (
                             nombre, self.proveedor.calls))

    # ------------------------------------------------------------------
    def test_toda_accion_mutante_falla_sin_llamar_al_proveedor(self):
        """La lista completa — con el usuario que tiene TODOS los grupos."""
        m = self.machine.with_user(self.poderoso)
        e = self.env_imp.with_user(self.poderoso)
        i = self.inst.with_user(self.poderoso)
        d = self.dns.with_user(self.poderoso)
        r = self.repo.with_user(self.poderoso)
        acciones = {
            # Servidor
            "iniciar": lambda: m.action_start(),
            "detener": lambda: m.action_stop(),
            "reiniciar": lambda: m.action_restart(),
            "terminar": lambda: m.action_terminate(),
            "terminar (wizard)": lambda: m.action_open_terminate_wizard(),
            "job ciclo de vida (backend)": lambda: m.sudo().job_lifecycle(
                "ec2_stop"),
            "ejecutar comando": lambda: m.sudo().job_execute_command(
                "uptime"),
            "habilitar gestión": lambda: m.action_habilitar_gestion(),
            "cambiar marca de origen": lambda: m.sudo().write(
                {"management_state": "managed"}),
            "borrar servidor": lambda: m.sudo().unlink(),
            "staging desde el servidor": lambda: m
            .action_create_staging_from_instance(),
            "ticket destructivo": lambda: self.env[
                "primate.cloud.destruction.ticket"].with_user(
                self.poderoso).destruction_start(
                m._name, m.id, "ec2_terminate"),
            # Entorno
            "instalar/reinstalar": lambda: e.sudo().job_install_instance(
                {"db_mode": "none"}),
            "reintentar instalación": lambda: e.action_retry_install(),
            "agregar instancia": lambda: e.sudo().job_add_instance(
                self.inst.id, {}),
            "backup": lambda: e.action_run_backup(),
            "job backup (cron)": lambda: e.sudo().job_run_backup(),
            "staging sobre la base": lambda: e.action_create_staging(),
            "restore externo": lambda: e.action_restore_external_dump(),
            "restore (job)": lambda: e.sudo().job_restore_backup({
                "db_name": "cliente_prod", "instance_id": self.machine.id,
                "backup_id": False, "origin": {"type": "s3", "uri": "x"}}),
            "HTTPS (paso del modelo)": lambda: e.sudo()._provision_ssl(
                self.inst),
            # Instancia Odoo
            "Activar HTTPS": lambda: i.action_enable_ssl(),
            "job HTTPS": lambda: i.sudo().job_enable_ssl(),
            "cambiar dominio": lambda: i.write(
                {"main_url": "otro.cliente.com"}),
            "job cambio de dominio": lambda: i.sudo()
            .job_apply_domain_change("erp.cliente.com"),
            "desmontar": lambda: i.action_open_destroy_wizard(),
            "job desmontar": lambda: i.sudo().job_destroy(),
            "borrar instancia": lambda: i.sudo().unlink(),
            "reintentar DNS": lambda: i.sudo().job_retry_dns(),
            # DNS
            "editar DNS": lambda: d.action_open_edit(),
            "borrar DNS": lambda: d.action_open_delete(),
            "job cambio DNS": lambda: d.sudo().job_apply_change(),
            "job borrado DNS": lambda: d.sudo().job_delete(),
            # Repos
            "editar repo": lambda: r.sudo().write(
                {"configured_branch": "main"}),
            "borrar repo": lambda: r.sudo().unlink(),
        }
        for nombre, fn in acciones.items():
            with self.subTest(accion=nombre):
                self._intentar(nombre, fn)

    def test_cron_ssl_no_toca_importados(self):
        self.inst.with_context(pcm_import_sync=True).sudo().write(
            {"ssl_state": "pending"})
        self.proveedor.calls.clear()
        with mock.patch.object(type(self.account), "_get_aws_service",
                               return_value=self.proveedor):
            self.env["primate.cloud.instance"]._cron_ssl()
        self.assertEqual(self.proveedor.calls, [])

    def test_bloqueos_quedan_en_bitacora(self):
        antes = self.env["primate.cloud.operation.log"].search_count(
            [("action_type", "=", "readonly_block")])
        try:
            self.machine.action_stop()
        except UserError:
            pass
        despues = self.env["primate.cloud.operation.log"].search_count(
            [("action_type", "=", "readonly_block")])
        self.assertEqual(despues, antes + 1)

    def test_lectura_sigue_permitida(self):
        """Mirar es el propósito: el gemelo (lectura) no está bloqueado."""
        twin = {"instances": [], "nginx": [], "system": {}}
        with mock.patch.object(type(self.machine), "_twin_fetch",
                               return_value=twin):
            self.machine.action_capture_twin()

    # ------------------------------------------------- cerrojo de frontera
    def _base_con_guarda(self):
        base = aws_base.AwsBaseService.__new__(aws_base.AwsBaseService)
        clientes = {}

        def fake_client(service_name, **kwargs):
            return clientes.setdefault(service_name, mock.Mock(
                name="boto-%s" % service_name))

        base._session = mock.Mock(client=mock.Mock(side_effect=fake_client))
        base.guard = ReadonlyGuard.from_env(self.env)
        return base, clientes

    def test_frontera_bloquea_mutaciones_y_deja_lecturas(self):
        base, clientes = self._base_con_guarda()
        ec2 = base.get_client("ec2")
        with self.assertRaises(UserError):
            ec2.stop_instances(InstanceIds=["i-imp"])
        self.assertFalse(clientes["ec2"].stop_instances.called)
        with self.assertRaises(UserError):
            ec2.terminate_instances(InstanceIds=["i-otra", "i-imp"])
        self.assertFalse(clientes["ec2"].terminate_instances.called)
        ec2.describe_instances(InstanceIds=["i-imp"])  # lectura: pasa
        self.assertTrue(clientes["ec2"].describe_instances.called)
        ec2.stop_instances(InstanceIds=["i-de-pcm"])     # no importado: pasa
        self.assertTrue(clientes["ec2"].stop_instances.called)

    def test_frontera_ssm_solo_documento_de_lectura(self):
        base, clientes = self._base_con_guarda()
        ssm = base.get_client("ssm")
        with self.assertRaises(UserError):
            ssm.send_command(InstanceIds=["i-imp"],
                             DocumentName="AWS-RunShellScript",
                             Parameters={"commands": ["rm -rf /"]})
        self.assertFalse(clientes["ssm"].send_command.called)
        with self.assertRaises(UserError):  # documento bueno + parámetros
            ssm.send_command(InstanceIds=["i-imp"],
                             DocumentName="pcm-readonly-discovery",
                             Parameters={"x": ["y"]})
        ssm.send_command(InstanceIds=["i-imp"],
                         DocumentName="pcm-readonly-discovery", Parameters={})
        self.assertTrue(clientes["ssm"].send_command.called)

    def test_frontera_route53_nombres_importados(self):
        base, clientes = self._base_con_guarda()
        r53 = base.get_client("route53")
        cambio = {"Changes": [{"Action": "DELETE", "ResourceRecordSet": {
            "Name": "ERP.cliente.com.", "Type": "A"}}]}
        with self.assertRaises(UserError):
            r53.change_resource_record_sets(HostedZoneId="ZREAL",
                                            ChangeBatch=cambio)
        self.assertFalse(clientes["route53"].change_resource_record_sets.called)
        otro = {"Changes": [{"Action": "UPSERT", "ResourceRecordSet": {
            "Name": "nuevo.primate.cloud", "Type": "A"}}]}
        r53.change_resource_record_sets(HostedZoneId="ZPCM", ChangeBatch=otro)
        self.assertTrue(clientes["route53"].change_resource_record_sets.called)


@tagged("post_install", "-at_install", "primate_cloud")
class TestTripleConfirmacion(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "Cuenta PCM", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk"})
        partner = self.env["res.partner"].create({"name": "Cliente PCM"})
        self.project = self.env["primate.cloud.project"].create({
            "name": "Cliente PCM", "account_id": self.account.id,
            "partner_id": partner.id})
        self.server = self.env["primate.cloud.environment"].create({
            "name": "srv-pcm", "project_id": self.project.id,
            "account_id": self.account.id, "env_type": "testing",
            "odoo_version": "19", "odoo_edition": "community",
            "state": "active"})
        self.machine = self.env["primate.cloud.ec2.instance"].create({
            "name": "srv-pcm-1", "account_id": self.account.id,
            "environment_id": self.server.id, "aws_instance_id": "i-pcm1",
            "instance_state": "running", "region": "us-east-1"})
        self.machine_b = self.env["primate.cloud.ec2.instance"].create({
            "name": "srv-pcm-2", "account_id": self.account.id,
            "environment_id": self.server.id, "aws_instance_id": "i-pcm2",
            "instance_state": "running", "region": "us-east-1"})
        self.server.ec2_instance_id = self.machine
        self.admin_sin = new_test_user(
            self.env, login="admin_sin_destructivo", password="clave-sin-1",
            groups="base.group_user,primate_cloud_manager.group_cloud_admin")
        self.destructor = new_test_user(
            self.env, login="destructor_pcm", password="clave-ok-123",
            groups="base.group_user,primate_cloud_manager.group_cloud_admin,"
                   + GROUP_DESTRUCTIVE)
        self.Ticket = self.env["primate.cloud.destruction.ticket"]

    def _auditoria(self):
        return self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "destruction_flow")], order="id")

    # --------------------------------------------------- sin el grupo
    def test_nadie_tiene_el_grupo_por_defecto(self):
        grupo = self.env.ref(GROUP_DESTRUCTIVE)
        self.assertFalse(
            (grupo.user_ids - self.destructor).filtered(
                lambda u: u.login in ("admin", "__system__")),
            "el admin NO puede traer el grupo destructivo por defecto")
        self.assertFalse(self.env.ref("base.user_admin").has_group(
            GROUP_DESTRUCTIVE))

    def test_sin_grupo_ni_ve_ni_ejecuta(self):
        with self.assertRaises(AccessError):
            self.Ticket.with_user(self.admin_sin).destruction_start(
                self.machine._name, self.machine.id, "ec2_terminate")
        try:
            self.machine.with_user(self.admin_sin).action_terminate()
            self.fail("sin grupo destructivo terminó un servidor")
        except (AccessError, UserError):
            pass
        data = self.env["primate.cloud.dashboard"].with_user(
            self.admin_sin).get_server_detail(self.machine.id)
        self.assertFalse(data["can_destroy"])

    # ------------------------------------------------- los tres pasos
    def _paso1(self, machine=None, user=None):
        return self.Ticket.with_user(user or self.destructor).destruction_start(
            (machine or self.machine)._name, (machine or self.machine).id,
            "ec2_terminate")

    def test_falta_el_paso_2(self):
        t1 = self._paso1()
        with self.assertRaises(UserError):
            self.Ticket.with_user(self.destructor).destruction_reauth(
                t1["ticket_id"], t1["token"], "clave-ok-123")

    def test_nombre_equivocado(self):
        t1 = self._paso1()
        with self.assertRaises(UserError):
            self.Ticket.with_user(self.destructor).destruction_confirm_name(
                t1["ticket_id"], t1["token"], "srv-pcm-")

    def test_token_vencido(self):
        t1 = self._paso1()
        self.Ticket.browse(t1["ticket_id"]).sudo().expires_at = \
            fields.Datetime.now() - timedelta(seconds=1)
        with self.assertRaises(UserError):
            self.Ticket.with_user(self.destructor).destruction_confirm_name(
                t1["ticket_id"], t1["token"], "srv-pcm-1")

    def test_token_reusado(self):
        T = self.Ticket.with_user(self.destructor)
        t1 = self._paso1()
        T.destruction_confirm_name(t1["ticket_id"], t1["token"], "srv-pcm-1")
        with self.assertRaises(UserError):  # el token_1 ya no vale
            T.destruction_confirm_name(t1["ticket_id"], t1["token"],
                                       "srv-pcm-1")

    def test_password_incorrecta(self):
        T = self.Ticket.with_user(self.destructor)
        t1 = self._paso1()
        t2 = T.destruction_confirm_name(t1["ticket_id"], t1["token"],
                                        "srv-pcm-1")
        with self.assertRaises(UserError):
            T.destruction_reauth(t2["ticket_id"], t2["token"], "mal")

    def test_ticket_de_otro_usuario(self):
        otro = new_test_user(
            self.env, login="otro_destructor", password="otra-clave-9",
            groups="base.group_user," + GROUP_DESTRUCTIVE)
        t1 = self._paso1()
        with self.assertRaises(UserError):
            self.Ticket.with_user(otro).destruction_confirm_name(
                t1["ticket_id"], t1["token"], "srv-pcm-1")

    def _armado(self, machine=None):
        T = self.Ticket.with_user(self.destructor)
        t1 = self._paso1(machine)
        t2 = T.destruction_confirm_name(
            t1["ticket_id"], t1["token"], (machine or self.machine).name)
        return T.destruction_reauth(t2["ticket_id"], t2["token"],
                                    "clave-ok-123")

    def test_token_de_otro_recurso(self):
        t3 = self._armado(self.machine)
        with mock.patch.object(type(self.machine_b), "with_delay"):
            with self.assertRaises(UserError):
                self.machine_b.with_user(self.destructor).with_context(
                    destruction_ticket_id=t3["ticket_id"],
                    destruction_token=t3["token"]).action_terminate()

    def test_sin_ticket_ni_por_rpc_ni_por_job(self):
        with mock.patch.object(type(self.machine), "with_delay") as delay:
            with self.assertRaises(UserError):
                self.machine.with_user(self.destructor).action_terminate()
            with self.assertRaises(UserError):  # ni el superusuario
                self.machine.sudo().action_terminate()
        self.assertFalse(delay.called)

    def test_flujo_completo_ejecuta_y_audita(self):
        antes = len(self._auditoria())
        t3 = self._armado()
        with mock.patch.object(type(self.machine), "with_delay") as delay:
            self.machine.with_user(self.destructor).with_context(
                destruction_ticket_id=t3["ticket_id"],
                destruction_token=t3["token"]).action_terminate()
        self.assertTrue(delay.called, "el job de terminación se encola")
        ticket = self.Ticket.browse(t3["ticket_id"]).sudo()
        self.assertEqual(ticket.step, "consumed")
        logs = self._auditoria()[antes:]
        nombres = " | ".join(logs.mapped("name"))
        self.assertIn("Paso 1/3", nombres)
        self.assertIn("Paso 2/3", nombres)
        self.assertIn("Paso 3/3", nombres)
        self.assertIn("Confirmación completa", nombres)
        self.assertTrue(all(l.user_id == self.destructor for l in logs))
        # Un solo uso: el mismo token final no sirve dos veces.
        with mock.patch.object(type(self.machine), "with_delay"):
            with self.assertRaises(UserError):
                self.machine.with_user(self.destructor).with_context(
                    destruction_ticket_id=t3["ticket_id"],
                    destruction_token=t3["token"]).action_terminate()

    def test_cancelado_en_paso_2_queda_auditado(self):
        antes = len(self._auditoria())
        t1 = self._paso1()
        self.Ticket.with_user(self.destructor).destruction_cancel(
            t1["ticket_id"])
        logs = self._auditoria()[antes:]
        self.assertIn("Cancelado", " | ".join(logs.mapped("name")))
        with self.assertRaises(UserError):
            self.Ticket.with_user(self.destructor).destruction_confirm_name(
                t1["ticket_id"], t1["token"], "srv-pcm-1")

    def test_job_token_un_solo_uso(self):
        """El job destructivo quema su token: un re-encolado o un cron no
        pueden repetir la destrucción."""
        dns = self.env["primate.cloud.dns.record"].create({
            "name": "x.pcm.test", "account_id": self.account.id,
            "environment_id": self.server.id, "hosted_zone_id": "ZPCM",
            "record_type": "A", "record_value": "1.1.1.1", "state": "active"})
        T = self.Ticket.with_user(self.destructor)
        t1 = T.destruction_start(dns._name, dns.id, "dns_delete")
        t2 = T.destruction_confirm_name(t1["ticket_id"], t1["token"],
                                        "x.pcm.test")
        t3 = T.destruction_reauth(t2["ticket_id"], t2["token"],
                                  "clave-ok-123")
        job_token = T.consume_for_job(dns, "dns_delete", t3["ticket_id"],
                                      t3["token"])
        fake = mock.Mock()
        fake.delete_record.return_value = "/change/C1"
        with mock.patch.object(type(dns), "_service", return_value=fake), \
                mock.patch.object(type(dns), "_poll_and_finalize"):
            dns.sudo().job_delete(t3["ticket_id"], job_token)
            with self.assertRaises(UserError):
                dns.sudo().job_delete(t3["ticket_id"], job_token)
            with self.assertRaises(UserError):  # cron / reintento sin token
                dns.sudo().job_delete()
        self.assertEqual(fake.delete_record.call_count, 1)

    def test_wizard_arma_un_solo_ticket(self):
        """Abrir y guardar el wizard como lo hace el cliente web: UN ticket y
        un solo «Paso 1/3» en la auditoría (antes se duplicaba)."""
        from odoo.tests import Form
        antes_t = self.Ticket.sudo().search_count([])
        antes_a = len(self._auditoria())
        ctx = {"dest_model": self.machine._name, "dest_res_id": self.machine.id,
               "dest_action": "ec2_terminate",
               "dest_callback": "action_terminate"}
        form = Form(self.env["primate.cloud.destruction.wizard"].with_user(
            self.destructor).with_context(**ctx))
        wiz = form.save()
        self.assertEqual(self.Ticket.sudo().search_count([]), antes_t + 1)
        self.assertEqual(len(self._auditoria()) - antes_a, 1)
        self.assertEqual(wiz.ticket_id, self.Ticket.sudo().search(
            [], order="id desc", limit=1).id)
        wiz.action_next_from_impact()
        wiz.typed_name = "srv-pcm-1"
        wiz.action_confirm_name()
        self.assertEqual(wiz.step, "reauth")

    # --------------------------------------------- lo no destructivo, normal
    def test_lo_de_pcm_sigue_normal(self):
        with mock.patch.object(type(self.machine), "with_delay") as delay:
            self.machine.action_start()
            self.machine.action_stop()
        self.assertEqual(delay.call_count, 2)
        inst = self.server.primary_instance_id
        inst.write({"slug": "srv-pcm", "main_url": "srv.pcm.test",
                    "state": "active"})
        with mock.patch.object(type(inst), "with_delay") as delay:
            inst.action_enable_ssl()
        self.assertTrue(delay.called)


@tagged("post_install", "-at_install", "primate_cloud")
class TestOrigenDeLoSincronizado(TransactionCase):
    """Lo que trae el sync de AWS y no creó PCM (DNS de la zona, RDS) nace
    IMPORTADO; la cuenta de solo aprovisionamiento no importa nada ajeno."""

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "Prod RO", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk"})

    def test_dns_sincronizado_nace_importado_y_no_se_toca(self):
        Dns = self.env["primate.cloud.dns.record"]
        Dns._sync_from_aws(self.account, [{
            "hosted_zone_id": "ZREAL", "name": "erp.cliente.com.",
            "record_type": "A", "record_value": "9.9.9.9", "ttl": 300}])
        rec = Dns.search([("name", "=", "erp.cliente.com.")])
        self.assertEqual(rec.pcm_origin, "imported")
        self.assertTrue(rec.pcm_is_imported())
        for fn in (rec.action_open_edit, rec.action_open_delete,
                   rec.job_apply_change, rec.job_delete):
            with self.subTest(fn=fn.__name__):
                with self.assertRaises(UserError):
                    fn()
        guard = ReadonlyGuard.from_env(self.env)
        self.assertIn("erp.cliente.com", guard.dns_names)

    def test_rds_sincronizada_nace_importada(self):
        Db = self.env["primate.cloud.database"]
        Db._sync_rds_from_aws(self.account, [{
            "name": "cliente-prod", "rds_identifier": "cliente-prod-db",
            "engine": "postgres", "engine_version": "14.9",
            "status": "available"}])
        db = Db.search([("rds_identifier", "=", "cliente-prod-db")])
        self.assertEqual(db.pcm_origin, "imported")
        guard = ReadonlyGuard.from_env(self.env)
        self.assertIn("cliente-prod-db", guard.rds_ids)
        with self.assertRaises(UserError):
            guard.check("rds", "delete_db_instance",
                        {"DBInstanceIdentifier": "cliente-prod-db"})

    def test_cuenta_solo_aprovisionamiento_no_importa_lo_ajeno(self):
        prov = self.env["primate.cloud.account"].create({
            "name": "Prod PROV", "default_region": "us-east-1",
            "iam_access_key_id": "AK2", "iam_secret_access_key": "sk2",
            "provision_only": True})
        Ec2 = self.env["primate.cloud.ec2.instance"]
        res = Ec2._sync_from_aws(prov, [
            {"aws_instance_id": "i-ajena", "name": "prod-cliente",
             "instance_state": "running", "region": "us-east-1", "tags": {}},
            {"aws_instance_id": "i-de-pcm", "name": "barraca",
             "instance_state": "running", "region": "us-east-1",
             "tags": {"primate:managed_by": "pcm"}},
        ])
        self.assertEqual(res["created"], 1)
        self.assertFalse(Ec2.search([("aws_instance_id", "=", "i-ajena")]))
        self.assertTrue(Ec2.search([("aws_instance_id", "=", "i-de-pcm")]))
        corrida = self.env["primate.cloud.server.import"].create(
            {"account_id": prov.id, "regions": "us-east-1"})
        with self.assertRaises(UserError):
            corrida.job_import_servers(commit=False)
