"""Pruebas de caos / alta disponibilidad: cada una levanta su propio cluster y lo destruye a golpes."""

import os
import shutil
import threading
import time
import uuid

import httpx
import pytest
from conftest import admin_client, eventually, sha

from dfsha.client.sdk import Client
from dfsha.common import errors as E
from dfsha.pki import client_context

BS = 4 * 1024 * 1024


def healthy(cl: Client, nblocks: int | None = None, repl: int = 3) -> bool:
    """fsck sano y, además, todos los bloques con `repl` réplicas VIVAS."""
    f = cl.fsck()
    if f["under_replicated"] or f["lost"] or f["over_replicated"]:
        return False
    return nblocks is None or f["blocks"] >= nblocks


def replica_map(cl: Client, path: str) -> list[list[str]]:
    meta = cl._locations_all(cl.abspath(path))
    alive = {d["id"] for d in cl.datanodes() if d["alive"]}
    return [[loc["id"] for loc in b["locs"] if loc["id"] in alive] for b in meta["blocks"]]


def put_big(cl: Client, nblocks: int, path: str | None = None) -> tuple[str, bytes]:
    data = os.urandom(nblocks * BS - 11)
    path = path or f"/home/admin/chaos_{uuid.uuid4().hex[:6]}"
    cl.send(data, path)
    return path, data


def test_read_survives_datanode_sigkill(fresh_cluster):
    c = fresh_cluster
    cl = admin_client(c)
    path, data = put_big(cl, 12)
    eventually(lambda: all(len(r) == 3 for r in replica_map(cl, path)), msg="sin 3 réplicas")
    c.kill("dn2")  # SIGKILL: sin aviso
    t0 = time.time()
    assert sha(cl.receive(path)) == sha(data)  # lectura inmediata, antes de que el cluster note la caída
    assert time.time() - t0 < 15
    assert cl.stats["replica_fallbacks"] > 0, "debió haber conmutación transparente a otra réplica"


def test_write_survives_datanode_failure_and_rereplicates(fresh_cluster):
    c = fresh_cluster
    cl = admin_client(c)
    c.kill("dn3")
    path, data = put_big(cl, 10)  # el líder aún cree vivo a dn3: la tubería lo salta / reubica
    assert sha(cl.receive(path)) == sha(data)
    # el cluster declara muerto a dn3 y restablece 3 réplicas sobre los 3 nodos vivos
    eventually(lambda: healthy(cl) and all(len(r) == 3 for r in replica_map(cl, path)), timeout=60, msg="no volvió a 3 réplicas")
    assert "dn3" not in {d for r in replica_map(cl, path) for d in r}
    assert cl.cluster()["maintenance"]["replicated"] > 0


def test_transient_failure_does_not_trigger_data_movement(fresh_cluster):
    c = fresh_cluster
    cl = admin_client(c)
    path, data = put_big(cl, 6)
    eventually(lambda: all(len(r) == 3 for r in replica_map(cl, path)))
    before = cl.cluster()["maintenance"]["replicated"]
    c.kill("dn1")
    time.sleep(1.0)
    c.start("dn1")  # vuelve antes del periodo de gracia
    time.sleep(10)
    assert healthy(cl)
    assert cl.cluster()["maintenance"]["replicated"] == before, "un fallo breve no debe provocar copias"
    assert sha(cl.receive(path)) == sha(data)


def test_leader_failover_service_continues(fresh_cluster):
    c = fresh_cluster
    cl = admin_client(c)
    path, data = put_big(cl, 4)
    old = c.leader()
    c.kill(old)
    t0 = time.time()
    cl.mkdir("/home/admin/after_failover")  # operación de escritura: debe esperar la nueva elección y tener éxito
    failover = time.time() - t0
    assert failover < 15, f"conmutación por error demasiado lenta: {failover:.1f}s"
    assert c.leader() not in (None, old)
    assert sha(cl.receive(path)) == sha(data)  # los datos anteriores siguen ahí
    p2, d2 = put_big(cl, 3)
    assert sha(cl.receive(p2)) == sha(d2)
    # el líder caído vuelve: se pone al día como seguidor
    c.start(old)
    eventually(lambda: (c.node_info(int(old[1:])) or {}).get("role") == "follower", timeout=30, msg="no se reincorporó")
    n_new = c.node_info(int(c.leader()[1:]))
    eventually(lambda: (c.node_info(int(old[1:])) or {}).get("applied", 0) >= n_new["applied"], timeout=30, msg="no alcanzó el log")
    print(f"\n[failover] {failover:.2f}s")


def test_metadata_survives_full_control_plane_restart():
    import tempfile
    from pathlib import Path

    from dfsha.cluster import LocalCluster

    base = Path(tempfile.mkdtemp(prefix="dfsha-test-"))
    c = LocalCluster(base, n_control=3, n_data=4, fsync=True)
    c.prepare()
    c.start_all()
    try:
        c.wait_ready(60)
        cl = admin_client(c)
        cl.useradd("persistente", "password-123")
        path, data = put_big(cl, 5)
        cl.mkdir("/home/admin/d/e/f", parents=True)
        for n in ("c1", "c2", "c3"):
            c.kill(n)  # caída total del plano de control (SIGKILL)
        for n in ("c1", "c2", "c3"):
            c.start(n)
        c.wait_ready(60)
        cl2 = Client(c.endpoints, c.ca, credentials=("admin", c.admin_password))
        cl2.login("admin", c.admin_password)
        assert cl2.exists("/home/admin/d/e/f")
        assert sha(cl2.receive(path)) == sha(data)
        u = Client(c.endpoints, c.ca)
        u.login("persistente", "password-123")
    finally:
        c.down()
        shutil.rmtree(base, ignore_errors=True)


def test_corrupted_replica_is_skipped_quarantined_and_repaired(fresh_cluster):
    c = fresh_cluster
    cl = admin_client(c)
    path, data = put_big(cl, 6)
    eventually(lambda: all(len(r) == 3 for r in replica_map(cl, path)))
    s = cl.abspath(path)
    meta = cl._locations_all(s)
    blk = meta["blocks"][0]
    victim = blk["locs"][0]["id"]
    f = next(c.base.glob(f"data/{victim}/blocks/*/{blk['block_id']}.blk"))
    raw = bytearray(f.read_bytes())
    raw[40] ^= 0xFF  # bit rot en el ciphertext
    f.write_bytes(bytes(raw))
    blk["locs"].sort(key=lambda loc: loc["id"] != victim)  # la réplica dañada primero
    assert sha(cl._fetch_block(s, blk, 0)) == blk["sha"]  # igual se obtiene el dato correcto
    assert cl.stats["replica_fallbacks"] > 0
    # el DataNode puso en cuarentena el blob y avisó: el cluster re-replica
    eventually(lambda: not f.exists() and (c.base / "data" / victim / "corrupt" / f.name).exists(), msg="sin cuarentena")
    eventually(lambda: healthy(cl) and all(len(r) == 3 for r in replica_map(cl, path)), timeout=60, msg="no se reparó")
    assert sha(cl.receive(path)) == sha(data)


def test_scrubber_finds_silent_corruption(fresh_cluster):
    c = fresh_cluster
    cl = admin_client(c)
    path, data = put_big(cl, 6)
    eventually(lambda: all(len(r) == 3 for r in replica_map(cl, path)))
    victim = "dn1"
    files = list(c.base.glob(f"data/{victim}/blocks/*/*.blk"))
    assert files
    raw = bytearray(files[0].read_bytes())
    raw[len(raw) // 2] ^= 0x01
    files[0].write_bytes(bytes(raw))
    # pase del scrubber (endpoint interno con certificado de rol admin)
    j = c.data_urls(1)[1]
    crt, key = c.pki / "admin-tool.crt", c.pki / "admin-tool.key"
    r = httpx.post(f"{j}/internal/scrub", verify=client_context(c.ca, str(crt), str(key)), timeout=30)
    assert r.status_code == 200 and len(r.json()["corrupt"]) == 1
    eventually(lambda: healthy(cl) and all(len(x) == 3 for x in replica_map(cl, path)), timeout=60)
    assert sha(cl.receive(path)) == sha(data)


def test_datanode_with_wiped_disk_is_detected_and_data_restored(fresh_cluster):
    c = fresh_cluster
    cl = admin_client(c)
    path, data = put_big(cl, 8)
    eventually(lambda: all(len(r) == 3 for r in replica_map(cl, path)))
    c.kill("dn4")
    shutil.rmtree(c.base / "data" / "dn4" / "blocks")  # el disco "se pierde"
    c.start("dn4")
    eventually(lambda: healthy(cl) and all(len(r) == 3 for r in replica_map(cl, path)), timeout=90, msg="no se restauró tras reporte de bloques")
    assert sha(cl.receive(path)) == sha(data)


def test_quorum_loss_blocks_writes_then_recovers(fresh_cluster):
    c = fresh_cluster
    cl = admin_client(c, retry_window=6)
    path, data = put_big(cl, 2)
    leader = c.leader()
    others = [n for n in ("c1", "c2", "c3") if n != leader]
    c.kill(others[0])
    c.kill(others[1])  # 1/3: sin mayoría
    time.sleep(2.5)
    with pytest.raises(E.Unavailable):
        cl.mkdir("/home/admin/sin_quorum")  # CP: se rechaza en vez de arriesgar inconsistencia
    with pytest.raises(E.Unavailable):
        cl.stat(path)  # tampoco hay lecturas linealizables sin quórum
    c.start(others[0])
    c.start(others[1])
    c.wait_ready(60)
    cl2 = admin_client(c)
    cl2.mkdir("/home/admin/con_quorum")
    assert sha(cl2.receive(path)) == sha(data)


def test_write_quorum_unavailable_when_too_many_datanodes_die(fresh_cluster):
    c = fresh_cluster
    cl = admin_client(c, retry_window=8)
    for n in ("dn1", "dn2", "dn3"):
        c.kill(n)
    eventually(lambda: sum(1 for d in cl.datanodes() if d["alive"]) == 1, timeout=30)
    with pytest.raises((E.NoSpace, E.Unavailable)):
        cl.send(b"x" * 1000, "/home/admin/imposible")
    with pytest.raises(E.NotFound):
        cl.stat("/home/admin/imposible")  # nada quedó a medias
    c.start("dn1")
    c.start("dn2")
    eventually(lambda: sum(1 for d in cl.datanodes() if d["alive"]) >= 3, timeout=30)
    cl.send(b"ya si", "/home/admin/posible")


def test_decommission_moves_blocks_off_node(fresh_cluster):
    c = fresh_cluster
    cl = admin_client(c)
    path, data = put_big(cl, 8)
    eventually(lambda: all(len(r) == 3 for r in replica_map(cl, path)))
    cl.set_datanode_state("dn2", "decommissioning")

    def drained() -> bool:
        meta = cl._locations_all(cl.abspath(path))
        # 3 réplicas en nodos ACTIVOS (dn2 ya no cuenta)
        return all(len({loc["id"] for loc in b["locs"]} - {"dn2"}) >= 3 for b in meta["blocks"])

    # con 4 DataNodes y dn2 fuera, quedan exactamente 3 activos: debe haber 3 réplicas sin dn2
    eventually(drained, timeout=60, msg="no se vació el nodo")
    cl.set_datanode_state("dn2", "removed")
    assert sha(cl.receive(path)) == sha(data)
    assert {d["id"] for d in cl.datanodes()} == {"dn1", "dn3", "dn4"}


def test_deleted_files_blocks_are_garbage_collected(fresh_cluster):
    c = fresh_cluster
    cl = admin_client(c)
    path, _ = put_big(cl, 6)
    eventually(lambda: len(list(c.base.glob("data/dn*/blocks/*/*.blk"))) >= 18)
    cl.rm(path)
    eventually(lambda: len(list(c.base.glob("data/dn*/blocks/*/*.blk"))) == 0, timeout=40, msg="bloques huérfanos sin recolectar")
    assert cl.fsck()["blocks"] == 0


def test_abandoned_upload_is_garbage_collected(fresh_cluster):
    """Un cliente que sube bloques y nunca confirma no deja basura ni un archivo visible."""
    c = fresh_cluster
    cl = admin_client(c, retry_window=5)
    sess = cl._call("POST", "/v1/files/begin", json={"path": "/home/admin/abandonado"}, mutating=True)
    for a in cl._alloc(sess["sid"], 2):
        cl._upload_block(sess["sid"], a, os.urandom(1000), sess["min_repl"])
    assert any(c.base.glob("data/dn*/blocks/*/*.blk"))
    with pytest.raises(E.NotFound):
        cl.stat("/home/admin/abandonado")  # nunca fue visible
    cl._call("POST", f"/v1/files/{sess['sid']}/abort")  # (el lease vencería igual; ver test_fsm)
    eventually(lambda: not any(c.base.glob("data/dn*/blocks/*/*.blk")), timeout=40)


def test_leader_killed_during_large_upload(fresh_cluster):
    c = fresh_cluster
    cl = admin_client(c, workers=4)
    data = os.urandom(60 * BS)
    path = "/home/admin/big_upload"
    result: dict = {}

    def up() -> None:
        try:
            result["r"] = cl.send(data, path)
        except Exception as e:  # noqa: BLE001
            result["err"] = repr(e)

    t = threading.Thread(target=up)
    t.start()
    time.sleep(0.15)
    c.kill(c.leader())
    t.join(120)
    assert "err" not in result, result
    assert sha(cl.receive(path)) == sha(data)


def test_datanode_killed_during_large_upload_and_download(fresh_cluster):
    c = fresh_cluster
    cl = admin_client(c, workers=4)
    data = os.urandom(60 * BS)
    path = "/home/admin/big2"
    result: dict = {}

    def up() -> None:
        try:
            result["r"] = cl.send(data, path)
        except Exception as e:  # noqa: BLE001
            result["err"] = repr(e)

    t = threading.Thread(target=up)
    t.start()
    time.sleep(0.15)
    c.kill("dn1")
    t.join(120)
    assert "err" not in result, result
    eventually(lambda: all(len(r) >= 2 for r in replica_map(cl, path)), timeout=30)
    assert sha(cl.receive(path)) == sha(data)
    # y ahora se cae otro mientras se descarga
    out: dict = {}

    def down() -> None:
        try:
            out["d"] = cl.receive(path)
        except Exception as e:  # noqa: BLE001
            out["err"] = repr(e)

    t2 = threading.Thread(target=down)
    t2.start()
    time.sleep(0.05)
    c.kill("dn2")
    t2.join(120)
    assert "err" not in out, out
    assert sha(out["d"]) == sha(data)


def test_repair_waits_for_the_grace_period_after_a_datanode_is_declared_dead(fresh_cluster):
    """Regresión: durante hasta 1 s tras morir el nodo la gracia (repl_grace=3 s) se saltaba y se copiaban datos al instante."""
    c = fresh_cluster
    cl = admin_client(c)
    path, _ = put_big(cl, 6)
    eventually(lambda: all(len(r) == 3 for r in replica_map(cl, path)))
    victim = cl._locations_all(cl.abspath(path))["blocks"][0]["locs"][0]["id"]
    base = cl.cluster()["maintenance"]["replicated"]
    c.kill(victim)
    t_dead = t_copy = None
    t0 = time.time()
    while time.time() - t0 < 60 and t_copy is None:
        if t_dead is None and not next(d for d in cl.datanodes() if d["id"] == victim)["alive"]:
            t_dead = time.time()
        if cl.cluster()["maintenance"]["replicated"] > base:
            t_copy = time.time()
        time.sleep(0.15)
    assert t_dead and t_copy, "no hubo re-replicación"
    assert t_copy - t_dead >= 2.2, f"la re-replicación empezó {t_copy - t_dead:.2f}s después de declarar muerto el nodo (gracia = 3 s)"
