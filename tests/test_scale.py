"""Escalabilidad horizontal: añadir DataNodes en caliente + snapshots Raft sobre la red real."""

import os
import shutil

import httpx
from conftest import admin_client, eventually, make_cluster, sha

from dfsha.pki import client_context

MiB = 1024 * 1024


def blocks_per_dn(c) -> dict[str, int]:
    return {p.name: len(list(c.base.glob(f"data/{p.name}/blocks/*/*.blk"))) for p in c.procs.values() if p.kind == "data"}


def test_adding_datanode_triggers_rebalancing():
    c = make_cluster(n_data=3, repl=2, zones=1, block_size="1MiB", control_args=["--balance-interval", "2", "--gc-grace", "1"])
    try:
        cl = admin_client(c)
        data = os.urandom(30 * MiB)
        cl.send(data, "/home/admin/grande")
        eventually(lambda: sum(blocks_per_dn(c).values()) >= 60, msg="sin 2 réplicas por bloque")
        before = blocks_per_dn(c)
        assert all(v > 15 for v in before.values()), before
        new = c.add_data_node()  # escalado horizontal: nodo vacío
        eventually(lambda: sum(1 for d in cl.datanodes() if d["alive"]) == 4, timeout=30)
        eventually(lambda: blocks_per_dn(c)[new] >= 10, timeout=120, msg=f"el nodo nuevo no recibió datos: {blocks_per_dn(c)}")
        eventually(lambda: (cl.fsck()["under_replicated"] == 0 and cl.fsck()["over_replicated"] == 0), timeout=60)
        # Las réplicas sobrantes se borran del disco tras `gc_grace` + un ciclo de GC: el metadato ya está
        # sano (fsck) pero los archivos tardan unos segundos en desaparecer => se espera la convergencia física.
        eventually(lambda: sum(blocks_per_dn(c).values()) == 60, timeout=60, msg=f"réplicas sobrantes sin recolectar: {blocks_per_dn(c)}")
        after = blocks_per_dn(c)
        assert sum(after.values()) == 60, "el balanceo no debe cambiar la redundancia total"
        assert max(after.values()) - min(after.values()) <= 12, after
        assert sha(cl.receive("/home/admin/grande")) == sha(data)
        assert cl.cluster()["maintenance"]["balanced"] > 0
    finally:
        c.down()
        shutil.rmtree(c.base, ignore_errors=True)


def test_lagging_follower_catches_up_via_install_snapshot():
    c = make_cluster(control_args=["--snapshot-threshold", "20"])
    try:
        cl = admin_client(c)
        leader = c.leader()
        follower = next(n for n in ("c1", "c2", "c3") if n != leader)
        c.kill(follower)
        for i in range(80):  # 80 entradas Raft => varios snapshots/compactaciones en el líder
            cl.mkdir(f"/home/admin/d{i}")
        li = int(leader[1:])
        assert c.node_info(li)["snapshot_index"] > 0, "el líder debió compactar su log"
        c.start(follower)
        fi = int(follower[1:])
        eventually(lambda: (c.node_info(fi) or {}).get("applied", 0) >= c.node_info(li)["applied"] - 1, timeout=40, msg="no alcanzó al líder")
        assert c.node_info(fi)["snapshot_index"] > 0, "debió recibir InstallSnapshot, no el log completo"
        # el seguidor puesto al día sirve lecturas relajadas con los 80 directorios
        only = admin_client(c)
        only.endpoints = [c.control_urls(fi)[0]]
        assert len(only.ls("/home/admin", stale=True)) == 80
    finally:
        c.down()
        shutil.rmtree(c.base, ignore_errors=True)


def test_oversized_json_body_is_rejected(cluster):
    http = httpx.Client(verify=client_context(cluster.ca))
    r = http.post(cluster.endpoints[0] + "/v1/auth/login", content=b"x" * (33 * MiB), headers={"content-type": "application/json"})
    assert r.status_code == 413
