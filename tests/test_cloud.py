"""El validador y los benchmarks de nube, probados contra un cluster local que hace de 'remoto'."""

import json
import os
import stat
import textwrap
from pathlib import Path

import pytest
from conftest import admin_client

from dfsha.cloud import (
    Host,
    LocalKiller,
    SSHKiller,
    Validator,
    _measure_metadata,
    bench_scaling,
    read_inventory,
)


def test_inventory_parsing_and_port_convention(tmp_path):
    inv = tmp_path / "inventory.txt"
    inv.write_text(textwrap.dedent("""\
        # rol nombre ip dns zona
        control c1 10.0.1.11 ec2-1.example.com us-east-1a
        control c2 10.0.2.12 ec2-2.example.com us-east-1b

        data dn1 10.0.1.21 ec2-3.example.com us-east-1a
        data dn2 10.0.2.22 ec2-4.example.com us-east-1b
        client cl1 10.0.1.99 ec2-5.example.com us-east-1a
    """))
    hosts = read_inventory(inv)
    by = {h.name: h for h in hosts}
    assert (by["c2"].api_port, by["c2"].internal_port) == (8442, 9442)
    assert (by["dn2"].api_port, by["dn2"].internal_port) == (7442, 7542)
    assert by["cl1"].api_port == 0 and by["dn1"].az == "us-east-1a"


def test_sshkiller_builds_the_right_commands(tmp_path, monkeypatch):
    """El 'ssh' falso registra los argumentos: se comprueba host, usuario, clave y comando docker."""
    log = tmp_path / "ssh.log"
    fake = tmp_path / "ssh"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\ncase "$*" in *BLOBS*) printf "FILE:/x\\nMAGIC:44465342\\nBLOBS:12\\nHITS:0\\n";; esac\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    k = SSHKiller([Host("data", "dn2", "10.0.0.2", "ec2-9.example.com", "us-east-1b")], key="/k/labsuser.pem")
    k.stop("dn2")
    k.stop("dn2", graceful=True)
    k.start("dn2")
    r = k.inspect_blocks("dn2", b"MARCADOR")
    cmds = log.read_text().splitlines()
    assert all("-i /k/labsuser.pem" in c and "ec2-user@ec2-9.example.com" in c and "BatchMode=yes" in c for c in cmds)
    assert "sudo docker kill dfsha-node" in cmds[0] and "sudo docker stop dfsha-node" in cmds[1] and "sudo docker start dfsha-node" in cmds[2]
    assert r == {"blobs": 12, "magic_ok": True, "plaintext_hits": 0, "ssh_ok": True}


def test_full_validation_battery_against_a_local_cluster(fresh_cluster, tmp_path):
    """Los 10 pasos (incluidos failover del líder y caída de un DataNode) deben pasar sobre un cluster real."""
    c = fresh_cluster
    cl = admin_client(c, retry_window=45)
    lines: list[str] = []
    v = Validator(cl, c.endpoints, c.ca, hosts=[], killer=LocalKiller(c), size_mib=12, out=tmp_path / "ev", wait_repair_s=120, log=lines.append)
    res = v.run()
    by = {s["name"].split(". ", 1)[1]: s for s in res["pasos"]}
    failed = {k: x["detail"] for k, x in by.items() if x["status"] == "FAIL"}
    assert not failed, failed
    assert res["ok"]
    assert by["Failover del líder"]["status"] == "PASS" and by["Failover del líder"]["data"]["failover_s"] < 20
    assert by["Caída de un DataNode"]["data"]["repaired_s"] > 0
    assert by["Cifrado en reposo en el disco"]["data"]["plaintext_hits"] == 0
    assert by["Mapa de bloques (réplicas y zonas)"]["status"] == "PASS"
    ev = json.loads((tmp_path / "ev" / "resultados.json").read_text())
    assert ev["ok"] and len(ev["pasos"]) == 10
    assert "TODOS LOS PASOS SUPERADOS" in (tmp_path / "ev" / "resumen.md").read_text()
    assert cl.stat("/home/admin")["type"] == "dir"
    assert not [e for e in cl.ls("/home/admin") if e["name"].startswith("_")], "la limpieza debe borrar los archivos de prueba"


def test_validation_without_chaos_skips_destructive_steps(cluster, tmp_path):
    cl = admin_client(cluster)
    res = Validator(cl, cluster.endpoints, cluster.ca, hosts=[], killer=None, size_mib=6, out=tmp_path / "ev", log=lambda _m: None).run()
    st = {s["name"].split(". ", 1)[1]: s["status"] for s in res["pasos"]}
    assert st["Failover del líder"] == st["Caída de un DataNode"] == st["Cifrado en reposo en el disco"] == "SKIP"
    assert res["ok"], res["pasos"]


def test_bench_scaling_restores_datanode_states_and_measures(cluster):
    cl = admin_client(cluster)
    r = bench_scaling(cl, [1, 2], size_mib=4, reps=1, repl_list=[1])
    assert set(r["repl1"]) == {"1", "2"} and all(v["upload_MiBps"]["mean"] > 0 for v in r["repl1"].values())
    assert {d["state"] for d in cl.datanodes()} == {"active"}, "el benchmark debe dejar todos los DataNodes activos"


def test_metadata_bench_measures_and_cleans_up(cluster):
    cl = admin_client(cluster)
    r = _measure_metadata(lambda: admin_client(cluster), 2, 0.8)
    assert r["stat_linearizable"]["ops"] > 5 and r["mkdir_raft_write"]["ops"] > 5
    assert not cl.exists("/home/admin/_bench_meta")


@pytest.mark.parametrize("bad", ["", "# solo comentarios\n", "data dn1 10.0.0.1 h z\n"])
def test_inventory_without_controlnodes_is_rejected(tmp_path, bad):
    import click

    f = Path(tmp_path / "inv.txt")
    f.write_text(bad)
    with pytest.raises(click.ClickException):
        read_inventory(f)
