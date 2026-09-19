# -*- coding: utf-8 -*-
"""F2 parte 2: importación masiva de servidores de una cuenta AWS.

Lo que se cubre, en orden de importancia:

1. **Inofensiva por construcción**: lo que entra desde AWS queda en
   ``adopted_readonly`` y el discovery es de sólo lectura. (Los caminos del
   sync y de los tags se prueban en ``test_f2_contrato_fix``.)
2. **Robustez**: un servidor sin SSM, un layout raro, un servidor que explota o
   una región que falla NO abortan la corrida.
3. **Idempotencia**: volver a importar actualiza, no duplica.
4. **Clasificación**: lo descubierto queda en el proyecto centinela, archivado
   y fuera de las vistas de negocio; el filtro «Sin clasificar» lo encuentra.

Todo AWS/SSM está mockeado: los tests no tocan infraestructura real.
"""
import json
import re
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import file_open

SVC = "odoo.addons.primate_cloud_manager.services"
EC2_LIST = SVC + ".aws_ec2.AwsEc2Service.list_instances"
SSM_PING = SVC + ".aws_ssm.AwsSsmService.describe_instance_information"
SSM_RUN = SVC + ".aws_ssm.AwsSsmService.run_script"


def ec2(iid, name, state="running", region="us-east-1"):
    """Un EC2 como lo devuelve AwsEc2Service.list_instances."""
    return {"aws_instance_id": iid, "name": name, "instance_state": state,
            "instance_type": "t3.small", "public_ip": None, "private_ip": None,
            "region": region, "tags": {}, "volume_ids": [], "created_at": None}


def discovery(units=None, dbs=None, **extra):
    """La salida del script de discovery, como la ve el job."""
    datos = {"units": units or [], "units_error": None,
             "databases": dbs or [], "databases_error": None}
    datos.update(extra)
    return {"status": "Success",
            "stdout": "PCM_DISCOVERY_JSON:" + json.dumps(datos), "stderr": ""}


UNIT_BJ = {
    "unit": "odoo-bj.service", "active": "active", "user": "odoo",
    "exec": "/opt/odoo/venv/bin/python3 /opt/odoo/odoo-bin -c /etc/odoo/bj.conf",
    "conf_path": "/etc/odoo/bj.conf", "version": "19",
    "conf": {"data_dir": "/opt/odoo/data", "db_user": "odoo_bj",
             "http_port": "8069"},
    "conf_error": None,
}


@tagged("post_install", "-at_install", "primate_cloud")
class TestF2Importacion(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["primate.cloud.account"].create({
            "name": "Cuenta Primate", "default_region": "us-east-1",
            "iam_access_key_id": "AK", "iam_secret_access_key": "sk",
        })
        self.account.connection_state = "connected"
        self.Run = self.env["primate.cloud.server.import"]
        self.Ec2 = self.env["primate.cloud.ec2.instance"]

    def _corrida(self, regions="us-east-1", include_stopped=True, discover=True):
        return self.Run.create({
            "account_id": self.account.id, "regions": regions,
            "include_stopped": include_stopped, "discover": discover})

    def _correr(self, corrida, por_region, pings=None, run=None):
        """Corre el job con AWS/SSM mockeados. ``por_region``: {region: [...]}"""
        def listar(_self, region=None):
            valor = por_region.get(region, [])
            if isinstance(valor, Exception):
                raise valor
            return valor
        with mock.patch(EC2_LIST, listar), \
                mock.patch(SSM_PING, return_value=pings or {}), \
                mock.patch(SSM_RUN, side_effect=run or (
                    lambda *a, **k: discovery())) as run_script:
            corrida.job_import_servers(commit=False)
        return run_script

    # ------------------------------------------------------------------
    # 1. Inofensiva por construcción
    # ------------------------------------------------------------------
    def test_lo_importado_nace_en_solo_lectura(self):
        corrida = self._corrida()
        self._correr(corrida, {"us-east-1": [ec2("i-1", "prod-a")]})
        maquina = self.Ec2.search([("aws_instance_id", "=", "i-1")])
        self.assertEqual(maquina.management_state, "adopted_readonly")
        with self.assertRaises(UserError):
            maquina.action_stop()

    def test_el_script_de_discovery_es_solo_lectura(self):
        """El contrato, verificado sobre el texto del script."""
        with file_open("primate_cloud_manager/data/discovery_read.py") as fh:
            script = fh.read()
        codigo = "\n".join(l for l in script.splitlines()
                           if not l.strip().startswith("#"))
        verbos = set(re.findall(r'\["systemctl",\s*"([\w-]+)"', codigo))
        self.assertTrue(verbos)
        self.assertLessEqual(verbos, {"list-units", "show"},
                             "systemctl sólo puede leer: %s" % verbos)
        self.assertNotRegex(codigo, r"open\([^)]*['\"][wa]\+?['\"]")
        # Palabras completas: buscar la subcadena "apt" matchea en
        # "capture_output".
        for prohibido in (r"\bos\.remove\b", r"\bshutil\b", r"\bchown\b",
                          r"\bchmod\b", r"\bapt(-get)?\b", r"\bpip\s+install\b",
                          r"systemctl\s+(restart|stop|start|enable|disable)"):
            self.assertNotRegex(codigo, prohibido)
        self.assertNotIn("db_password", script.split("CONF_KEYS")[1][:400])

    # ------------------------------------------------------------------
    # 2. Robustez: nada aborta la corrida
    # ------------------------------------------------------------------
    def test_sin_agente_ssm_no_falla_y_deja_pasos(self):
        corrida = self._corrida()
        self._correr(corrida, {"us-east-1": [ec2("i-1", "prod-a")]}, pings={})
        linea = corrida.line_ids
        self.assertEqual(linea.ssm_state, "unreachable")
        self.assertIn("pcm-ssm-role", linea.manual_steps)
        self.assertEqual(linea.discovery_state, "not_run")
        self.assertEqual(corrida.state, "done")
        self.assertEqual(corrida.unreachable_count, 1)
        self.assertIn("SIN ACCESO SSM", corrida.report)

    def test_conexion_perdida_tiene_pasos_propios(self):
        corrida = self._corrida()
        self._correr(corrida, {"us-east-1": [ec2("i-1", "prod-a")]},
                     pings={"i-1": "ConnectionLost"})
        self.assertIn("ConnectionLost", corrida.line_ids.manual_steps)

    def test_layout_no_reconocido_no_aborta(self):
        raro = dict(UNIT_BJ, unit="odoo-raro.service", conf_path=None,
                    conf=None, exec="/usr/bin/supervisord odoo")
        corrida = self._corrida()
        self._correr(corrida, {"us-east-1": [ec2("i-1", "raro")]},
                     pings={"i-1": "Online"},
                     run=lambda *a, **k: discovery([raro]))
        linea = corrida.line_ids
        self.assertEqual(linea.discovery_state, "unrecognized")
        self.assertIn("supervisord", linea.detail)
        self.assertEqual(corrida.state, "done")
        self.assertIn("NO RECONOCIDOS", corrida.report)

    def test_un_servidor_que_explota_no_aborta_la_corrida(self):
        # mock.patch sobre el método de la CLASE: el mock no recibe self, el
        # primer argumento ya es el instance_id.
        def run(instance_id, *a, **k):
            if instance_id == "i-1":
                raise RuntimeError("se cayó el agente")
            return discovery([UNIT_BJ])
        corrida = self._corrida()
        self._correr(corrida, {"us-east-1": [ec2("i-1", "a"), ec2("i-2", "b")]},
                     pings={"i-1": "Online", "i-2": "Online"}, run=run)
        estados = dict(corrida.line_ids.mapped(
            lambda l: (l.aws_instance_id, l.discovery_state)))
        self.assertEqual(estados, {"i-1": "error", "i-2": "ok"})
        self.assertEqual(corrida.error_count, 1)
        self.assertEqual(corrida.state, "done")

    def test_una_region_que_falla_no_frena_las_otras(self):
        corrida = self._corrida(regions="us-east-1, sa-east-1")
        self._correr(corrida, {"us-east-1": RuntimeError("AccessDenied"),
                               "sa-east-1": [ec2("i-5", "sp", region="sa-east-1")]})
        self.assertEqual(corrida.total_servers, 1)
        self.assertIn("us-east-1", corrida.report)
        log = self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "server_import"),
             ("resource_id", "=", corrida.id)], limit=1)
        self.assertEqual(log.result, "partial")

    def test_el_avance_nunca_retrocede(self):
        """El total se fija ANTES de procesar: sumando región por región, 5 de 5
        daba 100 % y al entrar la región siguiente bajaba a 5 de 6."""
        corrida = self._corrida(regions="us-east-1, sa-east-1")
        muestras = []
        original = type(corrida)._avanzar

        def espiar(rec, commit):
            original(rec, commit)
            muestras.append((rec.total_servers, rec.progress))
        with mock.patch.object(type(corrida), "_avanzar", espiar):
            self._correr(corrida, {
                "us-east-1": [ec2("i-1", "a"), ec2("i-2", "b")],
                "sa-east-1": [ec2("i-3", "c", region="sa-east-1")]})
        self.assertEqual({t for t, _p in muestras}, {3}, "el total no cambia")
        avances = [p for _t, p in muestras]
        self.assertEqual(avances, sorted(avances), "el avance no retrocede")
        self.assertEqual(avances[-1], 100.0)

    def test_estados_filtrados(self):
        corrida = self._corrida(include_stopped=False)
        self._correr(corrida, {"us-east-1": [
            ec2("i-1", "vivo"), ec2("i-2", "parado", state="stopped"),
            ec2("i-3", "muerto", state="terminated")]})
        self.assertEqual(corrida.line_ids.mapped("aws_instance_id"), ["i-1"])

    # ------------------------------------------------------------------
    # 3. Discovery e idempotencia
    # ------------------------------------------------------------------
    def test_discovery_registra_instancias_y_bases(self):
        corrida = self._corrida()
        self._correr(corrida, {"us-east-1": [ec2("i-1", "bj-srv")]},
                     pings={"i-1": "Online"},
                     run=lambda *a, **k: discovery(
                         [UNIT_BJ], [{"name": "barraca_julia", "owner": "odoo_bj"},
                                     {"name": "otra", "owner": "postgres"}]))
        maquina = self.Ec2.search([("aws_instance_id", "=", "i-1")])
        entorno = maquina.environment_id
        self.assertTrue(entorno)
        self.assertFalse(entorno.project_id,
                         "el entorno queda sin proyecto (D6: servidor compartido)")
        inst = entorno.instance_ids
        self.assertEqual(inst.slug, "odoo-bj")
        self.assertEqual(inst.service_name, "odoo-bj")
        self.assertEqual(inst.conf_path, "/etc/odoo/bj.conf")
        self.assertEqual(inst.http_port, 8069)
        self.assertEqual(inst.env_type, "production", "default SEGURO")
        self.assertFalse(inst.project_id.active, "proyecto centinela, archivado")
        bases = self.env["primate.cloud.database"].search(
            [("environment_id", "=", entorno.id)])
        self.assertEqual(sorted(bases.mapped("name")), ["barraca_julia", "otra"])
        bj = bases.filtered(lambda b: b.name == "barraca_julia")
        self.assertEqual(bj.instance_id, inst, "asociada por el dueño de la base")
        self.assertFalse(bases.filtered(lambda b: b.name == "otra").instance_id)
        self.assertEqual(corrida.discovered_count, 1)

    def test_dos_odoo_en_el_mismo_puerto_no_revientan_el_discovery(self):
        """Sin puerto leído se usa el default real de Odoo (8069). Dos Odoo que
        caen en el mismo puerto violaban UNIQUE(environment_id, http_port) y
        tiraban el discovery de todo el servidor: ahora el segundo se anota
        para revisar a mano y el resto se registra."""
        a = dict(UNIT_BJ, unit="odoo-a.service", conf={"db_user": "odoo_a"})
        b = dict(UNIT_BJ, unit="odoo-b.service", conf={"db_user": "odoo_b"})
        corrida = self._corrida()
        self._correr(corrida, {"us-east-1": [ec2("i-1", "multi")]},
                     pings={"i-1": "Online"},
                     run=lambda *a_, **k: discovery([a, b]))
        linea = corrida.line_ids
        self.assertEqual(linea.discovery_state, "ok", linea.detail)
        insts = self.Ec2.search([("aws_instance_id", "=", "i-1")]) \
            .environment_id.instance_ids
        self.assertEqual(insts.mapped("slug"), ["odoo-a"])
        self.assertEqual(insts.http_port, 8069)
        self.assertIn("odoo-b.service", linea.detail)
        self.assertIn("8069", linea.detail)
        self.assertFalse(insts.data_dir,
                         "sin data_dir leído no se asume el del layout legacy")

    def test_puerto_por_argumento_del_execstart(self):
        unit = dict(UNIT_BJ, port_arg="8169", conf={"db_user": "odoo_bj"})
        corrida = self._corrida()
        self._correr(corrida, {"us-east-1": [ec2("i-1", "arg")]},
                     pings={"i-1": "Online"},
                     run=lambda *a, **k: discovery([unit]))
        inst = self.Ec2.search([("aws_instance_id", "=", "i-1")]) \
            .environment_id.instance_ids
        self.assertEqual(inst.http_port, 8169)

    def test_reimportar_no_duplica(self):
        por_region = {"us-east-1": [ec2("i-1", "bj-srv")]}
        pings = {"i-1": "Online"}
        run = lambda *a, **k: discovery(
            [UNIT_BJ], [{"name": "barraca_julia", "owner": "odoo_bj"}])
        self._correr(self._corrida(), por_region, pings, run)
        # Un operador corrige a mano una asociación: reimportar no la pisa.
        entorno = self.Ec2.search([("aws_instance_id", "=", "i-1")]).environment_id
        base = self.env["primate.cloud.database"].search(
            [("environment_id", "=", entorno.id)])
        otra = self.env["primate.cloud.instance"].create({
            "name": "otra", "slug": "otra", "environment_id": entorno.id,
            "http_port": 8070,
            "project_id": entorno.instance_ids[:1].project_id.id})
        base.instance_id = otra
        segunda = self._corrida()
        self._correr(segunda, por_region, pings, run)
        self.assertEqual(segunda.created_count, 0)
        self.assertEqual(segunda.updated_count, 1)
        self.assertEqual(self.Ec2.search_count([("aws_instance_id", "=", "i-1")]), 1)
        self.assertEqual(len(entorno.instance_ids.filtered(
            lambda i: i.slug == "odoo-bj")), 1)
        self.assertEqual(self.env["primate.cloud.database"].search_count(
            [("environment_id", "=", entorno.id)]), 1)
        self.assertEqual(base.instance_id, otra,
                         "la asociación manual sobrevive a la reimportación")

    def test_servidor_de_pcm_no_se_redescubre(self):
        self.Ec2._register_provisioned(self.account, ec2("i-1", "pcm-srv"))
        corrida = self._corrida()
        run_script = self._correr(corrida, {"us-east-1": [ec2("i-1", "pcm-srv")]},
                                  pings={"i-1": "Online"})
        self.assertEqual(corrida.line_ids.discovery_state, "skipped")
        run_script.assert_not_called()

    def test_sin_discovery_no_corre_nada_en_el_servidor(self):
        corrida = self._corrida(discover=False)
        run_script = self._correr(corrida, {"us-east-1": [ec2("i-1", "a")]},
                                  pings={"i-1": "Online"})
        run_script.assert_not_called()
        self.assertEqual(corrida.line_ids.ssm_state, "online")

    def test_discovery_queda_en_la_bitacora(self):
        corrida = self._corrida()
        self._correr(corrida, {"us-east-1": [ec2("i-1", "bj-srv")]},
                     pings={"i-1": "Online"},
                     run=lambda *a, **k: discovery([UNIT_BJ]))
        maquina = self.Ec2.search([("aws_instance_id", "=", "i-1")])
        self.assertTrue(self.env["primate.cloud.operation.log"].search(
            [("action_type", "=", "instance_discover"),
             ("resource_id", "=", maquina.id)]))

    # ------------------------------------------------------------------
    # 4. Clasificación
    # ------------------------------------------------------------------
    def test_centinela_archivado_y_reusado(self):
        uno = self.Run._proyecto_centinela()
        dos = self.Run._proyecto_centinela()
        self.assertEqual(uno, dos)
        self.assertFalse(uno.active)
        self.assertNotIn(uno, self.env["primate.cloud.project"].search([]),
                         "no aparece en las vistas de negocio")

    def test_filtro_sin_clasificar_y_clasificar_con_un_write(self):
        corrida = self._corrida()
        self._correr(corrida, {"us-east-1": [ec2("i-1", "bj-srv")]},
                     pings={"i-1": "Online"},
                     run=lambda *a, **k: discovery([UNIT_BJ]))
        Inst = self.env["primate.cloud.instance"]
        sin = Inst.search([("project_id.active", "=", False)])
        self.assertTrue(sin)
        maquina = self.Ec2.search([("aws_instance_id", "=", "i-1")])
        self.assertIn(maquina, self.Ec2.search(
            ["|", "|", ("environment_id", "=", False),
             ("environment_id.instance_ids.project_id.active", "=", False),
             "&", ("environment_id.project_id", "=", False),
             ("environment_id.instance_ids", "=", False)]))
        cliente = self.env["primate.cloud.project"].create({
            "name": "Barraca Julia",
            "partner_id": self.env["res.partner"].create({"name": "BJ"}).id})
        sin.write({"project_id": cliente.id})
        self.assertFalse(Inst.search([("project_id.active", "=", False),
                                      ("id", "in", sin.ids)]))
        # Clasificado, el pase a managed queda disponible.
        self.assertFalse(maquina._instancias_sin_clasificar())

    # ------------------------------------------------------------------
    # 5. El wizard
    # ------------------------------------------------------------------
    def _wizard(self, **vals):
        base = {"account_id": self.account.id, "regions": "us-east-1"}
        base.update(vals)
        return self.env["primate.cloud.server.import.wizard"].create(base)

    def test_wizard_admin_only(self):
        operador = new_test_user(
            self.env, login="pcm_op_import",
            groups="primate_cloud_manager.group_cloud_operator")
        with self.assertRaises(UserError):
            self.account.with_user(operador).action_import_servers()

    def test_wizard_region_invalida(self):
        with self.assertRaises(UserError) as e:
            self._wizard(regions="us-east-1, marte-1").action_import()
        self.assertIn("marte-1", str(e.exception))

    def test_wizard_exige_cuenta_conectada(self):
        self.account.connection_state = "error"
        with self.assertRaises(UserError):
            self._wizard().action_import()

    def test_wizard_crea_la_corrida_y_la_encola(self):
        with mock.patch.object(type(self.Run), "with_delay") as delay:
            accion = self._wizard(all_regions=True, discover=False).action_import()
            delay.assert_called_once()
        corrida = self.Run.browse(accion["res_id"])
        self.assertEqual(corrida.state, "draft")
        self.assertIn("sa-east-1", corrida.regions)
        self.assertFalse(corrida.discover)
