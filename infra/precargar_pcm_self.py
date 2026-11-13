# -*- coding: utf-8 -*-
"""Precarga del montaje de pcm-self en pcm_produccion (formulario listo).

LO CORRE DARYL (regla del runbook: Claude no ejecuta nada contra la base
productiva). Antes hace falta el módulo al día (19.0.1.8.0, trae el
monitoreo CloudWatch):

    ~/Odoo/clients/19.0/pcm_produccion/levantar.sh -u primate_cloud_manager --stop-after-init --no-http
    ~/Odoo/clients/19.0/pcm_produccion/levantar.sh shell --no-http \
        < ~/Odoo/clients/19.0/Primate-Cloude-Manager/infra/precargar_pcm_self.py

Idempotente: si algo ya existe, lo reutiliza y solo re-guarda la config del
wizard. No toca AWS: deja el entorno en Borrador con el formulario
precargado (GUIA_DIA_D_PCM_SELF.md, config de MUDANZA_PCM_SELF.md §8).

Lo que queda VACÍO a propósito (lo cargás vos en el wizard, el día D):
  - contraseña maestra (admin_password) → llavero;
  - el PAT del repo (campo Token del repo; desde ~/.pcm_produccion/pat_mudanza_pcm.txt).
"""

CUENTA = "pcm-prod-prov"
REPO_URL = "https://github.com/primateuy/Primate-Cloude-Manager"

Account = env["primate.cloud.account"]
Project = env["primate.cloud.project"]
Environment = env["primate.cloud.environment"]
Repository = env["primate.cloud.repository"]
Wizard = env["primate.cloud.provision.wizard"]

acc = Account.search([("name", "=", CUENTA)], limit=1)
assert acc, "No existe la cuenta «%s» en PCM: frenar y revisar." % CUENTA

partner = env["res.partner"].search([("name", "=", "PrimateUY")], limit=1)
if not partner:
    partner = env["res.partner"].create({"name": "PrimateUY"})
    print("creado partner PrimateUY (%d)" % partner.id)

proj = Project.search([("name", "=", "Primate")], limit=1)
if not proj:
    proj = Project.create({
        "name": "Primate", "account_id": acc.id, "partner_id": partner.id,
    })
    print("creado proyecto Primate (%d)" % proj.id)

srv = Environment.search([("name", "=", "pcm-self")], limit=1)
if not srv:
    srv = Environment.create({
        "name": "pcm-self",
        "project_id": proj.id,
        "account_id": acc.id,
        "env_type": "production",     # clave: el restore NO neutraliza
        "odoo_version": "19",
        "odoo_edition": "community",
    })
    print("creado entorno pcm-self (%d)" % srv.id)
else:
    print("entorno pcm-self ya existía (%d, estado %s)" % (srv.id, srv.state))

inst = srv.primary_instance_id
assert inst, "el entorno quedó sin instancia primaria"
if inst.slug != "pcm":
    inst.write({"slug": "pcm"})   # «Crear DNS ahora» propone <slug>.erp.primateuy.com
print("instancia %d · slug=%s · tipo=%s" % (inst.id, inst.slug, inst.env_type))

wiz = Wizard.create({
    "environment_id": srv.id,
    "region": "us-east-1",            # misma región que bj-uat
    "instance_name": "pcm",
    "instance_type": "t3.medium",
    "os_type": "ubuntu_24",
    "disk_size_gb": 50,               # gp3 (la AMI de Canonical es ebs-gp3)
    "db_mode": "local_pg",
    "db_name": "pcm_produccion",
    "pg_version": "16",
    "elastic_ip": True,               # IP fija
    "sslip_domain": True,             # sslip.io automático al montar
    "ssl_auto": True,                 # HTTPS al terminar
    "create_dns": False,              # el dominio definitivo va después, por «Crear DNS ahora»
    "domain": False,
    "admin_password": False,          # la maestra la cargás vos en el wizard
    "pcm_profile": True,              # perfil PCM: pip extra + conf + clave Fernet en el server
    "cw_monitoring": True,            # monitoreo CloudWatch (pedido INCO)
    "cw_ssm_parameter": False,        # config embebida (archivo local)
})
srv._save_provision_config(wiz._raw_values())
print("formulario precargado y guardado en el entorno (provision_config)")

rep = Repository.search([
    ("environment_id", "=", srv.id), ("name", "=", "primate_cloud_manager"),
], limit=1)
if not rep:
    rep = Repository.create({
        "name": "primate_cloud_manager",
        "environment_id": srv.id,
        "repo_type": "custom_primate",
        "github_url": REPO_URL,
        "organization": "primateuy",
        "configured_branch": "19.0",
        # SIN token: el PAT lo pegás vos en la ficha del repo el día D.
    })
    print("creado repo primate_cloud_manager (%d), SIN token" % rep.id)
else:
    print("repo primate_cloud_manager ya existía (%d)" % rep.id)

env.cr.commit()
print("LISTO: entorno pcm-self en Borrador con el formulario precargado.")
print("Falta solo (día D): maestra en el wizard + PAT en el repo + Aprovisionar.")
