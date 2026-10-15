"""Benchmarks reproducibles de DFSha sobre un cluster local real (procesos + TLS/mTLS).

    uv run python bench/bench.py all            # todo (≈10-15 min)
    uv run python bench/bench.py scaling blocksize workers metadata clients failover availability

Escribe  bench/results/<experimento>.json  y  bench/results/summary.json.
Nota metodológica: ControlNodes, DataNodes y clientes comparten UNA máquina (CPU/disco/loopback),
así que la escalabilidad horizontal medida es un límite inferior de lo que se obtendría con
nodos en máquinas distintas (en la nube).  Ver docs/RESULTADOS.md.
"""

from __future__ import annotations

import json
import os
import platform
import statistics as st
import sys
import tempfile
import threading
import time
from pathlib import Path

from dfsha.client.sdk import Client
from dfsha.cluster import LocalCluster
from dfsha.common import errors as E

MiB = 1024 * 1024
RESULTS = Path(__file__).parent / "results"
RESULTS.mkdir(exist_ok=True)


def mk(n_data: int, *, repl: int = 3, block_size: str = "4MiB", fsync: bool = False, n_control: int = 3, **kw) -> LocalCluster:
    base = Path(tempfile.mkdtemp(prefix="dfsha-bench-"))
    c = LocalCluster(base, n_control=n_control, n_data=n_data, repl=repl, block_size=block_size, fsync=fsync, zones=2, **kw)
    c.prepare()
    c.start_all()
    c.wait_ready(90)
    return c


def admin(c: LocalCluster, **kw) -> Client:
    cl = Client(c.endpoints, c.ca, credentials=("admin", c.admin_password), **kw)
    cl.login("admin", c.admin_password)
    return cl


def stats(xs: list[float]) -> dict:
    return {"mean": st.mean(xs), "sd": st.pstdev(xs) if len(xs) > 1 else 0.0, "min": min(xs), "max": max(xs), "n": len(xs), "all": xs}


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]


def throughput(cl: Client, size_mib: int, reps: int = 3, tag: str = "t") -> dict:
    """MB/s de subida y bajada de un archivo de `size_mib` MiB (datos aleatorios incompresibles)."""
    data = os.urandom(size_mib * MiB)
    ups, downs = [], []
    for i in range(reps):
        p = f"/home/admin/{tag}_{i}"
        t = time.perf_counter()
        cl.send(data, p)
        ups.append(size_mib / (time.perf_counter() - t))
        t = time.perf_counter()
        out = cl.receive(p)
        downs.append(size_mib / (time.perf_counter() - t))
        assert out == data
        cl.rm(p)
    return {"upload_MiBps": stats(ups), "download_MiBps": stats(downs)}


def exp_scaling() -> dict:
    """Throughput vs. nº de DataNodes (repl=1 para aislar el efecto de repartir bloques; y repl=3)."""
    res: dict = {"repl1": {}, "repl3": {}}
    for repl, counts in ((1, (1, 2, 3, 4, 6)), (3, (3, 4, 6))):
        for n in counts:
            c = mk(n, repl=repl)
            try:
                cl = admin(c, workers=8)
                res[f"repl{repl}"][n] = throughput(cl, 256, reps=3, tag=f"s{n}")
                print(f"  repl={repl} DataNodes={n}: {res[f'repl{repl}'][n]['upload_MiBps']['mean']:.0f} / {res[f'repl{repl}'][n]['download_MiBps']['mean']:.0f} MiB/s (sube/baja)", flush=True)
            finally:
                c.destroy()
    return res


def exp_scaling_bw() -> dict:
    """Escalado horizontal con un TOPE DE ANCHO DE BANDA por DataNode (40 MiB/s), como el de una VM real.

    En una sola máquina el cuello de botella es la CPU del cliente; limitar cada DataNode emula nodos
    con red/disco propios y deja ver cómo el reparto de bloques multiplica el ancho de banda agregado.
    """
    res: dict = {"repl1": {}, "repl3": {}}
    for repl, counts in ((1, (1, 2, 3, 4, 6, 8)), (3, (3, 4, 6, 8))):
        for n in counts:
            c = mk(n, repl=repl, data_args=["--bandwidth-limit", "40MiB"])
            try:
                cl = admin(c, workers=16)
                res[f"repl{repl}"][n] = throughput(cl, 160, reps=2, tag=f"sb{n}")
                r = res[f"repl{repl}"][n]
                print(f"  tope 40 MiB/s/nodo · repl={repl} DataNodes={n}: {r['upload_MiBps']['mean']:.0f} / {r['download_MiBps']['mean']:.0f} MiB/s", flush=True)
            finally:
                c.destroy()
    return res


def exp_blocksize() -> dict:
    res = {}
    for bs in ("1MiB", "2MiB", "4MiB", "8MiB", "16MiB", "32MiB"):
        c = mk(4, block_size=bs)
        try:
            cl = admin(c, workers=8)
            res[bs] = throughput(cl, 256, reps=3, tag="b")
            print(f"  bloque={bs}: {res[bs]['upload_MiBps']['mean']:.0f} / {res[bs]['download_MiBps']['mean']:.0f} MiB/s", flush=True)
        finally:
            c.destroy()
    return res


def exp_workers() -> dict:
    c = mk(4)
    res = {}
    try:
        for w in (1, 2, 4, 8, 16, 32):
            cl = admin(c, workers=w)
            res[w] = throughput(cl, 256, reps=3, tag=f"w{w}")
            print(f"  workers={w}: {res[w]['upload_MiBps']['mean']:.0f} / {res[w]['download_MiBps']['mean']:.0f} MiB/s", flush=True)
    finally:
        c.destroy()
    return res


def _hammer(fn, threads: int, seconds: float) -> dict:
    lat: list[float] = []
    errs = [0]
    stop = time.monotonic() + seconds
    lock = threading.Lock()

    def run(k: int) -> None:
        mine = []
        i = 0
        while time.monotonic() < stop:
            t = time.perf_counter()
            try:
                fn(k, i)
                mine.append((time.perf_counter() - t) * 1000)
            except E.DfshaError:
                with lock:
                    errs[0] += 1
            i += 1
        with lock:
            lat.extend(mine)

    ts = [threading.Thread(target=run, args=(k,)) for k in range(threads)]
    t0 = time.perf_counter()
    [t.start() for t in ts]
    [t.join() for t in ts]
    dt = time.perf_counter() - t0
    return {"ops_per_s": len(lat) / dt, "p50_ms": pct(lat, 50), "p95_ms": pct(lat, 95), "p99_ms": pct(lat, 99), "errors": errs[0], "ops": len(lat)}


def exp_metadata() -> dict:
    """Latencia/rendimiento de operaciones de metadatos: lecturas con lease, lecturas stale y escrituras Raft (fsync on/off)."""
    res: dict = {}
    for fsync in (False, True):
        c = mk(4, fsync=fsync)
        try:
            clients = [admin(c) for _ in range(32)]
            admin(c).mkdir("/home/admin/m", parents=True)
            for threads in (1, 8, 32):
                key = f"fsync={'on' if fsync else 'off'},threads={threads}"
                res[key] = {
                    "stat_linearizable": _hammer(lambda k, i: clients[k].stat("/home/admin/m"), threads, 4),
                    "stat_stale_followers": _hammer(lambda k, i: clients[k].stat("/home/admin/m", stale=True), threads, 4),
                    "mkdir_raft_write": _hammer(lambda k, i: clients[k].mkdir(f"/home/admin/m/t{threads}_{k}_{i}"), threads, 4),
                }
                r = res[key]
                print(f"  {key}: stat {r['stat_linearizable']['ops_per_s']:.0f}/s (p99 {r['stat_linearizable']['p99_ms']:.1f} ms), stale {r['stat_stale_followers']['ops_per_s']:.0f}/s, mkdir {r['mkdir_raft_write']['ops_per_s']:.0f}/s (p99 {r['mkdir_raft_write']['p99_ms']:.1f} ms)", flush=True)
        finally:
            c.destroy()
    return res


def exp_clients() -> dict:
    """Escalabilidad en nº de clientes concurrentes (cada uno sube y baja 64 MiB)."""
    c = mk(6)
    res = {}
    try:
        for n in (1, 2, 4, 8, 16):
            clients = [admin(c, workers=4) for _ in range(n)]
            data = os.urandom(64 * MiB)
            done = []

            def work(k: int) -> None:
                p = f"/home/admin/cc{n}_{k}"
                clients[k].send(data, p)
                assert clients[k].receive(p) == data
                done.append(1)

            t = time.perf_counter()
            ts = [threading.Thread(target=work, args=(k,)) for k in range(n)]
            [x.start() for x in ts]
            [x.join() for x in ts]
            dt = time.perf_counter() - t
            res[n] = {"agg_MiBps": 2 * 64 * n / dt, "per_client_MiBps": 2 * 64 / dt, "seconds": dt, "ok": len(done)}
            print(f"  clientes={n}: agregado {res[n]['agg_MiBps']:.0f} MiB/s, por cliente {res[n]['per_client_MiBps']:.0f} MiB/s", flush=True)
    finally:
        c.destroy()
    return res


def exp_failover() -> dict:
    """Tiempo hasta que una escritura vuelve a tener éxito tras matar al líder (5 repeticiones) y tiempo de re-replicación."""
    c = mk(5)
    out: dict = {"leader_failover_s": [], "rereplication": {}}
    try:
        cl = admin(c, retry_window=60)
        for i in range(5):
            ldr = c.leader()
            t0 = time.perf_counter()
            c.kill(ldr)
            while True:
                try:
                    cl.mkdir(f"/home/admin/f{i}")
                    break
                except E.DfshaError:
                    time.sleep(0.02)
            out["leader_failover_s"].append(time.perf_counter() - t0)
            c.start(ldr)
            time.sleep(3)
            print(f"  failover {i + 1}: {out['leader_failover_s'][-1]:.2f}s (líder anterior {ldr})", flush=True)
        # re-replicación: 256 MiB, matar un DataNode y medir hasta restaurar 3 réplicas VIVAS
        data = os.urandom(256 * MiB)
        cl.send(data, "/home/admin/big")
        time.sleep(4)
        victim = "dn1"
        t0 = time.perf_counter()
        c.kill(victim)
        detected = repaired = None
        while time.perf_counter() - t0 < 180:
            f = cl.fsck()
            if detected is None and (f["under_replicated"] > 0):
                detected = time.perf_counter() - t0
            if detected is not None and f["under_replicated"] == 0 and f["lost"] == 0:
                repaired = time.perf_counter() - t0
                break
            time.sleep(0.25)
        out["rereplication"] = {"data_MiB": 256, "detect_s": detected, "repaired_s": repaired}
        print(f"  re-replicación: detectada a {detected:.1f}s, reparada a {repaired:.1f}s", flush=True)
        assert cl.receive("/home/admin/big") == data
    finally:
        c.destroy()
    return out


def exp_availability() -> dict:
    """Carga continua de lecturas+escrituras mientras se matan nodos; mide éxito y latencia."""
    c = mk(5)
    out: dict = {}
    try:
        base = admin(c)
        payload = os.urandom(2 * MiB)
        base.send(payload, "/home/admin/probe")
        events: list[tuple[float, str]] = []
        ok = {"read": 0, "write": 0}
        bad = {"read": 0, "write": 0}
        lat = {"read": [], "write": []}
        samples: list[tuple[float, str, float]] = []  # (t_fin, tipo, latencia_ms)
        stop = threading.Event()
        t_start = time.perf_counter()

        def loop(kind: str, k: int) -> None:
            cl = admin(c, workers=2, retry_window=30)
            i = 0
            while not stop.is_set():
                t = time.perf_counter()
                try:
                    if kind == "read":
                        assert cl.receive("/home/admin/probe") == payload
                    else:
                        cl.send(payload, f"/home/admin/av_{k}_{i % 20}")
                    ok[kind] += 1
                    lat[kind].append((time.perf_counter() - t) * 1000)
                    samples.append((time.perf_counter() - t_start, kind, lat[kind][-1]))
                except Exception:
                    bad[kind] += 1
                i += 1

        threads = [threading.Thread(target=loop, args=(kind, k)) for kind in ("read", "write") for k in range(3)]
        [t.start() for t in threads]
        # Plan de caos: nunca hay más de UN ControlNode caído a la vez (f = 1 de 3) y hasta 2 DataNodes.
        plan = [(8, "kill dn1"), (16, "start dn1"), (24, "kill leader"), (34, "start leader"), (40, "kill dn2"), (48, "kill follower"), (58, "start all")]
        dead_leader = None
        for at, what in plan:
            time.sleep(max(0, at - (time.perf_counter() - t_start)))
            events.append((time.perf_counter() - t_start, what))
            if what == "kill dn1":
                c.kill("dn1")
            elif what == "start dn1":
                c.start("dn1")
            elif what == "kill leader":
                dead_leader = c.leader()
                c.kill(dead_leader)
                events[-1] = (events[-1][0], f"kill leader {dead_leader}")
            elif what == "start leader":
                c.start(dead_leader)
                events[-1] = (events[-1][0], f"start {dead_leader}")
            elif what == "kill dn2":
                c.kill("dn2")
            elif what == "kill follower":
                ldr = c.leader()
                fol = next(n for n in ("c1", "c2", "c3") if n != ldr and c.alive(n))
                c.kill(fol)
                events[-1] = (events[-1][0], f"kill follower {fol}")
            elif what == "start all":
                for n in list(c.procs):
                    c.start(n)
        time.sleep(max(0, 72 - (time.perf_counter() - t_start)))
        stop.set()
        [t.join() for t in threads]
        out = {
            "duration_s": time.perf_counter() - t_start, "events": events, "ok": ok, "failed": bad,
            "availability_pct": {k: 100 * ok[k] / max(1, ok[k] + bad[k]) for k in ok},
            "samples": [(round(a, 3), b, round(c, 2)) for a, b, c in samples],
            "latency_ms": {k: {"p50": pct(v, 50), "p95": pct(v, 95), "p99": pct(v, 99), "max": max(v)} for k, v in lat.items() if v},
        }
        print(f"  disponibilidad lecturas {out['availability_pct']['read']:.2f}%  escrituras {out['availability_pct']['write']:.2f}%  (ok={ok} fallos={bad})", flush=True)
    finally:
        c.destroy()
    return out


def micro() -> dict:
    """Costo de la criptografía y del hashing por bloque (microbenchmark, un solo hilo)."""
    import hashlib

    from dfsha.security import crypto

    blk = os.urandom(4 * MiB)
    dek = crypto.new_dek()
    t = time.perf_counter()
    for _ in range(50):
        blob = crypto.encrypt_block(dek, "a" * 32, 1, blk)
    enc = 50 * 4 / (time.perf_counter() - t)
    t = time.perf_counter()
    for _ in range(50):
        crypto.decrypt_block(dek, "a" * 32, blob)
    dec = 50 * 4 / (time.perf_counter() - t)
    t = time.perf_counter()
    for _ in range(50):
        hashlib.sha256(blk).hexdigest()
    sha = 50 * 4 / (time.perf_counter() - t)
    return {"aes256gcm_encrypt_MiBps": enc, "aes256gcm_decrypt_MiBps": dec, "sha256_MiBps": sha, "block_MiB": 4}


def exp_meta_mem() -> dict:
    """Memoria de metadatos por archivo y por bloque + velocidad pura de la máquina de estados (sin red)."""
    import tracemalloc
    import uuid

    from dfsha.control.fsm import MetadataStore

    def build(nfiles: int, nblocks: int) -> tuple[float, float]:
        fs, i = MetadataStore(), 0

        def do(op, **kw):
            nonlocal i
            i += 1
            return fs.apply(i, {"op": op, "ts": 1000.0 + i * 1e-3, **kw})

        do("bootstrap_admin", pw="x")
        for d in ("d1", "d2", "d3"):
            do("dn_register", dn=d, zone="z", public_url="u", internal_url="i")
        do("mkdir", user="admin", path="/home/admin/data")
        tracemalloc.start()
        base = tracemalloc.get_traced_memory()[0]
        t = time.perf_counter()
        for k in range(nfiles):
            sid = uuid.uuid4().hex
            do("begin_write", user="admin", path=f"/home/admin/data/f{k}", sid=sid, repl=3, min_repl=2, bs=4194304, ttl=900, dek="d" * 64)
            ids = [uuid.uuid4().hex for _ in range(nblocks)]
            do("alloc_blocks", user="admin", sid=sid, blocks=[{"id": b, "dns": ["d1", "d2", "d3"]} for b in ids])
            do("commit_write", user="admin", sid=sid, size=1000 * nblocks, layout=[{"id": b, "len": 1000, "sha": "a" * 64, "reps": ["d1", "d2", "d3"]} for b in ids])
        dt = time.perf_counter() - t
        fs.dedup.clear()  # caché acotada de idempotencia: no es metadato
        mem = tracemalloc.get_traced_memory()[0] - base
        tracemalloc.stop()
        return mem / nfiles, 3 * nfiles / dt

    a, rate = build(3000, 1)
    b, _ = build(3000, 11)
    per_block = (b - a) / 10
    return {"bytes_per_file_1block": a, "bytes_per_block": per_block, "bytes_per_file_base": a - per_block, "fsm_cmds_per_s": rate}


EXPS = {
    "micro": micro, "meta_mem": exp_meta_mem, "scaling": exp_scaling, "scaling_bw": exp_scaling_bw, "blocksize": exp_blocksize, "workers": exp_workers,
    "metadata": exp_metadata, "clients": exp_clients, "failover": exp_failover, "availability": exp_availability,
}


def main() -> None:
    want = sys.argv[1:] or ["all"]
    names = list(EXPS) if "all" in want else want
    env = {"python": platform.python_version(), "machine": platform.machine(), "system": platform.platform(), "cpus": os.cpu_count()}
    for name in names:
        print(f"== {name}", flush=True)
        t = time.time()
        r = EXPS[name]()
        (RESULTS / f"{name}.json").write_text(json.dumps({"env": env, "result": r, "elapsed_s": time.time() - t}, indent=1, default=str))
    print("listo ->", RESULTS)


if __name__ == "__main__":
    main()
