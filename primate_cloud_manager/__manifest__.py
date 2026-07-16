# -*- coding: utf-8 -*-
{
    "name": "Primate Cloud Manager",
    "version": "19.0.1.1.0",
    "author": "PrimateUY",
    "website": "https://primate.uy",
    "category": "Technical",
    "license": "AGPL-3",
    "summary": "Administración de infraestructura AWS para entornos Odoo.",
    "description": """
Primate Cloud Manager
=====================
Módulo de gobierno de infraestructura cloud. Permite gestionar desde Odoo
cuentas AWS, proyectos, entornos Odoo (EC2 + PostgreSQL/RDS), repositorios
Git, despliegues, DNS Route53, respaldos S3, métricas CloudWatch y costos
Cost Explorer. Diseñado para operar sin plataformas intermedias de hosting.
Arquitectura extensible para Azure y GCP en fases futuras.
    """,
    # queue_job (OCA rama 19.0) es obligatorio: todo flujo largo / llamada AWS va en jobs.
    "depends": ["base", "mail", "queue_job"],
    "external_dependencies": {
        "python": ["boto3", "botocore", "paramiko", "cryptography", "PyGithub"],
    },
    "data": [
        # Seguridad
        "security/primate_cloud_manager_groups.xml",
        "security/ir.model.access.csv",
        # Datos
        "data/primate_cloud_sequence.xml",
        "data/primate_cloud_cron.xml",
        "data/primate_cloud_backup_policy_data.xml",
        # Vistas
        "views/pcm_app_action.xml",
        "views/primate_cloud_dashboard_views.xml",
        "views/primate_cloud_operation_log_views.xml",
        "views/primate_cloud_account_views.xml",
        "views/primate_cloud_project_views.xml",
        "views/primate_cloud_environment_views.xml",
        "views/primate_cloud_instance_views.xml",
        "views/primate_cloud_ec2_instance_views.xml",
        "views/primate_cloud_database_views.xml",
        "views/primate_cloud_backup_policy_views.xml",
        "views/primate_cloud_backup_views.xml",
        "views/primate_cloud_cost_entry_views.xml",
        "views/primate_cloud_dns_record_views.xml",
        "views/primate_cloud_repository_views.xml",
        "views/primate_cloud_deployment_views.xml",
        # Wizards
        "wizard/primate_cloud_ec2_command_wizard_views.xml",
        "wizard/primate_cloud_ec2_terminate_wizard_views.xml",
        "wizard/primate_cloud_ec2_create_wizard_views.xml",
        "wizard/primate_cloud_provision_wizard_views.xml",
        "wizard/primate_cloud_staging_create_wizard_views.xml",
        "wizard/primate_cloud_backup_restore_wizard_views.xml",
        "wizard/primate_cloud_staging_refresh_wizard_views.xml",
        "wizard/primate_cloud_dns_record_wizard_views.xml",
        "wizard/primate_cloud_dns_delete_wizard_views.xml",
        "wizard/primate_cloud_instance_create_wizard_views.xml",
        # Menús (último: referencia acciones de las vistas anteriores)
        "views/primate_cloud_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "primate_cloud_manager/static/src/scss/pcm_theme.scss",
            "primate_cloud_manager/static/src/scss/pcm_notifier.scss",
            "primate_cloud_manager/static/src/scss/dashboard.scss",
            "primate_cloud_manager/static/src/app/pcm_app.scss",
            "primate_cloud_manager/static/src/js/pcm_status.js",
            "primate_cloud_manager/static/src/js/pcm_notifier.js",
            "primate_cloud_manager/static/src/js/dashboard.js",
            "primate_cloud_manager/static/src/app/pcm_actions.js",
            "primate_cloud_manager/static/src/app/components/status_badge.js",
            "primate_cloud_manager/static/src/app/components/wizard_drawer.js",
            "primate_cloud_manager/static/src/app/screens/inicio.js",
            "primate_cloud_manager/static/src/app/screens/costos.js",
            "primate_cloud_manager/static/src/app/screens/entornos.js",
            "primate_cloud_manager/static/src/app/screens/entorno_detalle.js",
            "primate_cloud_manager/static/src/app/screens/servidor_detalle.js",
            "primate_cloud_manager/static/src/app/screens/instancia_detalle.js",
            "primate_cloud_manager/static/src/app/screens/base_datos_detalle.js",
            "primate_cloud_manager/static/src/app/screens/repositorio_detalle.js",
            "primate_cloud_manager/static/src/app/screens/despliegue_detalle.js",
            "primate_cloud_manager/static/src/app/screens/dns_detalle.js",
            "primate_cloud_manager/static/src/app/screens/cuenta_detalle.js",
            "primate_cloud_manager/static/src/app/screens/proyecto_detalle.js",
            "primate_cloud_manager/static/src/app/pcm_app.js",
            "primate_cloud_manager/static/src/xml/pcm_status.xml",
            "primate_cloud_manager/static/src/xml/dashboard.xml",
            "primate_cloud_manager/static/src/app/components/status_badge.xml",
            "primate_cloud_manager/static/src/app/components/wizard_drawer.xml",
            "primate_cloud_manager/static/src/app/screens/inicio.xml",
            "primate_cloud_manager/static/src/app/screens/costos.xml",
            "primate_cloud_manager/static/src/app/screens/entornos.xml",
            "primate_cloud_manager/static/src/app/screens/entorno_detalle.xml",
            "primate_cloud_manager/static/src/app/screens/servidor_detalle.xml",
            "primate_cloud_manager/static/src/app/screens/instancia_detalle.xml",
            "primate_cloud_manager/static/src/app/screens/base_datos_detalle.xml",
            "primate_cloud_manager/static/src/app/screens/repositorio_detalle.xml",
            "primate_cloud_manager/static/src/app/screens/despliegue_detalle.xml",
            "primate_cloud_manager/static/src/app/screens/dns_detalle.xml",
            "primate_cloud_manager/static/src/app/screens/cuenta_detalle.xml",
            "primate_cloud_manager/static/src/app/screens/proyecto_detalle.xml",
            "primate_cloud_manager/static/src/app/pcm_app.xml",
        ],
    },
    "application": True,
    "installable": True,
    "auto_install": False,
}
