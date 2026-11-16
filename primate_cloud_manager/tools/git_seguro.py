# -*- coding: utf-8 -*-
"""git EN LOS SERVIDORES: siempre como el dueño del clon y con safe.directory.

git se niega a operar un repo de otro dueño («detected dubious ownership»):
los clones de PCM pertenecen a «odoo» y los comandos SSM corren como root.
Todo git que PCM manda a un servidor pasa por acá: ``sudo -u odoo`` más
``-c safe.directory=<ruta>`` (config protegida: git la honra por línea de
comandos desde 2.37; Ubuntu 24.04 trae 2.43) — cinturón y tiradores, por los
clones viejos que pudieran tener otro dueño hasta que se reparen.
"""
import shlex

USUARIO_ODOO = "odoo"


def git_servidor(path, subcomando, usuario=USUARIO_ODOO):
    """Comando git completo para correr vía SSM sobre un clon del servidor."""
    q = shlex.quote(path)
    return "sudo -u %s git -c safe.directory=%s -C %s %s" % (
        usuario, q, q, subcomando)
