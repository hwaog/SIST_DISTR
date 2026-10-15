"""Cluster DFSha local en procesos reales (TLS + mTLS), para desarrollo, demos, tests y benchmarks.

    dfsha-cluster up --control 3 --data 5 --zones 2
    eval "$(dfsha-cluster env)"          # exporta DFSHA_ENDPOINTS, DFSHA_CA, DFSHA_USER...
    dfsha login && dfsha ls /
    dfsha-cluster kill dn2               # caos: matar un DataNode (SIGKILL)
    dfsha-cluster kill leader            # caos: matar al líder Raft
    dfsha-cluster down

Cada nodo es un proceso independiente con su propio directorio de datos y su
certificado; la comunicación pasa por la red de loopback con exactamente el
mismo código y TLS que en producción.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import click
import httpx

from .pki import client_context, create_ca, create_cluster_secret, issue_cert

ADMIN_PASS_DEFAULT = "dfsha-admin-dev"


def _free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_base_port(span: int = 400, host: str = "127.0.0.1") -> int:
    import random

    for _ in range(200):
        base = random.randrange(20000, 60000 - span)
        probe = [base + off for off in (1, 2, 3, 4, 5, 101, 102, 103, 104, 105, 201, 202, 203, 204, 205, 206, 207, 208, 301, 302, 303, 304, 305, 306, 307, 308)]
        if all(_free(p, host) for p in probe):
            return base
    raise RuntimeError("no encontré un rango de puertos libre")


@dataclass
class Proc:
    name: str
    kind: str  # control | data
    args: list[str]
    pid: int | None = None


class LocalCluster:
    def __init__(
        self,
        base: str | Path,
        *,
        n_control: int = 3,
        n_data: int = 4,
        zones: int = 2,
        host: str = "127.0.0.1",
        base_port: int | None = None,
        fast: bool = True,
        repl: int = 3,
        block_size: str = "4MiB",
        admin_password: str = ADMIN_PASS_DEFAULT,
        fsync: bool = True,
        write_quorum: str = "majority",
        data_args: list[str] | None = None,
        control_args: list[str] | None = None,
        scrub_interval: float = 3600.0,
    ) -> None:
        self.base = Path(base).resolve()
        self.n_control, self.n_data, self.zones = n_control, n_data, max(1, zones)
        self.host = host
        self.base_port = base_port or find_base_port(host=host)
        self.fast, self.repl, self.block_size = fast, repl, block_size
        self.admin_password = admin_password
        self.fsync, self.write_quorum = fsync, write_quorum
        self.data_args, self.control_args = data_args or [], control_args or []
        self.scrub_interval = scrub_interval
        self.procs: dict[str, Proc] = {}
        self.pki = self.base / "pki"
        self.logs = self.base / "logs"

    # ---------------------------------------------------------- topología
    def control_urls(self, i: int) -> tuple[str, str]:
        return (f"https://{self.host}:{self.base_port + i}", f"https://{self.host}:{self.base_port + 100 + i}")

    def data_urls(self, j: int) -> tuple[str, str]:
        return (f"https://{self.host}:{self.base_port + 200 + j}", f"https://{self.host}:{self.base_port + 300 + j}")

    @property
    def endpoints(self) -> list[str]:
        return [self.control_urls(i)[0] for i in range(1, self.n_control + 1)]

    @property
    def ca(self) -> str:
        return str(self.pki / "ca.pem")

    @property
    def peers_spec(self) -> str:
        return ";".join(f"c{i}={self.control_urls(i)[0]},{self.control_urls(i)[1]}" for i in range(1, self.n_control + 1))

    @property
    def controls_internal(self) -> str:
        return ",".join(self.control_urls(i)[1] for i in range(1, self.n_control + 1))

    # ----------------------------------------------------------- preparación
    def prepare(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(exist_ok=True)
        create_ca(self.pki)
        secret = create_cluster_secret(self.base / "secrets" / "cluster.key")
        (self.base / "admin.pass").write_text(self.admin_password)
        os.chmod(self.base / "admin.pass", 0o600)
        for i in range(1, self.n_control + 1):
            issue_cert(self.pki, name=f"c{i}", role="controlnode", sans=[self.host])
            name = f"c{i}"
            self.procs[name] = Proc(name, "control", self._control_args(i, secret))
        for j in range(1, self.n_data + 1):
            issue_cert(self.pki, name=f"dn{j}", role="datanode", sans=[self.host])
            name = f"dn{j}"
            self.procs[name] = Proc(name, "data", self._data_args(j))
        issue_cert(self.pki, name="admin-tool", role="admin", sans=[self.host])

    def _control_args(self, i: int, secret: Path) -> list[str]:
        a = [
            "--id", f"c{i}", "--peers", self.peers_spec, "--data-dir", str(self.base / "data" / f"c{i}"),
            "--cert", str(self.pki / f"c{i}.crt"), "--key", str(self.pki / f"c{i}.key"), "--ca", self.ca,
            "--cluster-secret", str(secret), "--host", self.host,
            "--admin-password-file", str(self.base / "admin.pass"), "--repl", str(self.repl),
            "--block-size", self.block_size, "--write-quorum", self.write_quorum,
        ]  # fmt: skip
        if self.fast:
            a += ["--election-timeout", "0.5", "1.0", "--heartbeat", "0.1", "--dn-dead-after", "6",
                  "--repl-grace", "3", "--gc-grace", "3", "--balance-interval", "0"]  # fmt: skip
        if not self.fsync:
            a.append("--no-fsync")
        return a + self.control_args

    def _data_args(self, j: int) -> list[str]:
        pub, internal = self.data_urls(j)
        a = [
            "--id", f"dn{j}", "--controls", self.controls_internal, "--data-dir", str(self.base / "data" / f"dn{j}"),
            "--cert", str(self.pki / f"dn{j}.crt"), "--key", str(self.pki / f"dn{j}.key"), "--ca", self.ca,
            "--zone", f"zone-{(j - 1) % self.zones + 1}", "--host", self.host,
            "--api-port", str(self.base_port + 200 + j), "--internal-port", str(self.base_port + 300 + j),
            "--advertise-public", pub, "--advertise-internal", internal, "--scrub-interval", str(self.scrub_interval),
        ]  # fmt: skip
        if self.fast:
            a += ["--heartbeat", "1.0", "--report-interval", "20"]
        if not self.fsync:
            a.append("--no-fsync")
        return a + self.data_args

    def add_data_node(self) -> str:
        """Agrega (y arranca) un DataNode nuevo a un cluster en marcha: escalado horizontal."""
        self.n_data += 1
        j = self.n_data
        issue_cert(self.pki, name=f"dn{j}", role="datanode", sans=[self.host])
        self.procs[f"dn{j}"] = Proc(f"dn{j}", "data", self._data_args(j))
        self.start(f"dn{j}")
        return f"dn{j}"

    # ----------------------------------------------------------- procesos
    def _spawn(self, p: Proc) -> None:
        mod = "dfsha.control.main" if p.kind == "control" else "dfsha.data.main"
        log = open(self.logs / f"{p.name}.log", "ab")  # noqa: SIM115
        proc = subprocess.Popen(
            [sys.executable, "-m", mod, *p.args], stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )  # fmt: skip
        p.pid = proc.pid
        self._popen = getattr(self, "_popen", {})
        self._popen[p.name] = proc

    def start(self, name: str) -> None:
        p = self.procs[name]
        if p.pid and self.alive(name):
            return
        self._spawn(p)
        self.save()

    def start_all(self) -> None:
        for name in self.procs:
            self.start(name)

    def alive(self, name: str) -> bool:
        p = self.procs[name]
        if not p.pid:
            return False
        po = getattr(self, "_popen", {}).get(name)
        if po is not None:
            return po.poll() is None
        try:
            os.kill(p.pid, 0)
            return True
        except OSError:
            return False

    def kill(self, name: str, sig: int = signal.SIGKILL) -> None:
        p = self.procs[name]
        if p.pid and self.alive(name):
            try:
                os.killpg(p.pid, sig)
            except ProcessLookupError:
                pass
            po = getattr(self, "_popen", {}).get(name)
            if po is not None:
                try:
                    po.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    po.wait(timeout=5)
            else:
                for _ in range(100):
                    if not self.alive(name):
                        break
                    time.sleep(0.1)
        p.pid = None
        self.save()

    def stop(self, name: str) -> None:
        self.kill(name, signal.SIGTERM)

    def restart(self, name: str) -> None:
        self.kill(name, signal.SIGKILL)
        self.start(name)

    def down(self) -> None:
        """Apagado ordenado en paralelo: SIGTERM a todos; SIGKILL a quien siga vivo a los 8 s."""
        for name, p in self.procs.items():
            if p.pid and self.alive(name):
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        end = time.monotonic() + 8
        while time.monotonic() < end and any(self.alive(n) for n in self.procs):
            time.sleep(0.1)
        for name in list(self.procs):
            self.kill(name, signal.SIGKILL)

    def destroy(self) -> None:
        self.down()
        shutil.rmtree(self.base, ignore_errors=True)

    # ------------------------------------------------------------- consultas
    def _client(self) -> httpx.Client:
        return httpx.Client(verify=client_context(self.ca), timeout=3.0)

    def node_info(self, i: int) -> dict | None:
        try:
            with self._client() as c:
                r = c.get(f"{self.control_urls(i)[0]}/v1/node")
                return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    def leader(self) -> str | None:
        """Nombre (c1, c2...) del líder Raft actual, según los propios nodos."""
        votes: dict[str, int] = {}
        for i in range(1, self.n_control + 1):
            info = self.node_info(i)
            if info and info["role"] == "leader":
                votes[info["id"]] = votes.get(info["id"], 0) + 1
        return next(iter(votes)) if len(votes) == 1 else None

    def wait_ready(self, timeout: float = 60.0, datanodes: int | None = None) -> None:
        """Espera: líder elegido, admin creado y todos los DataNodes vivos y registrados."""
        want_dn = self.n_data if datanodes is None else datanodes
        end = time.monotonic() + timeout
        last = ""
        while time.monotonic() < end:
            try:
                ldr = self.leader()
                if ldr:
                    with self._client() as c:
                        r = c.post(
                            f"{self.control_urls(int(ldr[1:]))[0]}/v1/auth/login",
                            json={"username": "admin", "password": self.admin_password},
                        )
                        if r.status_code == 200:
                            tok = r.json()["token"]
                            d = c.get(
                                f"{self.control_urls(int(ldr[1:]))[0]}/v1/admin/datanodes",
                                headers={"Authorization": f"Bearer {tok}"},
                            ).json()
                            alive = sum(1 for x in d if x["alive"])
                            last = f"líder={ldr}, DataNodes vivos {alive}/{want_dn}"
                            if alive >= want_dn:
                                return
                        else:
                            last = f"login admin: HTTP {r.status_code}"
                else:
                    last = "sin líder"
            except Exception as e:
                last = repr(e)
            time.sleep(0.3)
        raise TimeoutError(f"el cluster no quedó listo: {last}\n{self.tail_logs()}")

    def tail_logs(self, n: int = 12) -> str:
        out = []
        for name in sorted(self.procs):
            f = self.logs / f"{name}.log"
            if f.exists():
                lines = f.read_text(errors="replace").splitlines()[-n:]
                out.append(f"--- {name} ---\n" + "\n".join(lines))
        return "\n".join(out)

    def env(self) -> dict[str, str]:
        return {
            "DFSHA_ENDPOINTS": ",".join(self.endpoints),
            "DFSHA_CA": self.ca,
            "DFSHA_USER": "admin",
            "DFSHA_PASSWORD": self.admin_password,
            "DFSHA_HOME": str(self.base / "client"),
        }

    # ---------------------------------------------------------- persistencia
    def save(self) -> None:
        state = {
            "base_port": self.base_port, "n_control": self.n_control, "n_data": self.n_data, "zones": self.zones,
            "host": self.host, "repl": self.repl, "block_size": self.block_size, "fast": self.fast,
            "admin_password": self.admin_password, "fsync": self.fsync, "write_quorum": self.write_quorum,
            "procs": {k: {"kind": v.kind, "args": v.args, "pid": v.pid} for k, v in self.procs.items()},
        }  # fmt: skip
        (self.base / "cluster.json").write_text(json.dumps(state, indent=1))

    @classmethod
    def load(cls, base: str | Path) -> LocalCluster:
        s = json.loads((Path(base) / "cluster.json").read_text())
        c = cls(
            base, n_control=s["n_control"], n_data=s["n_data"], zones=s["zones"], host=s["host"],
            base_port=s["base_port"], fast=s["fast"], repl=s["repl"], block_size=s["block_size"],
            admin_password=s["admin_password"], fsync=s["fsync"], write_quorum=s["write_quorum"],
        )  # fmt: skip
        c.procs = {k: Proc(k, v["kind"], v["args"], v["pid"]) for k, v in s["procs"].items()}
        return c


# --------------------------------------------------------------------------- CLI


@click.group(help="Cluster DFSha local (procesos reales con TLS/mTLS).")
@click.option("--dir", "base", default=".cluster", show_default=True, type=click.Path())
@click.pass_context
def main(ctx: click.Context, base: str) -> None:  # pragma: no cover - CLI fina
    ctx.obj = base


@main.command(help="Crea (si hace falta) y arranca el cluster.")
@click.option("--control", "n_control", default=3, show_default=True)
@click.option("--data", "n_data", default=5, show_default=True)
@click.option("--zones", default=2, show_default=True)
@click.option("--repl", default=3, show_default=True)
@click.option("--block-size", default="4MiB", show_default=True)
@click.option("--admin-password", default=ADMIN_PASS_DEFAULT, show_default=True)
@click.option("--no-fsync", is_flag=True)
@click.option("--balance-interval", default=0.0, show_default=True, help="Segundos entre pasadas del balanceador (0 = desactivado).")
@click.option("--timeout", default=60.0)
@click.pass_obj
def up(base, n_control, n_data, zones, repl, block_size, admin_password, no_fsync, balance_interval, timeout):  # pragma: no cover
    if (Path(base) / "cluster.json").exists():
        c = LocalCluster.load(base)
    else:
        c = LocalCluster(base, n_control=n_control, n_data=n_data, zones=zones, repl=repl, block_size=block_size, admin_password=admin_password, fsync=not no_fsync,
                         control_args=["--balance-interval", str(balance_interval)] if balance_interval else None)
        c.prepare()
    c.start_all()
    c.save()
    c.wait_ready(timeout)
    click.echo(f"Cluster listo: {c.n_control} ControlNodes, {c.n_data} DataNodes. Líder: {c.leader()}")
    click.echo("Use:  eval \"$(dfsha-cluster env)\"")


@main.command(help="Imprime exports de entorno para el CLI 'dfsha'.")
@click.pass_obj
def env(base):  # pragma: no cover
    for k, v in LocalCluster.load(base).env().items():
        click.echo(f"export {k}='{v}'")


@main.command(help="Estado de los procesos.")
@click.pass_obj
def status(base):  # pragma: no cover
    c = LocalCluster.load(base)
    ldr = c.leader()
    for name, p in c.procs.items():
        mark = "UP  " if c.alive(name) else "DOWN"
        extra = " (líder)" if name == ldr else ""
        click.echo(f"{mark} {name:6s} pid={p.pid}{extra}")


@main.command(help="SIGKILL a un nodo (c1, dn2...) o a 'leader'.")
@click.argument("target")
@click.pass_obj
def kill(base, target):  # pragma: no cover
    c = LocalCluster.load(base)
    name = c.leader() if target == "leader" else target
    if not name:
        raise click.ClickException("no hay líder")
    c.kill(name)
    click.echo(f"{name} terminado (SIGKILL)")


@main.command(help="(Re)inicia un nodo detenido.")
@click.argument("name")
@click.pass_obj
def start(base, name):  # pragma: no cover
    c = LocalCluster.load(base)
    c.start(name)
    click.echo(f"{name} iniciado")


@main.command(help="Detiene todo el cluster (conserva los datos).")
@click.pass_obj
def down(base):  # pragma: no cover
    LocalCluster.load(base).down()


@main.command(help="Detiene y BORRA el cluster y sus datos.")
@click.pass_obj
def destroy(base):  # pragma: no cover
    LocalCluster.load(base).destroy()


if __name__ == "__main__":  # pragma: no cover
    main()
