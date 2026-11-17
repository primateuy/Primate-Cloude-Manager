# -*- coding: utf-8 -*-
"""Precarga del ENSAYO de Alta na Pipa en pcm_demo (sandbox, cuenta personal).

Se corre con el shell de Odoo sobre pcm_demo (módulo ya al día):

    .venv/bin/python ../forum/_shared/community/odoo-bin shell \
        -c PrimateCloudeManager.conf -d pcm_demo --no-http \
        < infra/precargar_alta_sandbox.py

Idempotente: reutiliza lo que ya existe y re-guarda la config del wizard.
No toca AWS: deja el entorno en Borrador con el formulario precargado y los
4 repos NO públicos creados SIN token (el PAT lo pega Daryl en cada ficha).
odoo/odoo NO lleva ficha: lo cubre el runtime compartido de PCM (primera
entrada del addons_path).

Config = runbook docs/DEPLOY_AWS.md de alta_na_pipa (rama 19.0 mergeada) +
decisiones del pedido: t3.medium, 30 GB gp3, Ubuntu 24.04, PG16 local,
wkhtmltopdf 0.12.6.1-3 del release, -i con pt_BR, openpyxl, limits 600/1200,
sslip.io + HTTPS (el dominio definitivo es cosa de PRODUCCIÓN, no del ensayo).
"""

CUENTA = "Personal Prueba Real"          # sandbox 603011031378
SLUG = "altanapipa"
BASE_REPOS = "/opt/pcm/instances/%s/repos" % SLUG
# Orden EXPLÍCITO del addons_path (runbook §3): el runtime va primero solo.
REPOS = [
    ("webOCA", "primateuy", "oca", 10),
    ("surveyOCA", "primateuy", "oca", 20),
    ("primate_portal", "PrimateUy-SAS", "custom_primate", 30),
    ("alta_na_pipa", "PrimateUy-SAS", "custom_client", 40),
]
INIT_MODULES = "alta_na_pipa_website,alta_awards_portal,alta_ranking_portal,web_responsive"

Account = env["primate.cloud.account"]
Project = env["primate.cloud.project"]
Environment = env["primate.cloud.environment"]
Repository = env["primate.cloud.repository"]
Wizard = env["primate.cloud.provision.wizard"]

acc = Account.search([("name", "=", CUENTA)], limit=1)
assert acc, "No existe la cuenta «%s» en pcm_demo: frenar y revisar." % CUENTA

partner = env["res.partner"].search([("name", "=", "Alta na Pipa")], limit=1)
if not partner:
    partner = env["res.partner"].create({"name": "Alta na Pipa"})
    print("creado partner Alta na Pipa (%d)" % partner.id)

proj = Project.search([("name", "=", "Alta na Pipa (sandbox)")], limit=1)
if not proj:
    proj = Project.create({
        "name": "Alta na Pipa (sandbox)", "account_id": acc.id,
        "partner_id": partner.id,
    })
    print("creado proyecto Alta na Pipa (sandbox) (%d)" % proj.id)

srv = Environment.search([("name", "=", SLUG),
                          ("project_id", "=", proj.id)], limit=1)
if not srv:
    srv = Environment.create({
        "name": SLUG,
        "project_id": proj.id,
        "account_id": acc.id,
        "env_type": "production",    # fiel al montaje real (dbfilter exacto)
        "odoo_version": "19",
        "odoo_edition": "community",
    })
    print("creado entorno %s (%d)" % (SLUG, srv.id))
else:
    print("entorno %s ya existía (%d, estado %s)" % (SLUG, srv.id, srv.state))

inst = srv.primary_instance_id
assert inst, "el entorno quedó sin instancia primaria"
if inst.slug != SLUG:
    inst.write({"slug": SLUG})
print("instancia %d · slug=%s" % (inst.id, inst.slug))

wiz = Wizard.create({
    "environment_id": srv.id,
    "region": "us-east-1",
    "instance_name": SLUG,
    "instance_type": "t3.medium",
    "os_type": "ubuntu_24",
    "disk_size_gb": 30,
    "db_mode": "local_pg",
    "db_name": "altanapipa",
    "pg_version": "16",
    "elastic_ip": True,
    "sslip_domain": True,             # el dominio definitivo es de PRODUCCIÓN
    "ssl_auto": True,
    "create_dns": False,
    "domain": False,
    "admin_password": "sandbox-alta-2026",   # sandbox descartable
    "pcm_profile": False,
    "cw_monitoring": False,           # el agente CW es del montaje de PRODUCCIÓN
    # Montaje a la carta (gaps a-c + límites del runbook §6)
    "init_modules": INIT_MODULES,
    "load_language": "pt_BR",
    "pip_packages": "openpyxl",
    "wkhtml_version": "0.12.6.1-3",
    "limit_time_cpu": 600,
    "limit_time_real": 1200,
})
srv._save_provision_config(wiz._raw_values())
print("formulario precargado y guardado en el entorno (provision_config)")

for nombre, org, tipo, seq in REPOS:
    rep = Repository.search([
        ("environment_id", "=", srv.id), ("name", "=", nombre)], limit=1)
    if not rep:
        rep = Repository.create({
            "name": nombre,
            "environment_id": srv.id,
            "repo_type": tipo,
            "github_url": "https://github.com/%s/%s" % (org, nombre),
            "organization": org,
            "configured_branch": "19.0",
            "local_path": "%s/%s" % (BASE_REPOS, nombre),
            "sequence": seq,
            # SIN token: el PAT lo pega Daryl en cada ficha.
        })
        print("creado repo %s (%d, seq %d), SIN token" % (nombre, rep.id, seq))
    else:
        rep.write({"sequence": seq, "configured_branch": "19.0",
                   "local_path": "%s/%s" % (BASE_REPOS, nombre)})
        print("repo %s ya existía (%d): orden/rama/ruta re-afirmados" % (nombre, rep.id))

alta = Repository.search([("environment_id", "=", srv.id),
                          ("name", "=", "alta_na_pipa")], limit=1)
srv.write({"modules_ref_repo_id": alta.id,
           "modules_ref_path": "docs/MODULES_PROD.txt"})
print("referencia de verificación: alta_na_pipa · docs/MODULES_PROD.txt (59)")

env.cr.commit()
print("LISTO: entorno %s en Borrador, 4 repos sin token, wizard precargado." % SLUG)
print("Falta: PAT en las 4 fichas (Daryl) → Aprovisionar (ensayo C.2).")
