"""Versión PDF del informe: aplana informe.md (includes, figuras) -> HTML -> PDF con Chrome sin cabeza.

    uv run --with markdown python report/build_pdf.py     ->  report/DFSha-Informe.pdf
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from build_report import MERMAID, cover_lines, load_portada  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CSS = """
@page { size: Letter; margin: 20mm 18mm 18mm 18mm; }
body { font-family: -apple-system, 'Segoe UI', Calibri, Helvetica, Arial, sans-serif; font-size: 10.3pt; line-height: 1.42; color: #0f172a; }
h1 { font-size: 19pt; color: #1d4ed8; border-bottom: 2px solid #1d4ed8; padding-bottom: 3px; margin-top: 26pt; page-break-before: always; }
h1:first-of-type { page-break-before: auto; }
h2 { font-size: 14pt; color: #1d4ed8; margin-top: 16pt; } h3 { font-size: 12pt; margin-top: 12pt; } h4 { font-size: 10.8pt; color: #475569; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0; font-size: 8.6pt; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th { background: #1d4ed8; color: white; text-align: left; padding: 4px 6px; }
td { border: 1px solid #cbd5e1; padding: 3px 6px; vertical-align: top; } tr:nth-child(even) td { background: #f1f5f9; }
code { font-family: Menlo, Consolas, monospace; font-size: 8.6pt; background: #f1f5f9; color: #9f1239; padding: 0 2px; border-radius: 2px; }
pre { background: #f1f5f9; padding: 7px 9px; font-size: 7.6pt; line-height: 1.25; border-left: 3px solid #94a3b8; white-space: pre-wrap; page-break-inside: avoid; }
pre code { background: none; color: #0f172a; padding: 0; font-size: 7.6pt; }
img { max-width: 100%; display: block; margin: 8pt auto 2pt; page-break-inside: avoid; }
p.fig { text-align: center; font-style: italic; color: #64748b; font-size: 8.8pt; margin-top: 0; }
.cover { height: 230mm; page-break-after: always; display: flex; flex-direction: column; justify-content: center; } .cover h1 { border: none; font-size: 44pt; page-break-before: auto; margin: 0; }
.cover .sub { font-size: 16pt; margin-bottom: 26pt; } .cover p { color: #475569; font-size: 12pt; margin: 4pt 0; }
blockquote { border-left: 3px solid #cbd5e1; margin-left: 0; padding-left: 10px; color: #475569; }
"""


def flatten(path: Path, shift: int = 0, skip_h1: bool = False, counter: list[int] | None = None, mer: dict | None = None) -> list[str]:
    counter = counter if counter is not None else [0]
    out: list[str] = []
    lines = path.read_text().splitlines()
    first_h1_skipped = not skip_h1
    i = 0
    while i < len(lines):
        ln = lines[i]
        st = ln.strip()
        m = re.match(r"\{\{include\s+(\S+)(.*)\}\}", st)
        if m:
            opts = m.group(2)
            sh = re.search(r"shift=(-?\d+)", opts)
            inc = (ROOT / m.group(1)).resolve()
            out += flatten(inc, int(sh.group(1)) if sh else shift, "skip_h1" in opts, counter, mer)
            i += 1
            continue
        if st.startswith("```"):
            lang = st[3:].strip()
            buf = [ln]
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            buf.append(lines[i] if i < len(lines) else "```")
            i += 1
            if lang == "mermaid":
                figs = (mer or {}).get(path.name, [])
                if figs:
                    img, cap = figs.pop(0)
                    counter[0] += 1
                    out += ["", f"![{cap}]({(ROOT / img).as_uri()})", f"<p class='fig'>Figura {counter[0]}. {cap}</p>", ""]
            else:
                out += [""] + buf + [""]
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            lvl = len(m.group(1))
            if lvl == 1 and not first_h1_skipped:
                first_h1_skipped = True
            else:
                out.append("#" * min(max(lvl + shift, 1), 6) + " " + m.group(2))
            i += 1
            continue
        m = re.match(r"^!\[(.*?)\]\((.*?)\)\s*$", st)
        if m:
            counter[0] += 1
            out += ["", f"![{m.group(1)}]({(ROOT / m.group(2)).as_uri()})", f"<p class='fig'>Figura {counter[0]}. {m.group(1)}</p>", ""]
            i += 1
            continue
        out.append(ln)
        i += 1
    return out


def main() -> None:
    import cloud_section

    cloud_section.main()
    portada = load_portada()
    cv = cover_lines(portada)
    mer = {k: list(v) for k, v in MERMAID.items()}
    md = "\n".join(flatten(ROOT / "report" / "informe.md", mer=mer))
    fixed: list[str] = []
    for ln in md.split("\n"):  # una tabla debe ir precedida de línea en blanco
        if ln.lstrip().startswith("|") and fixed and fixed[-1].strip() and not fixed[-1].lstrip().startswith("|"):
            fixed.append("")
        fixed.append(ln)
    md = "\n".join(fixed)
    md = re.sub(r"\\\|", "&#124;", md)
    body = markdown.markdown(md, extensions=["tables", "fenced_code", "sane_lists"])
    hoy = dt.date.today()
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    cover = f"""<div class='cover'><h1>DFSha</h1><div class='sub'>Sistema de archivos distribuido con alta disponibilidad, rendimiento y seguridad</div>
<p><b>{cv["universidad"]}</b></p>
<p><b>Proyecto 1</b> · ST0263 Tópicos Especiales en Telemática / SI3007 Sistemas Distribuidos · 2026-2</p>
<p><b>Opción 1</b> · Arquitectura Cliente/Servidor con servidor compuesto y distribuido (S2S)</p><p>&nbsp;</p>
<p><b>{cv["equipo"]}</b></p><p><b>Integrantes:</b> {cv["integrantes"]}</p><p><b>{cv["profesores_label"]}:</b> {cv["profesores"]}</p>
<p><b>Fecha:</b> {hoy.day} de {meses[hoy.month - 1]} de {hoy.year}</p><p><b>Repositorio:</b> {cv["repositorio"]}</p></div>"""
    html = f"<!doctype html><html lang='es'><head><meta charset='utf-8'><title>DFSha — Informe técnico</title><style>{CSS}</style></head><body>{cover}{body}</body></html>"
    h = ROOT / "report" / "DFSha-Informe.html"
    h.write_text(html)
    pdf = ROOT / "report" / "DFSha-Informe.pdf"
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--allow-file-access-from-files", f"--print-to-pdf={pdf}", "--no-pdf-header-footer", h.as_uri()], check=True, capture_output=True, timeout=180)
    print(f"escrito {pdf} ({pdf.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
