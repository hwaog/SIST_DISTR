#!/usr/bin/env python3
"""Verificación de punta a punta de la ejecución en Docker (la que NO se pudo hacer en el entorno de desarrollo).

    uv run python scripts/docker_verify.py            # construye, levanta, verifica, hace caos y deja evidencia
    uv run python scripts/docker_verify.py --keep     # no apaga el stack al terminar
    uv run python scripts/docker_verify.py --wipe     # al final borra también los volúmenes (datos)

Requiere Docker Desktop / OrbStack / Colima funcionando.  Escribe  docs/evidence/docker/<fecha>/  con
``resultado.json`` y ``log.txt``; ``report/cloud_section.py`` los usa para actualizar el estado de verificación del informe.

Qué comprueba (es el pendiente «Ejecutar Docker de punta a punta» y la verificación del aislamiento de claves):
  1. docker/compose disponibles          5. login + put/get de 48 MiB con verificación byte a byte + mapa de bloques
  2. build + up + los 8 contenedores sanos   6. AISLAMIENTO: qué ve cada contenedor en /pki y /secrets; ca.key en ninguno
  3. contenedores sin root, sin capacidades   7. cifrado en reposo dentro de un DataNode (cabecera DFSB)
  4. puertos internos (mTLS) no publicados  8. caos: docker stop del líder y docker kill de un DataNode
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTROLS = ["c1", "c2", "c3"]
DATAS = ["dn1", "dn2", "dn3", "dn4", "dn5"]


class Docker:
    """Envoltura mínima del CLI de docker (inyectable en las pruebas)."""

    def __init__(self, cwd: Path = ROOT) -> None:
        self.cwd = cwd

    def call(self, *args: str, timeout: float = 900, check: bool = False) -> tuple[int, str]:
        r = subprocess.run(["docker", *args], cwd=self.cwd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        if check and r.returncode != 0:
            raise RuntimeError(f"docker {' '.join(args)} falló ({r.returncode}): {out[-400:]}")
        return r.returncode, out

    def exec(self, container: str, *cmd: str, timeout: float = 120) -> tuple[int, str]:
        return self.call("exec", container, *cmd, timeout=timeout)


@dataclass
class Step:
    name: str
    status: str = "PENDIENTE"
    detail: str = ""
    seconds: float = 0.0
    data: dict = field(default_factory=dict)


class Verifier:
    def __init__(self, dk: Docker, *, admin_password: str, log=print, poll=2.0, healthy_timeout=240.0) -> None:
        self.dk, self.pw, self.log = dk, admin_password, log
        self.poll, self.healthy_timeout = poll, healthy_timeout
        self.steps: list[Step] = []

    # --- infraestructura
    def step(self, name: str, fn) -> Step:
        s = Step(name)
        t0 = time.perf_counter()
        self.log(f"\n▶ {name}")
        try:
            res = fn()
            s.status, s.detail, s.data = res if isinstance(res, tuple) else ("PASS", str(res or ""), {})
        except AssertionError as e:
            s.status, s.detail = "FAIL", str(e)
        except Exception as e:  # noqa: BLE001
            s.status, s.detail = "FAIL", f"{type(e).__name__}: {e}"
        s.seconds = round(time.perf_counter() - t0, 1)
        self.steps.append(s)
        self.log(f"  {'✓' if s.status == 'PASS' else '✗'} {s.status}: {s.detail} ({s.seconds}s)")
        return s

    def cli(self, *args: str, timeout: float = 300) -> tuple[int, str]:
        """Ejecuta `dfsha ...` dentro del contenedor cliente."""
        return self.dk.exec("dfsha-client", "dfsha", *args, timeout=timeout)

    # --- pasos
    def s_available(self):
        rc, out = self.dk.call("version", "--format", "{{.Server.Version}}")
        assert rc == 0, f"el daemon de Docker no responde: {out.strip()[:200]}"
        rc2, out2 = self.dk.call("compose", "version", "--short")
        assert rc2 == 0, "falta el plugin 'docker compose' (v2)"
        return "PASS", f"Docker {out.strip()} · compose {out2.strip()}", {}

    def s_up(self):
        self.dk.call("compose", "build", timeout=1800, check=True)
        self.dk.call("compose", "up", "-d", timeout=900, check=True)
        self.dk.call("compose", "--profile", "client", "up", "-d", "client", timeout=300, check=True)
        names = [f"dfsha-{n}" for n in CONTROLS + DATAS]
        deadline = time.time() + self.healthy_timeout
        status: dict[str, str] = {}
        while time.time() < deadline:
            status = {}
            for n in names:
                rc, out = self.dk.call("inspect", "--format", "{{.State.Health.Status}}", n)
                status[n] = out.strip() if rc == 0 else "ausente"
            if all(v == "healthy" for v in status.values()):
                return "PASS", f"{len(names)} contenedores healthy (build + up)", {"health": status}
            time.sleep(self.poll)
        raise AssertionError(f"no todos quedaron healthy en {self.healthy_timeout:.0f}s: { {k: v for k, v in status.items() if v != 'healthy'} }")

    def s_hardening(self):
        problems, info = [], {}
        for n in CONTROLS + DATAS:
            rc, uid = self.dk.exec(f"dfsha-{n}", "id", "-u")
            info[n] = uid.strip()
            if rc != 0 or uid.strip() != "10001":
                problems.append(f"{n} corre como uid {uid.strip()!r} (esperado 10001)")
            rc, caps = self.dk.call("inspect", "--format", "{{.HostConfig.CapDrop}}", f"dfsha-{n}")
            if "ALL" not in caps:
                problems.append(f"{n} conserva capacidades de Linux ({caps.strip()})")
        assert not problems, "; ".join(problems)
        return "PASS", "los 8 nodos corren como usuario sin privilegios (uid 10001) y con todas las capacidades de Linux retiradas", info

    def s_ports(self):
        rc, out = self.dk.call("ps", "--format", "{{.Names}} {{.Ports}}")
        assert rc == 0, out
        published = " ".join(out.split())
        leaked = [p for p in re.findall(r":(\d+)->", published) if int(p) >= 9000 or 7500 <= int(p) < 7600]
        assert not leaked, f"puertos INTERNOS (mTLS) publicados al anfitrión: {leaked}"
        return "PASS", "sólo se publican los puertos públicos (8441-8443 y 7441-7445); los internos (9441-9443, 7541-7545) no", {}

    def s_functional(self):
        rc, out = self.cli("login", "-u", "admin", "-p", self.pw)
        assert rc == 0, f"login falló: {out[-300:]}"
        rc, out = self.dk.exec("dfsha-client", "sh", "-c", "head -c 50331648 /dev/urandom > /tmp/p.bin && sha256sum /tmp/p.bin | cut -d' ' -f1")
        assert rc == 0, out
        sha_in = out.strip().split()[0]
        rc, out = self.cli("put", "/tmp/p.bin", "/home/admin/prueba.bin")
        assert rc == 0, f"put falló: {out[-300:]}"
        rc, out = self.cli("get", "/home/admin/prueba.bin", "/tmp/p.out")
        assert rc == 0, f"get falló: {out[-300:]}"
        rc, out = self.dk.exec("dfsha-client", "sh", "-c", "sha256sum /tmp/p.out | cut -d' ' -f1")
        assert out.strip().split()[0] == sha_in, "el archivo descargado NO coincide (SHA-256)"
        rc, blocks = self.cli("blocks", "/home/admin/prueba.bin")
        assert rc == 0 and blocks.count("dn") >= 12, f"mapa de bloques inesperado: {blocks[-300:]}"
        rc, nodes = self.cli("nodes")
        assert rc == 0 and nodes.count("leader") >= 1, nodes[-300:]
        return "PASS", f"login, put/get de 48 MiB con SHA-256 idéntico ({sha_in[:12]}…), mapa de bloques y estado del cluster", {"sha256": sha_in}

    def s_key_isolation(self):
        problems, seen = [], {}
        for n in CONTROLS + DATAS:
            rc, out = self.dk.exec(f"dfsha-{n}", "ls", "/pki")
            files = sorted(out.split())
            seen[n] = files
            if files != sorted(["ca.pem", f"{n}.crt", f"{n}.key"]):
                problems.append(f"{n} ve en /pki: {files}")
            rc, out = self.dk.exec(f"dfsha-{n}", "ls", "/secrets")
            sec = sorted(out.split()) if rc == 0 else []  # la imagen crea /secrets VACÍO en todos: lo que importa es su contenido
            if n in DATAS and sec:
                problems.append(f"{n} (DataNode) tiene secretos montados en /secrets: {sec}")
            if n in CONTROLS and sec != ["admin.pass", "cluster.key"]:
                problems.append(f"{n} ve en /secrets: {sec}")
            rc, out = self.dk.exec(f"dfsha-{n}", "sh", "-c", "find / -name 'ca.key' -not -path '/proc/*' 2>/dev/null | head -3")
            if out.strip():
                problems.append(f"{n} contiene ca.key: {out.strip()}")
        rc, out = self.dk.exec("dfsha-client", "ls", "/pki")
        if sorted(out.split()) != ["ca.pem"]:
            problems.append(f"el cliente ve en /pki: {out.split()}")
        assert not problems, "; ".join(problems)
        return "PASS", f"cada nodo ve SÓLO su par de claves (p. ej. dn1: {seen['dn1']}); ca.key y cluster.key no están donde no deben", seen

    def s_encryption(self):
        rc, out = self.dk.exec("dfsha-dn1", "sh", "-c", "f=$(find /data/blocks -name '*.blk' | head -1); test -n \"$f\" && od -An -tx1 -N4 \"$f\"")
        assert rc == 0, "dn1 no tiene bloques (¿falló el put?)"
        assert out.split() == ["44", "46", "53", "42"], f"cabecera inesperada: {out.strip()} (esperado DFSB = 44 46 53 42)"
        return "PASS", "los bloques en el disco de dn1 empiezan por «DFSB» (AES-256-GCM): cifrados en reposo", {}

    def leader(self) -> str | None:
        rc, out = self.cli("nodes")
        m = re.search(r"│ (c\d+) +│ leader", out)
        return m.group(1) if m else None

    def s_chaos(self):
        lead = self.leader()
        assert lead, "no se identificó el líder"
        t0 = time.perf_counter()
        self.dk.call("stop", f"dfsha-{lead}", check=True)
        ok_at = None
        for _ in range(300):
            rc, out = self.cli("mkdir", "/home/admin/tras_failover")
            if rc == 0 or "ya existe" in out:
                ok_at = time.perf_counter()
                break
            time.sleep(0.5)
        assert ok_at, "el servicio no se recuperó tras detener al líder"
        failover = ok_at - t0
        rc, out = self.cli("get", "/home/admin/prueba.bin", "/tmp/p2.out")
        assert rc == 0, f"get tras failover falló: {out[-200:]}"
        new = self.leader()
        assert new and new != lead, f"líder nuevo inválido ({new})"
        self.dk.call("start", f"dfsha-{lead}", check=True)
        # caída de un DataNode (kill = caída abrupta)
        self.dk.call("kill", "dfsha-dn2", check=True)
        rc, out = self.cli("get", "/home/admin/prueba.bin", "/tmp/p3.out")
        assert rc == 0, f"get con dn2 caído falló: {out[-200:]}"
        self.dk.call("start", "dfsha-dn2", check=True)
        return "PASS", f"docker stop del líder {lead} → servicio de vuelta en {failover:.1f}s (nuevo líder {new}); docker kill dn2 → lectura inmediata correcta", {"failover_s": round(failover, 1), "old_leader": lead, "new_leader": new}

    def run(self) -> dict:
        for name, fn in (
            ("1. Docker y compose disponibles", self.s_available), ("2. build + up: 8 contenedores healthy", self.s_up),
            ("3. Contenedores sin root ni capacidades", self.s_hardening), ("4. Sólo puertos públicos publicados", self.s_ports),
            ("5. Login, put/get verificado, mapa de bloques", self.s_functional), ("6. Aislamiento de claves por contenedor", self.s_key_isolation),
            ("7. Cifrado en reposo en el disco del DataNode", self.s_encryption), ("8. Caos: líder y DataNode", self.s_chaos),
        ):  # fmt: skip
            s = self.step(name, fn)
            if s.status == "FAIL" and name[0] in "12":
                break  # sin stack no tiene sentido seguir
        return {"fecha": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": all(s.status == "PASS" for s in self.steps) and len(self.steps) == 8, "pasos": [asdict(s) for s in self.steps]}


def ensure_env() -> str:
    env = ROOT / ".env"
    if env.exists():
        m = re.search(r"^ADMIN_PASSWORD=(.+)$", env.read_text(), re.M)
        if m:
            return m.group(1).strip()
    pw = secrets.token_urlsafe(18)
    base = (ROOT / ".env.example").read_text()
    env.write_text(re.sub(r"^ADMIN_PASSWORD=.*$", f"ADMIN_PASSWORD={pw}", base, flags=re.M))
    env.chmod(0o600)
    return pw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="no apagar el stack al terminar")
    ap.add_argument("--wipe", action="store_true", help="al terminar, borrar también los volúmenes")
    args = ap.parse_args()
    out_dir = ROOT / "docs" / "evidence" / "docker" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_f = open(out_dir / "log.txt", "w")  # noqa: SIM115

    def log(m: str) -> None:
        print(m, flush=True)
        log_f.write(m + "\n")
        log_f.flush()

    pw = ensure_env()
    dk = Docker()
    res = Verifier(dk, admin_password=pw, log=log).run()
    (out_dir / "resultado.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    latest = out_dir.parent / "latest"
    try:
        if latest.is_symlink():
            latest.unlink()
        latest.symlink_to(out_dir.name)
    except OSError:
        pass
    if not args.keep:
        dk.call("compose", "--profile", "client", "down", *(["-v"] if args.wipe else []), timeout=300)
    log(f"\nEvidencia en {out_dir}  →  {'TODO OK' if res['ok'] else 'HAY FALLOS (revise log.txt y `docker compose logs`)'}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
