# -*- coding: utf-8 -*-
"""Convierte un Markdown (el README de INCO) a PDF A4 con tablas legibles.

Markdown → HTML (python-markdown, tablas y bloques de código) → PDF con
Chromium (Playwright). Sin red: todo el estilo va adentro.

Uso: python infra/readme_a_pdf.py <entrada.md> <salida.pdf>
"""
import html
import re
import sys

import markdown
from playwright.sync_api import sync_playwright

ESTILO = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm; }
body { font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif;
       font-size: 10.5pt; line-height: 1.45; color: #1f2937; }
h1 { font-size: 20pt; color: #312e81; margin: 0 0 10pt; }
h2 { font-size: 14pt; color: #3730a3; margin: 18pt 0 6pt;
     border-bottom: 1px solid #c7d2fe; padding-bottom: 3pt; }
h3 { font-size: 12pt; margin: 12pt 0 4pt; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt;
        font-size: 9pt; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th, td { border: 1px solid #c7d2fe; padding: 4pt 6pt; vertical-align: top;
         text-align: left; }
th { background: #eef2ff; }
code { font-family: Menlo, Consolas, monospace; font-size: 8.8pt;
       background: #f3f4f6; padding: 0 2pt; border-radius: 2pt; }
td code { overflow-wrap: anywhere; word-break: break-word; }
td:first-child { width: 28%; }
pre { background: #f3f4f6; padding: 6pt 8pt; border-radius: 4pt;
      white-space: pre-wrap; word-break: break-all; }
pre code { background: none; padding: 0; }
hr { border: 0; border-top: 1px solid #e5e7eb; margin: 12pt 0; }
"""


CERCA = re.compile(r"(?m)^(?P<ind>[ \t]*)```[^\n]*\n(?P<codigo>.*?)^(?P=ind)```[ \t]*$",
                   re.S)


def _bloques_aparte(texto):
    """python-markdown no reconoce bloques con cercas dentro de una lista:
    cada bloque se cambia por una marca y vuelve como <pre> ya escapado."""
    bloques = []

    def marcar(m):
        ind = m.group("ind")
        lineas = [l[len(ind):] if l.startswith(ind) else l
                  for l in m.group("codigo").rstrip("\n").split("\n")]
        bloques.append("<pre><code>%s</code></pre>"
                       % html.escape("\n".join(lineas)))
        return "%sPCMBLOQUE%dPCM" % (ind, len(bloques) - 1)

    return CERCA.sub(marcar, texto), bloques


def main(entrada, salida):
    with open(entrada, encoding="utf-8") as fh:
        texto = fh.read()
    sin_bloques, bloques = _bloques_aparte(texto)
    # Las listas numeradas del README siguen con 3 espacios (válido en
    # GitHub); python-markdown pide 4 para que el ítem no se corte.
    sin_bloques = re.sub(r"(?m)^   (?=\S)", "    ", sin_bloques)
    # Un ítem numerado pegado a un párrafo se toma como continuación: se
    # separa con una línea en blanco.
    sin_bloques = re.sub(r"(?m)(?<=\S)\n(?=\d+\. )", "\n\n", sin_bloques)
    cuerpo = markdown.markdown(sin_bloques, extensions=["tables"])
    for i, bloque in enumerate(bloques):
        marca = "PCMBLOQUE%dPCM" % i
        cuerpo = cuerpo.replace("<p>%s</p>" % marca, bloque).replace(marca, bloque)
    titulo = texto.splitlines()[0].lstrip("# ").strip() if texto else ""
    pagina = ("<!doctype html><html lang='es'><head><meta charset='utf-8'>"
              "<title>%s</title><style>%s</style></head><body>%s</body></html>"
              % (html.escape(titulo), ESTILO, cuerpo))
    with sync_playwright() as p:
        navegador = p.chromium.launch()
        page = navegador.new_page()
        page.set_content(pagina, wait_until="load")
        page.pdf(path=salida, format="A4", print_background=True,
                 display_header_footer=True, header_template="<span></span>",
                 footer_template=(
                     "<div style='font-size:8pt;width:100%;text-align:center;"
                     "color:#6b7280'><span class='pageNumber'></span> / "
                     "<span class='totalPages'></span></div>"),
                 margin={"top": "18mm", "bottom": "18mm",
                         "left": "16mm", "right": "16mm"})
        navegador.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("uso: readme_a_pdf.py <entrada.md> <salida.pdf>")
    main(sys.argv[1], sys.argv[2])
