#!/usr/bin/env python3
"""Lista de verificación previa a la ENTREGA: qué falta y qué está mal, en un vistazo.

    uv run python scripts/check_entrega.py            # revisa todo
    uv run python scripts/check_entrega.py --tests    # además corre la suite completa de pruebas (≈4 min)

✓ cumplido · ! pendiente/recomendado · ✗ error que hay que corregir.  Código de salida ≠ 0 si hay ✗.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET_PATTERNS = [r"\.key$", r"\.pem$", r"(^|/)\.env$", r"(^|/)admin\.pass$", r"(^|/)cluster\.key$", r"(^|/)state/", r"(^|/)bundles/", r"(^|/)\.venv/", r"\.pfx$", r"id_rsa"]
BIG = 8 * 1024 * 1024


def sh(*cmd: str) -> tuple[int, str]:
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def tracked_secret_files(files: list[str]) -> list[str]:
    """Archivos versionados que no deberían estarlo (claves, .env, secretos, el venv...)."""
    return [f for f in files if any(re.search(p, f) for p in SECRET_PATTERNS) and not f.startswith("docs/") and not f.endswith(".example")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", action="store_true")
    args = ap.parse_args()
    res: list[tuple[str, str, str]] = []

    def add(level: str, what: str, hint: str = "") -> None:
        res.append((level, what, hint))

    # 1) portada
    por = json.loads((ROOT / "report" / "portada.json").read_text())
    falta = [k for k, v in por.items() if k != "video" and "COMPLETAR" in json.dumps(v)]
    add("✗" if falta else "✓", "Portada del informe (integrantes, profesor, repositorio)", f"complete report/portada.json: {', '.join(falta)}" if falta else "")
    if "COMPLETAR" in json.dumps(por.get("video", "")):
        add("!", "Enlace al video en la portada (opcional)", "ponga la URL en report/portada.json → video")

    # 2) git
    rc, out = sh("git", "status", "--porcelain")
    add("✗" if out else "✓", "Árbol de trabajo limpio (todo commiteado)", "git add -A && git commit" if out else "")
    rc, remote = sh("git", "remote", "get-url", "origin")
    if rc != 0:
        add("✗", "Remoto de GitHub configurado", "git remote add origin <URL> && git push -u origin main")
    else:
        add("✓", f"Remoto configurado: {remote}")
        rc, st = sh("git", "status", "-sb")
        ahead = re.search(r"ahead (\d+)", st.splitlines()[0]) if st else None
        add("✗" if ahead or "[gone]" in st else "✓", "Todo lo commiteado está subido (push)", "git push" if ahead else "")
        if por["repositorio"] and "COMPLETAR" not in por["repositorio"] and por["repositorio"].rstrip("/").removesuffix(".git") not in remote.rstrip("/").removesuffix(".git"):
            add("!", "La URL de la portada coincide con el remoto", f"portada: {por['repositorio']}  remoto: {remote}")

    # 3) secretos / tamaño
    rc, ls = sh("git", "ls-files")
    files = ls.splitlines()
    bad = tracked_secret_files(files)
    add("✗" if bad else "✓", "Ningún secreto/clave/.env/.venv versionado", f"versionados: {bad[:5]}" if bad else "")
    big = [f for f in files if (ROOT / f).exists() and (ROOT / f).stat().st_size > BIG]
    add("!" if big else "✓", "Sin archivos grandes versionados (>8 MiB)", f"{big}" if big else "")

    # 4) evidencia de ejecuciones externas
    dk = ROOT / "docs" / "evidence" / "docker" / "latest" / "resultado.json"
    if dk.exists():
        d = json.loads(dk.read_text())
        add("✓" if d.get("ok") else "✗", f"Docker verificado de punta a punta ({d['fecha']})", "" if d.get("ok") else "revise docs/evidence/docker/latest/log.txt")
    else:
        add("!", "Docker NO verificado todavía", "uv run python scripts/docker_verify.py")
    cl = ROOT / "deploy" / "aws" / "evidence" / "latest" / "resultados.json"
    if cl.exists():
        c = json.loads(cl.read_text())
        add("✓" if c.get("ok") else "✗", f"Validación en la nube ({c['fecha']})", "" if c.get("ok") else "revise deploy/aws/evidence/latest/log.txt")
        caps = [p for p in (ROOT / "deploy/aws/evidence/capturas").glob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg")] if (ROOT / "deploy/aws/evidence/capturas").exists() else []
        add("✓" if len(caps) >= 3 else "!", f"Capturas de pantalla de la nube ({len(caps)})", "" if len(caps) >= 3 else "guarde ≥3 capturas en deploy/aws/evidence/capturas/ (vea deploy/aws/README.md §9)")
        add("✓" if (ROOT / "bench/results_cloud/scaling.json").exists() else "!", "Benchmarks en VMs separadas", "" if (ROOT / "bench/results_cloud/scaling.json").exists() else "deploy/aws/bench-remote.sh")
    else:
        add("!", "Despliegue en la nube NO validado todavía", "siga deploy/aws/README.md y ejecute deploy/aws/validate.sh")

    # 5) informe al día
    docx, pdf = ROOT / "report" / "DFSha-Informe.docx", ROOT / "report" / "DFSha-Informe.pdf"
    srcs = [p for pat in ("docs/*.md", "report/*.md", "report/portada.json", "report/figs/*.png") for p in ROOT.glob(pat)]
    newest = max((p.stat().st_mtime for p in srcs), default=0)
    stale = not docx.exists() or not pdf.exists() or min(docx.stat().st_mtime, pdf.stat().st_mtime) < newest - 1
    add("✗" if stale else "✓", "Informe .docx y .pdf regenerados con el contenido actual", "uv run python report/build_report.py && uv run --with markdown python report/build_pdf.py" if stale else "")
    if docx.exists() and "COMPLETAR" in "".join(p.text for p in __import__("docx").Document(str(docx)).paragraphs[:40]):
        add("✗", "El informe aún contiene campos «COMPLETAR» en la portada", "complete report/portada.json y regenere")

    # 6) pruebas
    if args.tests:
        rc, out = sh(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider")
        add("✓" if rc == 0 else "✗", "Suite de pruebas: " + out.splitlines()[-1] if out else "Suite de pruebas", "")

    print("\nVERIFICACIÓN PREVIA A LA ENTREGA\n" + "─" * 60)
    for lvl, what, hint in res:
        print(f" {lvl} {what}" + (f"\n     → {hint}" if hint else ""))
    errs = sum(1 for lvl, *_ in res if lvl == "✗")
    pend = sum(1 for lvl, *_ in res if lvl == "!")
    print("─" * 60 + f"\n {errs} errores, {pend} pendientes/recomendaciones.")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
