"""Validación y medición de un despliegue REAL de DFSha (AWS Academy, GCP o cualquier IaaS).

    dfsha-cloud validate --inventory deploy/aws/inventory.txt --ssh-key ~/labsuser.pem
    dfsha-cloud bench scaling --inventory deploy/aws/inventory.txt          # desde una VM del mismo VPC
    dfsha-cloud bench throughput | metadata

``validate`` ejecuta, contra el cluster desplegado, la misma batería que se demuestra en el video y deja la
EVIDENCIA para el informe en ``deploy/aws/evidence/<fecha>/`` (``resultados.json``, ``resumen.md``, ``log.txt``):

  1. conectividad y exactamente un líder Raft     6. cifrado en reposo (inspección del disco de un DataNode)
  2. login y topología (zonas / AZ)               7. FAILOVER del líder (se detiene su contenedor)
  3. controles de seguridad de red                8. caída de un DataNode: lectura continua + re-replicación
  4. put/get de un archivo con verificación       9. fsck final
  5. mapa de bloques: ≥2 réplicas y ≥2 zonas

Los pasos 6-8 necesitan poder detener/arrancar contenedores en los nodos (``--ssh-key``).  Para poder PROBAR
esta misma lógica sin la nube, ``--local-cluster DIR`` usa un cluster de ``dfsha-cluster`` como si fuera remoto.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import socket
import ssl
import statistics as st
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import click
import httpx

from .client.sdk import Client
from .common import errors as E

MiB = 1024 * 1024


# --------------------------------------------------------------------------- inventario
@dataclass
class Host:
    role: str  # control | data | client
    name: str
    private_ip: str
    public_host: str
    az: str
    api_port: int = 0
    internal_port: int = 0


def read_inventory(path: str | Path) -> list[Host]:
    """Lee ``rol nombre ip_privada dns_publico zona`` (las líneas con # se ignoran) y asigna puertos por convención."""
    hosts: list[Host] = []
    ci = di = 0
    for ln in Path(path).read_text().splitlines():
        parts = ln.split()
        if not parts or parts[0].startswith("#"):
            continue
        role, name, priv, pub, az = (parts + [""] * 5)[:5]
        h = Host(role, name, priv, pub, az)
        if role == "control":
            ci += 1
            h.api_port, h.internal_port = 8440 + ci, 9440 + ci
        elif role == "data":
            di += 1
            h.api_port, h.internal_port = 7440 + di, 7540 + di
        hosts.append(h)
    if not any(h.role == "control" for h in hosts):
        raise click.ClickException(f"{path}: el inventario no tiene ControlNodes")
    return hosts


# --------------------------------------------------------------------- "mata-nodos" (chaos)
class Killer:
    """Detener/arrancar un nodo e inspeccionar su disco.  Implementaciones: SSH (nube) y local (pruebas)."""

    def stop(self, name: str, *, graceful: bool = False) -> None:
        raise NotImplementedError

    def start(self, name: str) -> None:
        raise NotImplementedError

    def inspect_blocks(self, dn: str, marker: bytes) -> dict[str, Any]:
        """{'blobs': n, 'magic_ok': bool, 'plaintext_hits': n} sobre el almacenamiento del DataNode."""
        raise NotImplementedError


class SSHKiller(Killer):
    def __init__(self, hosts: list[Host], key: str, user: str = "ec2-user", container: str = "dfsha-node", data_dir: str = "/opt/dfsha/data") -> None:
        self.by_name = {h.name: h for h in hosts}
        self.key, self.user, self.container, self.data_dir = key, user, container, data_dir

    def _ssh(self, name: str, remote: str, timeout: float = 90) -> subprocess.CompletedProcess:
        h = self.by_name[name]
        cmd = ["ssh", "-i", self.key, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", f"{self.user}@{h.public_host}", remote]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def stop(self, name: str, *, graceful: bool = False) -> None:
        r = self._ssh(name, f"sudo docker {'stop' if graceful else 'kill'} {self.container}")
        if r.returncode != 0:
            raise RuntimeError(f"no se pudo detener {name}: {r.stderr.strip()[:200]}")

    def start(self, name: str) -> None:
        r = self._ssh(name, f"sudo docker start {self.container}")
        if r.returncode != 0:
            raise RuntimeError(f"no se pudo arrancar {name}: {r.stderr.strip()[:200]}")

    def inspect_blocks(self, dn: str, marker: bytes) -> dict[str, Any]:
        script = (
            f"set -e; d={shlex.quote(self.data_dir)}/blocks; f=$(sudo find $d -name '*.blk' | head -1); "
            "echo \"FILE:$f\"; echo \"MAGIC:$(sudo head -c 4 $f | xxd -p)\"; "
            f"echo \"BLOBS:$(sudo find $d -name '*.blk' | wc -l)\"; "
            f"echo \"HITS:$(sudo grep -rlF -- {shlex.quote(marker.decode())} $d 2>/dev/null | wc -l)\""
        )
        r = self._ssh(dn, script)
        kv = dict(ln.split(":", 1) for ln in r.stdout.splitlines() if ":" in ln)
        return {"blobs": int(kv.get("BLOBS", 0)), "magic_ok": kv.get("MAGIC", "").strip() == "44465342", "plaintext_hits": int(kv.get("HITS", -1)), "ssh_ok": r.returncode == 0}


class LocalKiller(Killer):
    """Para PROBAR el validador sin nube: un LocalCluster actúa como cluster remoto."""

    def __init__(self, cluster) -> None:
        self.c = cluster

    def stop(self, name: str, *, graceful: bool = False) -> None:
        self.c.kill(name)

    def start(self, name: str) -> None:
        self.c.start(name)

    def inspect_blocks(self, dn: str, marker: bytes) -> dict[str, Any]:
        files = list((self.c.base / "data" / dn / "blocks").glob("*/*.blk"))
        hits = sum(1 for f in files if marker in f.read_bytes())
        magic = bool(files) and files[0].read_bytes()[:4] == b"DFSB"
        return {"blobs": len(files), "magic_ok": magic, "plaintext_hits": hits, "ssh_ok": True}


# ------------------------------------------------------------------------------ validador
@dataclass
class Step:
    name: str
    status: str = "PENDIENTE"  # PASS | FAIL | WARN | SKIP
    detail: str = ""
    seconds: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)


class Validator:
    def __init__(self, client: Client, endpoints: list[str], ca: str, *, hosts: list[Host] | None = None, killer: Killer | None = None,
                 size_mib: int = 64, out: Path | None = None, wait_repair_s: float = 240.0, log=print) -> None:  # fmt: skip
        self.c, self.endpoints, self.ca = client, endpoints, ca
        self.hosts, self.killer = hosts or [], killer
        self.size_mib, self.out, self.wait_repair_s = size_mib, out, wait_repair_s
        self.steps: list[Step] = []
        self._log = log
        self.test_path = f"/home/admin/_validacion_{int(time.time())}.bin"
        self.marker = b"DFSHA-MARCADOR-EN-CLARO-" + os.urandom(4).hex().encode()
        self.payload = b""
        self.payload_sha = ""
        self.blockmap: list[dict] = []

    # --- infraestructura de pasos
    def log(self, msg: str) -> None:
        self._log(msg)

    def run_step(self, name: str, fn) -> Step:
        s = Step(name)
        t0 = time.perf_counter()
        self.log(f"\n▶ {name}")
        try:
            res = fn()
            s.status, s.detail, s.data = res if isinstance(res, tuple) else ("PASS", str(res or ""), {})
        except AssertionError as e:
            s.status, s.detail = "FAIL", f"{e}"
        except Exception as e:  # noqa: BLE001
            s.status, s.detail = "FAIL", f"{type(e).__name__}: {e}"
        s.seconds = round(time.perf_counter() - t0, 2)
        self.steps.append(s)
        icon = {"PASS": "✓", "FAIL": "✗", "WARN": "!", "SKIP": "-"}.get(s.status, "?")
        self.log(f"  {icon} {s.status}: {s.detail}  ({s.seconds}s)")
        return s

    # --- utilidades
    def node_infos(self) -> dict[str, dict]:
        out = {}
        with httpx.Client(verify=_ctx(self.ca), timeout=4.0) as h:
            for ep in self.endpoints:
                try:
                    j = h.get(f"{ep}/v1/node").json()
                    out[j["id"]] = {**j, "endpoint": ep}
                except Exception:  # noqa: BLE001
                    pass
        return out

    def leader_name(self) -> str | None:
        ls = [i for i, v in self.node_infos().items() if v["role"] == "leader"]
        return ls[0] if len(ls) == 1 else None

    def block_map(self) -> list[dict]:
        meta = self.c._locations_all(self.c.abspath(self.test_path))
        zones = {d["id"]: d["zone"] for d in self.c.datanodes()}
        return [{"block": b["block_id"][:10], "replicas": [{"dn": loc["id"], "zone": zones.get(loc["id"], "?")} for loc in b["locs"]]} for b in meta["blocks"]]

    # --- pasos
    def s_connectivity(self):
        infos = self.node_infos()
        leaders = [i for i, v in infos.items() if v["role"] == "leader"]
        assert len(infos) == len(self.endpoints), f"sólo respondieron {sorted(infos)} de {len(self.endpoints)} ControlNodes"
        assert len(leaders) == 1, f"líderes: {leaders}"
        return "PASS", f"{len(infos)} ControlNodes responden por HTTPS verificado contra la CA; líder = {leaders[0]} (término {infos[leaders[0]]['term']})", {"leader": leaders[0], "nodes": {k: v['role'] for k, v in infos.items()}}

    def s_login_topology(self):
        who = self.c.whoami()
        dns = self.c.datanodes()
        alive = [d for d in dns if d["alive"]]
        zones = sorted({d["zone"] for d in alive})
        assert who["admin"], "el usuario no es admin"
        assert len(alive) >= 3, f"sólo {len(alive)} DataNodes vivos (se necesitan ≥3 para replicación ×3)"
        status = "PASS" if len(zones) >= 2 else "WARN"
        return status, f"{len(alive)} DataNodes vivos en {len(zones)} zonas {zones}" + ("" if len(zones) >= 2 else " — use ≥2 zonas para tolerar la pérdida de una AZ"), {"datanodes": [{"id": d["id"], "zone": d["zone"]} for d in alive]}

    def s_network_security(self):
        ep = self.endpoints[0]
        host, port = re.match(r"https://([^:/]+):(\d+)", ep).groups()  # type: ignore[union-attr]
        findings, status = {}, "PASS"
        # 1) HTTP en claro no se atiende
        try:
            r = httpx.get(f"http://{host}:{port}/health", timeout=5)
            findings["http_plano"] = f"ATENDIDO (HTTP {r.status_code})"
            status = "FAIL"
        except Exception:  # noqa: BLE001
            findings["http_plano"] = "rechazado"
        # 2) el certificado (CA privada) NO es de confianza pública: un cliente sin la CA debe rechazarlo
        try:
            httpx.get(f"https://{host}:{port}/health", timeout=5)
            findings["tls_sin_ca"] = "ACEPTADO sin la CA (inesperado)"
            status = "FAIL"
        except Exception as e:  # noqa: BLE001
            findings["tls_sin_ca"] = "rechazado (CERTIFICATE_VERIFY_FAILED)" if "CERTIFICATE_VERIFY_FAILED" in repr(e) else f"rechazado ({type(e).__name__})"
        # 3) la API exige autenticación
        with httpx.Client(verify=_ctx(self.ca), timeout=5) as h:
            code = h.get(f"{ep}/v1/fs/ls", params={"path": "/"}).status_code
            findings["api_sin_token"] = f"HTTP {code}"
            if code not in (401, 503):
                status = "FAIL"
            code = h.get(f"{self.c.datanodes()[0]['url']}/v1/blocks/{'0' * 32}").status_code
            findings["bloque_sin_token"] = f"HTTP {code}"
            if code not in (401, 403):
                status = "FAIL"
        # 4) el puerto INTERNO (mTLS) no debe ser alcanzable desde Internet (security group)
        internal = next((h.internal_port for h in self.hosts if h.role == "control"), 0)
        if internal:
            try:
                with socket.create_connection((host, internal), timeout=4):
                    pass
                # alcanzable (p. ej. se ejecuta dentro del VPC): debe exigir certificado de cliente
                try:
                    with socket.create_connection((host, internal), timeout=4) as raw, _ctx(self.ca).wrap_socket(raw, server_hostname=host) as tls:
                        tls.do_handshake()
                        tls.sendall(b"GET /metrics HTTP/1.0\r\n\r\n")
                        resp = tls.recv(200)
                    findings["puerto_interno"] = "ALCANZABLE y SIN mTLS" if resp.startswith(b"HTTP/1.1 200") else "alcanzable; no entrega datos sin certificado de cliente"
                    status = "FAIL" if resp.startswith(b"HTTP/1.1 200") else ("PASS" if status == "PASS" else status)
                except (ssl.SSLError, OSError):
                    findings["puerto_interno"] = "alcanzable desde este origen pero el handshake mTLS sin certificado de cliente se rechaza"
                if status == "PASS":
                    status = "WARN"
                    findings["nota"] = "Ejecute este paso desde Internet (su portátil) para comprobar que el security group oculta el puerto interno"
            except OSError:
                findings["puerto_interno"] = "no alcanzable desde Internet (security group correcto)"
        return status, "; ".join(f"{k}: {v}" for k, v in findings.items()), findings

    def s_put_get(self):
        self.payload = self.marker * (self.size_mib * MiB // len(self.marker)) + os.urandom(self.size_mib * MiB % len(self.marker) or 1)
        self.payload_sha = hashlib.sha256(self.payload).hexdigest()
        t = time.perf_counter()
        r = self.c.send(self.payload, self.test_path)
        t_up = time.perf_counter() - t
        t = time.perf_counter()
        back = self.c.receive(self.test_path)
        t_down = time.perf_counter() - t
        assert hashlib.sha256(back).hexdigest() == self.payload_sha, "el contenido descargado NO coincide (SHA-256)"
        mb = len(self.payload) / MiB
        return "PASS", f"{mb:.0f} MiB en {r['blocks']} bloques; subida {mb / t_up:.0f} MiB/s, bajada {mb / t_down:.0f} MiB/s; SHA-256 idéntico", {"size_mib": mb, "blocks": r["blocks"], "upload_MiBps": round(mb / t_up, 1), "download_MiBps": round(mb / t_down, 1), "sha256": self.payload_sha}

    def s_block_map(self):
        repl = self.c.stat(self.test_path)["repl"]
        deadline = time.time() + 60
        while time.time() < deadline:  # la 3ª réplica la completa el líder en segundo plano
            self.blockmap = self.block_map()
            if all(len(b["replicas"]) >= repl for b in self.blockmap):
                break
            time.sleep(2)
        bad = [b for b in self.blockmap if len(b["replicas"]) < repl]
        one_zone = [b for b in self.blockmap if len({r["zone"] for r in b["replicas"]}) < 2]
        used = sorted({r["dn"] for b in self.blockmap for r in b["replicas"]})
        assert not bad, f"{len(bad)} bloques con menos de {repl} réplicas"
        status = "PASS" if not one_zone else "WARN"
        return status, f"{len(self.blockmap)} bloques × {repl} réplicas, repartidos en {len(used)} DataNodes {used}" + ("" if not one_zone else f"; {len(one_zone)} bloques en una sola zona"), {"blocks": self.blockmap[:12], "datanodes_used": used}

    def s_encryption_at_rest(self):
        if not self.killer:
            return "SKIP", "requiere acceso SSH a los DataNodes (--ssh-key)", {}
        dn = self.blockmap[0]["replicas"][0]["dn"] if self.blockmap else self.c.datanodes()[0]["id"]
        r = self.killer.inspect_blocks(dn, self.marker)
        assert r.get("ssh_ok", True), "no se pudo inspeccionar el disco"
        assert r["blobs"] > 0, f"{dn} no tiene bloques"
        assert r["magic_ok"], "los bloques en disco no tienen el formato cifrado esperado (DFSB)"
        assert r["plaintext_hits"] == 0, f"¡se encontró texto en claro en el disco de {dn}!"
        return "PASS", f"{dn}: {r['blobs']} bloques en disco, cabecera DFSB (AES-GCM) y 0 coincidencias del marcador en claro que se subió", {"datanode": dn, **r}

    def s_failover_leader(self):
        if not self.killer:
            return "SKIP", "requiere detener contenedores por SSH (--ssh-key)", {}
        leader = self.leader_name()
        assert leader, "no hay líder único"
        t0 = time.perf_counter()
        self.killer.stop(leader)
        ok_at = None
        for i in range(600):
            try:
                self.c.mkdir(f"/home/admin/_failover_{int(t0)}_{i % 3}")
                ok_at = time.perf_counter()
                break
            except (E.Unavailable, E.NotLeader):
                time.sleep(0.1)
        assert ok_at, "el servicio no volvió a aceptar escrituras tras la caída del líder"
        failover = ok_at - t0
        new_leader = self.leader_name()
        assert new_leader and new_leader != leader, f"líder nuevo inválido: {new_leader}"
        back = self.c.receive(self.test_path)
        assert hashlib.sha256(back).hexdigest() == self.payload_sha, "datos corruptos tras el failover"
        put_ok = self.c.send(b"escritura tras failover", f"/home/admin/_tras_failover_{int(t0)}")
        self.killer.start(leader)
        rejoin = None
        t1 = time.perf_counter()
        while time.perf_counter() - t1 < 90:
            info = self.node_infos().get(leader)
            if info and info["role"] == "follower":
                rejoin = time.perf_counter() - t1
                break
            time.sleep(1)
        assert rejoin is not None, f"{leader} no volvió como seguidor en 90 s"
        return "PASS", f"líder {leader} detenido → escritura aceptada otra vez en {failover:.1f}s por el nuevo líder {new_leader}; datos íntegros; {leader} se reincorporó como seguidor en {rejoin:.0f}s", {"old_leader": leader, "new_leader": new_leader, "failover_s": round(failover, 2), "rejoin_s": round(rejoin, 1), "write_after_failover": bool(put_ok)}

    def s_datanode_failure(self):
        if not self.killer:
            return "SKIP", "requiere detener contenedores por SSH (--ssh-key)", {}
        repl = self.c.stat(self.test_path)["repl"]
        victim = self.blockmap[0]["replicas"][0]["dn"] if self.blockmap else self.c.datanodes()[0]["id"]
        alive_after = sum(1 for d in self.c.datanodes() if d["alive"]) - 1
        target = min(repl, alive_after)
        self.killer.stop(victim)
        t0 = time.perf_counter()
        back = self.c.receive(self.test_path)  # lectura INMEDIATA, con el nodo caído
        read_s = time.perf_counter() - t0
        assert hashlib.sha256(back).hexdigest() == self.payload_sha, "lectura incorrecta con un DataNode caído"
        detected = repaired = None
        while time.perf_counter() - t0 < self.wait_repair_s:
            f = self.c.fsck()
            alive = {d["id"] for d in self.c.datanodes() if d["alive"]}
            # «detectado» = el cluster ya sabe que el nodo cayó (lo declara muerto o ve subreplicación); si la reparación es
            # tan rápida que no llega a observarse subreplicación, igualmente cuenta como detectada al declararse muerto.
            if detected is None and (f["under_replicated"] > 0 or victim not in alive):
                detected = time.perf_counter() - t0
            m = self.block_map()
            if detected is not None and f["under_replicated"] == 0 and all(len([r for r in b["replicas"] if r["dn"] in alive]) >= target for b in m):
                repaired = time.perf_counter() - t0
                break
            time.sleep(1)
        self.killer.start(victim)
        for _ in range(60):
            if any(d["id"] == victim and d["alive"] for d in self.c.datanodes()):
                break
            time.sleep(2)
        assert repaired is not None, f"no se restableció el factor de replicación en {self.wait_repair_s:.0f}s"
        return "PASS", f"{victim} detenido → lectura inmediata correcta ({read_s:.1f}s); subreplicación detectada a {detected:.0f}s; {target} réplicas vivas por bloque restablecidas a {repaired:.0f}s", {"victim": victim, "read_s": round(read_s, 2), "detected_s": round(detected or 0, 1), "repaired_s": round(repaired, 1), "target_replicas": target}

    def s_fsck(self):
        for _ in range(60):
            f = self.c.fsck()
            if f["under_replicated"] == 0 and f["lost"] == 0 and f["over_replicated"] == 0:
                return "PASS", f"{f['blocks']} bloques, todos sanos", f
            time.sleep(2)
        return "FAIL", f"fsck no converge: {f}", f

    def s_cleanup(self):
        for p in [x["name"] for x in self.c.ls("/home/admin") if x["name"].startswith(("_validacion_", "_failover_", "_tras_failover_"))]:
            self.c.rm(f"/home/admin/{p}", recursive=True)
        return "PASS", "archivos de prueba eliminados", {}

    def run(self) -> dict[str, Any]:
        order = [
            ("1. Conectividad y líder único", self.s_connectivity), ("2. Login y topología", self.s_login_topology),
            ("3. Controles de seguridad de red", self.s_network_security), ("4. put/get con verificación SHA-256", self.s_put_get),
            ("5. Mapa de bloques (réplicas y zonas)", self.s_block_map), ("6. Cifrado en reposo en el disco", self.s_encryption_at_rest),
            ("7. Failover del líder", self.s_failover_leader), ("8. Caída de un DataNode", self.s_datanode_failure),
            ("9. fsck final", self.s_fsck), ("10. Limpieza", self.s_cleanup),
        ]  # fmt: skip
        for name, fn in order:
            self.run_step(name, fn)
        result = {
            "fecha": time.strftime("%Y-%m-%d %H:%M:%S"), "endpoints": self.endpoints, "size_mib": self.size_mib,
            "topologia": [asdict(h) for h in self.hosts], "ok": all(s.status in ("PASS", "WARN", "SKIP") for s in self.steps),
            "pasos": [asdict(s) for s in self.steps],
        }  # fmt: skip
        if self.out:
            self.out.mkdir(parents=True, exist_ok=True)
            (self.out / "resultados.json").write_text(json.dumps(result, indent=1, ensure_ascii=False, default=str))
            (self.out / "resumen.md").write_text(self.summary_md(result))
        return result

    @staticmethod
    def summary_md(res: dict) -> str:
        lines = [f"# Evidencia de validación en la nube — {res['fecha']}", "", "| Paso | Resultado | Detalle | s |", "|---|---|---|---|"]
        for s in res["pasos"]:
            lines.append(f"| {s['name']} | **{s['status']}** | {s['detail'].replace('|', '/')} | {s['seconds']} |")
        lines.append("")
        lines.append(f"**Veredicto:** {'TODOS LOS PASOS SUPERADOS' if res['ok'] else 'HAY PASOS FALLIDOS'}")
        return "\n".join(lines) + "\n"


def _ctx(ca: str) -> ssl.SSLContext:
    from .pki import client_context

    return client_context(ca)


# ------------------------------------------------------------------------------ benchmark
def _stats(xs: list[float]) -> dict:
    return {"mean": st.mean(xs), "sd": st.pstdev(xs) if len(xs) > 1 else 0.0, "min": min(xs), "max": max(xs), "n": len(xs), "all": xs}


def throughput(cl: Client, size_mib: int, reps: int, repl: int | None = None, tag: str = "t") -> dict:
    data = os.urandom(size_mib * MiB)
    ups, downs = [], []
    for i in range(reps):
        p = f"/home/admin/_bench_{tag}_{i}"
        t = time.perf_counter()
        cl.send(data, p, repl=repl)
        ups.append(size_mib / (time.perf_counter() - t))
        t = time.perf_counter()
        out = cl.receive(p)
        downs.append(size_mib / (time.perf_counter() - t))
        assert out == data
        cl.rm(p)
    return {"upload_MiBps": _stats(ups), "download_MiBps": _stats(downs)}


def bench_throughput(cl: Client, sizes: list[int], workers_list: list[int], reps: int) -> dict:
    res: dict[str, Any] = {}
    for w in workers_list:
        cl.workers = w
        for size in sizes:
            res[f"workers={w},size={size}MiB"] = throughput(cl, size, reps, tag=f"w{w}s{size}")
            r = res[f"workers={w},size={size}MiB"]
            print(f"  workers={w:2d} {size:4d} MiB: sube {r['upload_MiBps']['mean']:.0f} / baja {r['download_MiBps']['mean']:.0f} MiB/s", flush=True)
    return res


def _hammer(fn, threads: int, seconds: float) -> dict:
    lat: list[float] = []
    stop = time.monotonic() + seconds
    lock = threading.Lock()

    def run(k: int) -> None:
        mine, i = [], 0
        while time.monotonic() < stop:
            t = time.perf_counter()
            try:
                fn(k, i)
                mine.append((time.perf_counter() - t) * 1000)
            except E.DfshaError:
                pass
            i += 1
        with lock:
            lat.extend(mine)

    ts = [threading.Thread(target=run, args=(k,)) for k in range(threads)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    lat.sort()
    return {
        "ops_per_s": len(lat) / (time.perf_counter() - t0),
        "p50_ms": lat[len(lat) // 2] if lat else 0.0,
        "p99_ms": lat[min(len(lat) - 1, int(0.99 * len(lat)))] if lat else 0.0,
        "ops": len(lat),
    }


def _measure_metadata(cl_factory, th: int, seconds: float) -> dict:
    clients = [cl_factory() for _ in range(th)]
    clients[0].mkdir("/home/admin/_bench_meta", parents=True)
    res = {
        "stat_linearizable": _hammer(lambda k, i: clients[k].stat("/home/admin/_bench_meta"), th, seconds),
        "mkdir_raft_write": _hammer(lambda k, i: clients[k].mkdir(f"/home/admin/_bench_meta/t{th}_{k}_{i}"), th, seconds),
    }
    clients[0].rm("/home/admin/_bench_meta", recursive=True)
    return res


def bench_metadata(cl_factory, threads_list: list[int], seconds: float) -> dict:
    res: dict[str, Any] = {}
    for th in threads_list:
        r = res[f"threads={th}"] = _measure_metadata(cl_factory, th, seconds)
        print(f"  clientes={th:3d}: stat {r['stat_linearizable']['ops_per_s']:.0f}/s (p99 {r['stat_linearizable']['p99_ms']:.1f} ms), mkdir {r['mkdir_raft_write']['ops_per_s']:.0f}/s (p99 {r['mkdir_raft_write']['p99_ms']:.1f} ms)", flush=True)
    return res


def bench_scaling(cl: Client, counts: list[int], size_mib: int, reps: int, repl_list: list[int]) -> dict:
    """Throughput vs nº de DataNodes ACTIVOS: se excluye al resto con `decommissioning` (no se tocan los contenedores)."""
    dns = sorted(d["id"] for d in cl.datanodes() if d["alive"])
    res: dict[str, Any] = {f"repl{r}": {} for r in repl_list}
    try:
        for repl in repl_list:
            for k in counts:
                if k > len(dns) or k < repl:
                    continue
                for j, d in enumerate(dns):
                    cl.set_datanode_state(d, "active" if j < k else "decommissioning")
                time.sleep(2)
                r = throughput(cl, size_mib, reps, repl=repl, tag=f"s{repl}_{k}")
                res[f"repl{repl}"][str(k)] = r
                print(f"  repl={repl} DataNodes activos={k}: sube {r['upload_MiBps']['mean']:.0f} / baja {r['download_MiBps']['mean']:.0f} MiB/s", flush=True)
    finally:
        for d in dns:
            cl.set_datanode_state(d, "active")
    return res


# ------------------------------------------------------------------------------------- CLI
def _env(local: str | None, inventory: str | None, endpoints: str | None, ca: str | None, user: str, password: str | None):
    """-> (endpoints, ca, hosts, killer_factory_args, password)"""
    hosts = read_inventory(inventory) if inventory else []
    if local:
        from .cluster import LocalCluster

        lc = LocalCluster.load(local)
        return lc.endpoints, lc.ca, hosts, lc, lc.admin_password
    eps = [e for e in (endpoints or os.environ.get("DFSHA_ENDPOINTS", "")).split(",") if e]
    if not eps and hosts:
        eps = [f"https://{h.public_host}:{h.api_port}" for h in hosts if h.role == "control"]
    ca = ca or os.environ.get("DFSHA_CA")
    password = password or os.environ.get("DFSHA_PASSWORD")
    if not eps or not ca or not password:
        raise click.ClickException("faltan datos: use --inventory (o DFSHA_ENDPOINTS), --ca (o DFSHA_CA) y la clave en DFSHA_PASSWORD")
    return eps, ca, hosts, None, password


@click.group(help="Validación y benchmarks de un despliegue real de DFSha.")
def main() -> None:  # pragma: no cover - CLI fina
    pass


@main.command("validate", help="Batería de validación + evidencia para el informe.")
@click.option("--inventory", type=click.Path(exists=True), help="deploy/aws/inventory.txt")
@click.option("--endpoints", help="URLs públicas de los ControlNodes (por defecto, las del inventario)")
@click.option("--ca", help="ca.pem del cluster (o DFSHA_CA)")
@click.option("--user", default="admin", show_default=True)
@click.option("--password", help="(mejor: variable DFSHA_PASSWORD)")
@click.option("--ssh-key", type=click.Path(), help="clave .pem para detener/arrancar contenedores y mirar el disco (pasos 6-8)")
@click.option("--ssh-user", default="ec2-user", show_default=True)
@click.option("--local-cluster", type=click.Path(), help="usa un cluster de dfsha-cluster como si fuera remoto (pruebas del propio validador)")
@click.option("--size-mib", default=64, show_default=True)
@click.option("--wait-repair-s", default=240.0, show_default=True, help="tiempo máximo a esperar la re-replicación tras caer un DataNode")
@click.option("--out", type=click.Path(), help="directorio de evidencia (por defecto deploy/aws/evidence/<fecha>)")
def validate(inventory, endpoints, ca, user, password, ssh_key, ssh_user, local_cluster, size_mib, wait_repair_s, out):  # pragma: no cover
    eps, ca, hosts, lc, password = _env(local_cluster, inventory, endpoints, ca, user, password)
    killer: Killer | None = LocalKiller(lc) if lc else (SSHKiller(hosts, ssh_key, ssh_user) if ssh_key and hosts else None)
    cl = Client(eps, ca, credentials=(user, password), workers=8, retry_window=45)
    cl.login(user, password)
    out_dir = Path(out) if out else Path("deploy/aws/evidence") / time.strftime("%Y%m%d-%H%M%S")
    log_path = out_dir / "log.txt"
    out_dir.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        print(msg, flush=True)
        with open(log_path, "a") as f:
            f.write(msg + "\n")

    res = Validator(cl, eps, ca, hosts=hosts, killer=killer, size_mib=size_mib, out=out_dir, wait_repair_s=wait_repair_s, log=log).run()
    latest = out_dir.parent / "latest"
    try:
        if latest.is_symlink():
            latest.unlink()
        latest.symlink_to(out_dir.name)
    except OSError:
        pass
    click.echo(f"\nEvidencia en {out_dir}  →  {'OK' if res['ok'] else 'CON FALLOS'}")
    raise SystemExit(0 if res["ok"] else 1)


@main.command("bench", help="Benchmarks: scaling | throughput | metadata (ejecútelos desde una VM del MISMO VPC).")
@click.argument("kind", type=click.Choice(["scaling", "throughput", "metadata", "all"]))
@click.option("--inventory", type=click.Path(exists=True))
@click.option("--endpoints")
@click.option("--ca")
@click.option("--user", default="admin", show_default=True)
@click.option("--password")
@click.option("--local-cluster", type=click.Path())
@click.option("--size-mib", default=256, show_default=True)
@click.option("--reps", default=3, show_default=True)
@click.option("--counts", default="1,2,3,4,6,8", show_default=True, help="nº de DataNodes activos a probar (scaling)")
@click.option("--workers", default="8", show_default=True, help="lista separada por comas (throughput)")
@click.option("--out", type=click.Path(), default="bench/results_cloud", show_default=True)
def bench(kind, inventory, endpoints, ca, user, password, local_cluster, size_mib, reps, counts, workers, out):  # pragma: no cover
    eps, ca, _hosts, _lc, password = _env(local_cluster, inventory, endpoints, ca, user, password)

    def new() -> Client:
        c = Client(eps, ca, credentials=(user, password), workers=8, retry_window=45)
        c.login(user, password)
        return c

    cl = new()
    outp = Path(out)
    outp.mkdir(parents=True, exist_ok=True)
    env = {"fecha": time.strftime("%Y-%m-%d %H:%M:%S"), "endpoints": eps, "datanodes": len([d for d in cl.datanodes() if d["alive"]]), "host": socket.gethostname()}
    jobs = ["scaling", "throughput", "metadata"] if kind == "all" else [kind]
    for job in jobs:
        click.echo(f"== {job}")
        if job == "scaling":
            r = bench_scaling(cl, [int(x) for x in counts.split(",")], size_mib, reps, [1, 3])
        elif job == "throughput":
            r = bench_throughput(cl, [size_mib], [int(w) for w in workers.split(",")], reps)
        else:
            r = bench_metadata(new, [1, 8, 32], 5.0)
        (outp / f"{job}.json").write_text(json.dumps({"env": env, "result": r}, indent=1, default=str))
    click.echo(f"resultados en {outp}")


if __name__ == "__main__":  # pragma: no cover
    main()
