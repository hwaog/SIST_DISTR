"""Ensayo del docker-compose SIN contenedores.

Lee docker-compose.yml, genera la PKI con docker/init-pki.sh (SAN = nombres de servicio), arranca los
8 nodos usando ÚNICAMENTE las variables de entorno que el compose declara (mismos nombres, mismos puertos,
mismas URLs ``https://c1:8441``...) y resuelve esos nombres a 127.0.0.1.  Luego ejecuta un humo funcional.
Simula los montajes volumen por volumen y comprueba el AISLAMIENTO DE CLAVES (lo que vería `docker exec dfsha-dn1
ls /pki`). Valida: cableado de variables, formato de peers, SAN por nombre de host, healthcheck, formación del
cluster y que ningún nodo reciba claves ajenas. NO valida: el Dockerfile, permisos reales de uid 10001 ni el
aislamiento de red (requieren Docker).

    uv run python scripts/compose_dryrun.py
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BIN = Path(sys.executable).parent


def interp(v: str, env: dict[str, str]) -> str:
    """Expande ${VAR}, ${VAR:-defecto} y ${VAR:?mensaje} como lo hace docker compose."""

    def rep(m: re.Match) -> str:
        name, op, arg = m.group(1), m.group(2), m.group(3)
        if name in env:
            return env[name]
        if op == "-":
            return arg
        if op == "?":
            raise SystemExit(f"variable obligatoria sin valor: {name} ({arg})")
        return ""

    return re.sub(r"\$\{(\w+)(?::?([-?])([^}]*))?\}", rep, v)


def main() -> int:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    tmp = Path(tempfile.mkdtemp(prefix="dfsha-compose-"))
    dot = {"ADMIN_PASSWORD": "compose-dryrun-pass-1"}
    svc = compose["services"]
    controls = sorted(k for k in svc if re.fullmatch(r"c\d+", k))
    datas = sorted(k for k in svc if re.fullmatch(r"dn\d+", k))
    print(f"compose: {len(controls)} ControlNodes + {len(datas)} DataNodes; tmp={tmp}")

    # 1) PKI exactamente como lo hace el servicio pki-init, y mapa  volumen -> directorio local
    init = svc["pki-init"]
    init_env = {k: interp(str(v), dot) for k, v in init["environment"].items()}
    out_root, state = tmp / "out", tmp / "state"
    subprocess.run(["sh", str(ROOT / "docker" / "init-pki.sh")], check=True, env={**os.environ, **init_env, "OUT_ROOT": str(out_root), "STATE_DIR": str(state), "PATH": f"{BIN}:{os.environ['PATH']}"}, capture_output=True)
    vol_dir: dict[str, Path] = {}
    for v in init["volumes"]:
        name, target = v.split(":")[:2]
        if not name.startswith("."):
            vol_dir[name] = state if target == "/state" else out_root / target.removeprefix("/out/")
    for name in compose["volumes"]:  # volúmenes de datos de cada nodo
        vol_dir.setdefault(name, tmp / "vol" / name)
    print("PKI generada para:", " ".join(sorted(p.name for p in (out_root / "pki").iterdir())))

    def service_mounts(name: str) -> dict[str, Path]:
        """ruta en el contenedor -> directorio local (equivale a lo que verá el contenedor)."""
        m = {}
        for v in svc[name].get("volumes", []):
            parts = v.split(":")
            if not parts[0].startswith("."):
                m[parts[1]] = vol_dir[parts[0]]
        return m

    # 1b) AISLAMIENTO: lo que cada contenedor vería en /pki y /secrets
    def listing(path: Path) -> list[str]:
        return sorted(p.name for p in path.iterdir()) if path.exists() else []

    for n in controls + datas:
        m = service_mounts(n)
        assert listing(m["/pki"]) == sorted(["ca.pem", f"{n}.crt", f"{n}.key"]), f"{n}: /pki = {listing(m['/pki'])}"
        if n in datas:
            assert "/secrets" not in m, f"{n} (DataNode) no debe recibir cluster.key"
        else:
            assert listing(m["/secrets"]) == ["admin.pass", "cluster.key"], f"{n}: /secrets = {listing(m['/secrets'])}"
    assert listing(service_mounts("client")["/pki"]) == ["ca.pem"]
    assert not list(out_root.rglob("ca.key")), "ca.key apareció fuera del volumen de estado de pki-init"
    print("AISLAMIENTO DE CLAVES: OK  (p. ej. dn1 ve /pki =", listing(service_mounts("dn1")["/pki"]), "y no tiene /secrets)")

    # 2) resolución de nombres de servicio -> 127.0.0.1 (sustituye al DNS de Docker)
    hooks = tmp / "hooks"
    hooks.mkdir()
    names = controls + datas
    (hooks / "sitecustomize.py").write_text(
        f"import socket\n_o = socket.getaddrinfo\n_N = {set(names)!r}\n"
        "def _p(host, *a, **k):\n"
        "    h = host.decode() if isinstance(host, bytes) else host  # anyio pasa el host como bytes\n"
        "    return _o('127.0.0.1' if h in _N else host, *a, **k)\n"
        "socket.getaddrinfo = _p\n"
    )

    procs: list[subprocess.Popen] = []

    def spawn(name: str) -> None:
        s_ = svc[name]
        env = {k: interp(str(v), dot) for k, v in s_["environment"].items()}
        mounts = sorted(service_mounts(name).items(), key=lambda kv: -len(kv[0]))
        for k, v in env.items():  # rutas del contenedor -> directorios de SUS volúmenes
            for target, local in mounts:
                if v == target or v.startswith(target + "/"):
                    v = str(local) + v[len(target):]
                    break
            env[k] = v
        for _t, local in mounts:
            local.mkdir(parents=True, exist_ok=True)
        if os.environ.get("KEEP"):
            env["DFSHA_LOG_LEVEL"] = "debug"
        log = open(tmp / f"{name}.log", "wb")  # noqa: SIM115
        procs.append(subprocess.Popen([str(BIN / s_["command"][0])], env={**os.environ, **env, "PYTHONPATH": str(hooks), "PYTHONUNBUFFERED": "1"}, stdout=log, stderr=subprocess.STDOUT, start_new_session=True))

    rc = 1
    try:
        for n in controls + datas:
            spawn(n)
        endpoints = svc["client"]["environment"]["DFSHA_ENDPOINTS"]
        cenv = {"DFSHA_ENDPOINTS": endpoints, "DFSHA_CA": str(service_mounts("client")["/pki"] / "ca.pem"), "DFSHA_USER": "admin", "DFSHA_PASSWORD": dot["ADMIN_PASSWORD"], "DFSHA_HOME": str(tmp / "client"), "PYTHONPATH": str(hooks), "PATH": f"{BIN}:{os.environ['PATH']}", "HOME": os.environ["HOME"]}
        end = time.time() + 90
        ok = False
        while time.time() < end:
            r = subprocess.run([str(BIN / "dfsha"), "nodes"], env=cenv, capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.count(" sí ") >= len(datas) and "leader" in r.stdout:
                ok = True
                break
            time.sleep(1)
        if not ok:
            print("el cluster no se formó:\n", r.stdout[-800:], r.stderr[-800:], flush=True)
            if os.environ.get("HOLD"):
                time.sleep(int(os.environ["HOLD"]))
            for n in controls + datas[:1]:
                print(f"--- {n}.log\n", (tmp / f"{n}.log").read_text()[-1200:])
            return 1
        print("cluster formado solo con las variables del compose:")
        print("\n".join(ln for ln in r.stdout.splitlines() if "│" in ln and ("leader" in ln or "follower" in ln or " dn" in ln))[:900])
        # humo funcional (put/get con verificación)
        data = tmp / "datos.bin"
        data.write_bytes(os.urandom(9 * 1024 * 1024))
        for cmd in (["put", str(data), "/home/admin/datos.bin"], ["get", "/home/admin/datos.bin", str(tmp / "copia.bin")]):
            subprocess.run([str(BIN / "dfsha"), *cmd], env=cenv, check=True, capture_output=True)
        assert data.read_bytes() == (tmp / "copia.bin").read_bytes()
        print("put/get de 9 MiB verificado byte a byte a través de las URLs del compose (https://c1:8441, https://dnN:744N)")
        # healthcheck de contenedor (el mismo script que usa el compose)
        for port, who in ((8441, "c1"), (7441, "dn1")):
            h = subprocess.run([sys.executable, str(ROOT / "docker" / "healthcheck.py"), str(port), str(service_mounts("client")["/pki"] / "ca.pem")], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(hooks)})
            print(f"healthcheck {who}:{port} ->", "OK" if h.returncode == 0 else f"FALLA {h.stderr[:200]}")
            assert h.returncode == 0
        # equivalente a `docker stop dfsha-<líder>`: el servicio debe seguir funcionando
        lead = re.search(r"│ (c\d+) +│ leader", r.stdout).group(1)
        os.killpg(procs[controls.index(lead)].pid, signal.SIGKILL)
        print(f"líder {lead} detenido (SIGKILL); comprobando que el servicio continúa...")
        t0 = time.time()
        for cmd in (["mkdir", "/home/admin/tras_failover"], ["put", str(data), "/home/admin/tras_failover/datos.bin"], ["get", "/home/admin/tras_failover/datos.bin", str(tmp / "copia2.bin")]):
            subprocess.run([str(BIN / "dfsha"), *cmd], env=cenv, check=True, capture_output=True)
        assert data.read_bytes() == (tmp / "copia2.bin").read_bytes()
        n2 = subprocess.run([str(BIN / "dfsha"), "nodes"], env=cenv, capture_output=True, text=True).stdout
        new_lead = re.search(r"│ (c\d+) +│ leader", n2).group(1)
        assert new_lead != lead
        print(f"servicio continuo tras la caída del líder: nuevo líder {new_lead}; mkdir+put+get en {time.time() - t0:.1f}s")
        rc = 0
    finally:
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(1.5)
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if rc == 0 or not os.environ.get("KEEP"):
            shutil.rmtree(tmp, ignore_errors=True)
    print("ENSAYO DEL COMPOSE:", "OK" if rc == 0 else "FALLÓ")
    return rc


if __name__ == "__main__":
    sys.exit(main())
