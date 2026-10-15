"""Genera, a partir de la EVIDENCIA real, las secciones del informe que dependen de ejecuciones externas.

    uv run python report/cloud_section.py        ->  docs/DESPLIEGUE_NUBE.md  y  docs/ESTADO_VERIFICACION.md

* Si existe  deploy/aws/evidence/latest/resultados.json  (lo escribe  deploy/aws/validate.sh)  se documentan los
  resultados medidos en la nube; si no, la sección dice con claridad que el despliegue NO se ha ejecutado.
* Igual con docs/evidence/docker/latest/resultado.json (lo escribe scripts/docker_verify.py).
* Capturas: cualquier PNG/JPG en deploy/aws/evidence/capturas/ se incluye con su nombre como pie de figura.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVID = ROOT / "deploy" / "aws" / "evidence"
DOCKER_EVID = ROOT / "docs" / "evidence" / "docker"
BENCH_CLOUD = ROOT / "bench" / "results_cloud"
BENCH_LOCAL = ROOT / "bench" / "results"


def _latest(base: Path, fname: str) -> Path | None:
    cand = base / "latest" / fname
    if cand.exists():
        return cand
    runs = sorted(p for p in base.glob(f"*/{fname}")) if base.exists() else []
    return runs[-1] if runs else None


def _load(p: Path | None):
    return json.loads(p.read_text()) if p and p.exists() else None


def _num(x: float) -> str:
    return f"{x:,.1f}".replace(",", " ").replace(".", ",") if abs(x) < 100 else f"{x:,.0f}".replace(",", " ")


def render_cloud(cloud: dict | None, bench: dict[str, dict], local_scaling: dict | None) -> str:
    if not cloud:
        return """# Despliegue en la nube (AWS Academy)

> **Estado: pendiente de ejecución.** En el momento de redactar este informe el sistema **no se había desplegado
> todavía en máquinas virtuales de un proveedor IaaS**; todas las mediciones de este documento se hicieron en una
> sola máquina (procesos reales con TLS/mTLS, sin simulaciones). La arquitectura de despliegue, los scripts
> (`deploy/aws/`) y el validador automático (`dfsha-cloud validate`) están implementados y probados contra
> comandos simulados, pero no contra AWS.

El procedimiento completo está en `deploy/aws/README.md`. Al ejecutarlo, `deploy/aws/validate.sh` deja la
evidencia en `deploy/aws/evidence/` y al regenerar el informe esta sección se reemplaza automáticamente por los
resultados medidos: topología real, validación de 10 pasos (incluido el *failover* del líder y la caída de un
DataNode), y los benchmarks con nodos en máquinas distintas comparados con los de una sola máquina.
"""
    L: list[str] = ["# Despliegue en la nube (AWS Academy)", ""]
    a = L.append
    topo = cloud.get("topologia", [])
    a(f"El sistema se desplegó en máquinas virtuales EC2 de AWS Academy (Learner Lab) con los scripts de `deploy/aws/` y se validó el {cloud['fecha']} con `dfsha-cloud validate` desde un cliente externo, con tráfico real sobre Internet. Cada nodo corre en su propio contenedor Docker con la misma imagen.")
    a("")
    if topo:
        a("| Rol | Nodo | Zona de disponibilidad | Dirección pública |")
        a("|---|---|---|---|")
        for h in topo:
            a(f"| {h['role']} | {h['name']} | {h['az']} | `{h['public_host']}` |")
        a("")
        zones = sorted({h["az"] for h in topo if h["role"] == "data"})
        a(f"Los DataNodes abarcan {len(zones)} zonas de disponibilidad ({', '.join(zones)}); la colocación de réplicas usa cada AZ como *zona*, de modo que la pérdida de una AZ completa no destruye datos.")
        a("")
    a("## Validación en la nube")
    a("")
    a("| Paso | Resultado | Detalle | s |")
    a("|---|---|---|---|")
    for s in cloud["pasos"]:
        a(f"| {s['name']} | **{s['status']}** | {s['detail'].replace('|', '/')} | {s['seconds']} |")
    a("")
    a(f"**Veredicto: {'todos los pasos superados' if cloud['ok'] else 'hubo pasos fallidos (ver detalle)'}.**")
    a("")
    by = {s["name"].split(". ", 1)[1]: s for s in cloud["pasos"]}
    fo, dnf, pg = by.get("Failover del líder", {}), by.get("Caída de un DataNode", {}), by.get("put/get con verificación SHA-256", {})
    bullets = []
    if pg.get("data"):
        d = pg["data"]
        bullets.append(f"**Transferencia sobre Internet** ({_num(d['size_mib'])} MiB en {d['blocks']} bloques): subida {_num(d['upload_MiBps'])} MiB/s y bajada {_num(d['download_MiBps'])} MiB/s desde el cliente externo, con SHA-256 idéntico.")
    if fo.get("status") == "PASS":
        d = fo["data"]
        bullets.append(f"**Failover del líder en la nube**: se detuvo el contenedor del líder `{d['old_leader']}` y el servicio volvió a aceptar escrituras en **{_num(d['failover_s'])} s** con el nuevo líder `{d['new_leader']}`; los datos previos siguieron íntegros y el nodo se reincorporó como seguidor en {_num(d['rejoin_s'])} s.")
    if dnf.get("status") == "PASS":
        d = dnf["data"]
        bullets.append(f"**Caída de un DataNode**: tras detener `{d['victim']}`, la lectura inmediata fue correcta ({_num(d['read_s'])} s); la subreplicación se detectó a los {_num(d['detected_s'])} s y se restablecieron {d['target_replicas']} réplicas vivas por bloque a los **{_num(d['repaired_s'])} s** (con `dn_dead_after`=10 s y `repl_grace`=15 s, los valores por defecto de los contenedores).")
    for b in bullets:
        a(f"* {b}")
    a("")
    sc = bench.get("scaling")
    if sc:
        a("## Rendimiento con nodos en máquinas distintas")
        a("")
        env = sc["env"]
        a(f"Medido desde una VM cliente del mismo VPC ({env.get('datanodes', '?')} DataNodes en total, {env.get('fecha', '')}); se varía el número de DataNodes *activos* excluyendo al resto con `decommissioning` (los contenedores no se tocan).")
        a("")
        a("| Replicación | DataNodes activos | Subida MiB/s | Bajada MiB/s |")
        a("|---|---|---|---|")
        for key, lab in (("repl1", "×1"), ("repl3", "×3")):
            for k in sorted(sc["result"].get(key, {}), key=int):
                r = sc["result"][key][k]
                a(f"| {lab} | {k} | {_num(r['upload_MiBps']['mean'])} ± {_num(r['upload_MiBps']['sd'])} | {_num(r['download_MiBps']['mean'])} ± {_num(r['download_MiBps']['sd'])} |")
        a("")
        a("![Escalado en la nube frente a una sola máquina](report/figs/cloud_escalado.png)")
        a("")
        r1 = sc["result"].get("repl1", {})
        if len(r1) >= 2:
            ks = sorted(r1, key=int)
            lo, hi = r1[ks[0]]["upload_MiBps"]["mean"], r1[ks[-1]]["upload_MiBps"]["mean"]
            a(f"Con replicación ×1, pasar de {ks[0]} a {ks[-1]} DataNodes cambia la subida de {_num(lo)} a {_num(hi)} MiB/s ({_num(hi / lo)}×). A diferencia del experimento de una sola máquina, aquí cada DataNode tiene su propia CPU, red y disco, por lo que el escalado refleja el comportamiento distribuido real.")
            a("")
            dl = [v["download_MiBps"]["mean"] for key in ("repl1", "repl3") for v in sc["result"].get(key, {}).values()]
            if len(dl) >= 3 and max(dl) <= 1.25 * min(dl):
                a(f"La **bajada** apenas cambia con el número de DataNodes (entre {_num(min(dl))} y {_num(max(dl))} MiB/s): a partir de ahí el límite deja de estar en el cluster y pasa a la única VM cliente (CPU para TLS y AES-GCM, o su red). No se aisló cuál de las dos; para medirlo habría que lanzar varios clientes en paralelo. Con replicación ×3 la subida es menor porque cada bloque se escribe tres veces (tubería de replicación).")
                a("")
    for kind, title in (("throughput", "Efecto del paralelismo y del tamaño de archivo"), ("metadata", "Operaciones de metadatos en la nube")):
        b = bench.get(kind)
        if not b:
            continue
        a(f"## {title}")
        a("")
        if kind == "throughput":
            a("| Configuración | Subida MiB/s | Bajada MiB/s |")
            a("|---|---|---|")
            for k, r in b["result"].items():
                a(f"| {k} | {_num(r['upload_MiBps']['mean'])} | {_num(r['download_MiBps']['mean'])} |")
        else:
            a("| Clientes | stat linealizable ops/s (p99 ms) | mkdir Raft ops/s (p99 ms) |")
            a("|---|---|---|")
            for k, r in b["result"].items():
                a(f"| {k.split('=')[1]} | {_num(r['stat_linearizable']['ops_per_s'])} ({_num(r['stat_linearizable']['p99_ms'])}) | {_num(r['mkdir_raft_write']['ops_per_s'])} ({_num(r['mkdir_raft_write']['p99_ms'])}) |")
        a("")
    caps = sorted(p for p in (EVID / "capturas").glob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg")) if (EVID / "capturas").exists() else []
    if caps:
        a("## Evidencia gráfica")
        a("")
        for p in caps:
            cap = re.sub(r"^\d+[-_ ]*", "", p.stem).replace("-", " ").replace("_", " ")
            a(f"![{cap}]({p.relative_to(ROOT)})")
            a("")
    return "\n".join(L) + "\n"


def render_status(cloud: dict | None, docker: dict | None) -> str:
    def fmt(res, ok_text, none_text):
        if res is None:
            return f"**Pendiente.** {none_text}"
        return f"**Verificado el {res['fecha']}**: {ok_text}" if res.get("ok") else f"**Ejecutado el {res['fecha']} con fallos** (ver la evidencia)."

    rows = [
        ("Cluster local multiproceso (TLS/mTLS reales)", "**Verificado.** Suite automática de pruebas unitarias, de integración, de caos y de escala; benchmarks reproducibles.", "`tests/`, `bench/`"),
        ("Configuración del `docker-compose.yml`", "**Verificada sin contenedores**: `scripts/compose_dryrun.py` arranca los 8 nodos con exactamente las variables, URLs, volúmenes por nodo y puertos del compose, y comprueba el aislamiento de claves; `tests/test_compose.py` lo verifica estructuralmente.", "`scripts/compose_dryrun.py`"),
        ("Imagen Docker y contenedores", fmt(docker, "construcción, 8 contenedores sanos, aislamiento de claves dentro de cada contenedor, put/get verificado, caos (docker stop del líder, docker kill de un DataNode).", "No se ejecutó: Docker no estaba disponible en el entorno de desarrollo."), "`docs/evidence/docker/`"),
        ("Scripts de AWS (`deploy/aws/`)", "**Probados con comandos simulados** (`aws`, `ssh`, `rsync`, `curl` falsos): inventario y reparto por zonas, certificados con SAN reales, mínimo privilegio por rol, persistencia de la CA entre redespliegues.", "`tests/test_aws_scripts.py`"),
        ("Despliegue real en AWS Academy / GCP", fmt(cloud, "topología, validación de 10 pasos con caos y benchmarks en máquinas distintas (ver el capítulo «Despliegue en la nube»).", "No se ejecutó (requiere las credenciales del laboratorio)."), "`deploy/aws/evidence/`"),
    ]
    L = ["| Componente | Estado | Evidencia |", "|---|---|---|"]
    L += [f"| {c} | {s} | {e} |" for c, s, e in rows]
    return "\n".join(L) + "\n"


def main() -> None:
    cloud = _load(_latest(EVID, "resultados.json"))
    docker = _load(_latest(DOCKER_EVID, "resultado.json"))
    bench = {k: v for k in ("scaling", "throughput", "metadata") if (v := _load(BENCH_CLOUD / f"{k}.json"))}
    local = _load(BENCH_LOCAL / "scaling.json")
    (ROOT / "docs" / "DESPLIEGUE_NUBE.md").write_text(render_cloud(cloud, bench, local))
    (ROOT / "docs" / "ESTADO_VERIFICACION.md").write_text(render_status(cloud, docker))
    print(f"docs/DESPLIEGUE_NUBE.md ({'con evidencia real' if cloud else 'estado: pendiente'}) y docs/ESTADO_VERIFICACION.md")


if __name__ == "__main__":
    main()
