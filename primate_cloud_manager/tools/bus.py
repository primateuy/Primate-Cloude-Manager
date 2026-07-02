# -*- coding: utf-8 -*-
"""Notificaciones en vivo al usuario vía el bus de Odoo.

Centraliza el envío de eventos al navegador del usuario que disparó una acción
asíncrona (queue_job), para mostrar resultado/errores en tiempo real y un
overlay bloqueante con progreso durante el aprovisionamiento. El front escucha
el canal en ``static/src/js/pcm_notifier.js``.

Todas las funciones envían al partner del usuario actual (``env.user``), que en
un job es el usuario que lo encoló.
"""

# Canal único del módulo. El componente OWL se suscribe a este tipo.
CHANNEL = "primate_cloud_manager.notification"


def _send(env, payload):
    """Envía un payload al bus del usuario actual (best-effort)."""
    partner = env.user.partner_id
    if partner:
        partner._bus_send(CHANNEL, payload)


def toast(env, message, title=None, ntype="info", sticky=False, reload=True):
    """Muestra una notificación (toast) y, por defecto, refresca la vista actual.

    Args:
        env: entorno Odoo.
        message (str): cuerpo del mensaje.
        title (str, optional): título.
        ntype (str): ``success`` / ``danger`` / ``warning`` / ``info``.
        sticky (bool): si queda fija hasta cerrarla (los errores convienen fijos).
        reload (bool): si refresca la vista actual (soft_reload) al recibirla.
    """
    _send(env, {
        "kind": "toast", "message": message, "title": title,
        "type": ntype, "sticky": sticky, "reload": reload,
    })


def provision_start(env, environment, title=None):
    """Abre el overlay bloqueante de aprovisionamiento para un entorno."""
    _send(env, {
        "kind": "provision_start", "env_id": environment.id,
        "title": title or environment.display_name,
    })


def provision_step(env, environment, step):
    """Agrega un paso al overlay de aprovisionamiento (progreso en vivo)."""
    _send(env, {"kind": "provision_step", "env_id": environment.id, "step": step})


def provision_done(env, environment, ok, message):
    """Cierra el overlay (habilita el cierre) y muestra el resultado final."""
    _send(env, {
        "kind": "provision_done", "env_id": environment.id,
        "ok": ok, "message": message,
    })
