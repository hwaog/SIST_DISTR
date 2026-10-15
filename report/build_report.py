"""Ensambla el informe técnico (Word) a partir de report/informe.md y docs/*.md.

    uv run python report/figs.py && uv run python report/build_report.py   ->  report/DFSha-Informe.docx
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from md2docx import Builder

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
OUT = ROOT / "report" / "DFSha-Informe.docx"

MERMAID = {
    "ARQUITECTURA.md": [
        ("report/figs/arquitectura.png", "Arquitectura de DFSha: plano de control (Raft), plano de datos y enlaces seguros."),
        ("report/figs/secuencia_escritura.png", "Secuencia de escritura de un archivo: bloques en paralelo, tubería de replicación y commit atómico."),
        ("report/figs/reparacion.png", "Ciclo de autorreparación tras la caída de un DataNode."),
    ],
}


def load_portada() -> dict:
    return json.loads((ROOT / "report" / "portada.json").read_text())


def cover_lines(portada: dict) -> dict:
    """Textos de la portada (compartidos por el Word y el PDF). Admite `profesores` (lista) o el antiguo `profesor`."""
    profs = portada.get("profesores") or [portada.get("profesor", "")]
    return {
        "universidad": portada.get("universidad", ""),
        "equipo": portada.get("equipo", ""),
        "integrantes": "; ".join(portada["integrantes"]),
        "repositorio": "[agregar enlace del repositorio]" if "COMPLETAR" in portada.get("repositorio", "COMPLETAR") else portada["repositorio"],
        "profesores_label": "Profesores" if len(profs) > 1 else "Profesor",
        "profesores": " y ".join(p for p in profs if p),
    }


def warn_placeholders(portada: dict) -> None:
    pend = [k for k, v in portada.items() if k != "video" and "COMPLETAR" in json.dumps(v)]
    if pend:
        print(f"⚠  ATENCIÓN: faltan datos de portada en report/portada.json: {', '.join(pend)}")


def main() -> None:
    import cloud_section

    cloud_section.main()  # DESPLIEGUE_NUBE.md y ESTADO_VERIFICACION.md reflejan la evidencia existente
    portada = load_portada()
    warn_placeholders(portada)
    b = Builder(ROOT, MERMAID)
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    hoy = dt.date.today()
    cv = cover_lines(portada)
    b.cover(
        "DFSha",
        "Sistema de archivos distribuido con alta disponibilidad, rendimiento y seguridad",
        [
            f"**{cv['universidad']}**" if cv["universidad"] else "",
            "**Proyecto 1** · ST0263 Tópicos Especiales en Telemática / SI3007 Sistemas Distribuidos · 2026-2",
            "**Opción 1** · Arquitectura Cliente/Servidor con servidor compuesto y distribuido (S2S)",
            "",
            f"**{cv['equipo']}**" if cv["equipo"] else "",
            "**Integrantes:** " + cv["integrantes"],
            f"**{cv['profesores_label']}:** {cv['profesores']}",
            f"**Fecha:** {hoy.day} de {meses[hoy.month - 1]} de {hoy.year}",
            f"**Repositorio:** {cv['repositorio']}",
        ],
    )
    b.toc()
    b.markdown((ROOT / "report" / "informe.md").read_text(), src=ROOT / "report" / "informe.md")
    b.save(OUT)
    print(f"escrito {OUT} ({OUT.stat().st_size / 1024:.0f} KiB, {b.fig_n} figuras)")


if __name__ == "__main__":
    main()
