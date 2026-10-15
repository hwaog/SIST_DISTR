"""Aislamiento de claves en el docker-compose (mínimo privilegio) — sin necesidad de Docker."""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]
CONTROLS = sorted(k for k in SERVICES if re.fullmatch(r"c\d+", k))
DATAS = sorted(k for k in SERVICES if re.fullmatch(r"dn\d+", k))


def mounts(service: str) -> dict[str, tuple[str, str]]:
    """volumen -> (ruta en el contenedor, modo) de los volúmenes NOMBRADOS (los binds './x' se ignoran)."""
    out = {}
    for v in SERVICES[service].get("volumes", []):
        parts = v.split(":")
        if parts[0].startswith("."):
            continue
        out[parts[0]] = (parts[1], parts[2] if len(parts) > 2 else "rw")
    return out


def test_compose_matches_generator():
    spec = importlib.util.spec_from_file_location("gen_compose", ROOT / "docker" / "gen_compose.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (ROOT / "docker-compose.yml").read_text() == mod.build(len(CONTROLS), len(DATAS)), (
        "docker-compose.yml fue editado a mano: edite docker/gen_compose.py y regenere"
    )


def test_each_node_mounts_only_its_own_keys():
    for n in CONTROLS + DATAS:
        m = mounts(n)
        pki_vols = [v for v in m if v.startswith("pki-")]
        assert pki_vols == [f"pki-{n}"], f"{n} monta claves ajenas: {pki_vols}"
        assert m[f"pki-{n}"] == ("/pki", "ro")
    assert [v for v in mounts("client") if v.startswith(("pki-", "sec-", "state"))] == ["pki-client"]


def test_master_secret_only_reaches_controlnodes():
    for n in DATAS + ["client"]:
        assert not [v for v in mounts(n) if v.startswith("sec-") or v == "state"], f"{n} no debe recibir cluster.key"
    for n in CONTROLS:
        sec = [v for v in mounts(n) if v.startswith("sec-")]
        assert sec == [f"sec-{n}"] and mounts(n)[f"sec-{n}"] == ("/secrets", "ro")


def test_ca_private_key_volume_is_exclusive_to_pki_init():
    holders = [s for s in SERVICES if "state" in mounts(s)]
    assert holders == ["pki-init"]


def test_every_secret_volume_has_exactly_two_holders_init_and_owner():
    for vol in [v for v in COMPOSE["volumes"] if v.startswith(("pki-", "sec-"))]:
        holders = sorted(s for s in SERVICES if vol in mounts(s))
        owner = vol.split("-", 1)[1]
        assert holders == sorted(["pki-init", owner]), f"{vol}: {holders}"


def test_nodes_are_hardened_and_published_ports_are_public_only():
    for n in CONTROLS + DATAS:
        s = SERVICES[n]
        assert s["cap_drop"] == ["ALL"] and "no-new-privileges:true" in s["security_opt"]
        for p in s["ports"]:  # sólo puertos públicos: el interno (mTLS) jamás se publica
            port = int(p.split(":")[0])
            assert port < 9000 and not 7500 <= port < 7600, f"{n}: puerto interno publicado {port}"


def test_init_pki_script_distributes_least_privilege(tmp_path):
    env = {
        **os.environ,
        "CONTROLS": "c1 c2 c3", "DATAS": "dn1 dn2", "ADMIN_PASSWORD": "x-pass-1234",
        "STATE_DIR": str(tmp_path / "state"), "OUT_ROOT": str(tmp_path / "out"),
        "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
    }  # fmt: skip

    def run():
        subprocess.run(["sh", str(ROOT / "docker" / "init-pki.sh")], check=True, env=env, capture_output=True)

    run()
    out = tmp_path / "out"
    for n in ("c1", "c2", "c3", "dn1", "dn2"):
        assert sorted(p.name for p in (out / "pki" / n).iterdir()) == sorted(["ca.pem", f"{n}.crt", f"{n}.key"])
    assert [p.name for p in (out / "pki" / "client").iterdir()] == ["ca.pem"]
    for n in ("c1", "c2", "c3"):
        assert sorted(p.name for p in (out / "sec" / n).iterdir()) == ["admin.pass", "cluster.key"]
    assert not (out / "sec" / "dn1").exists()
    assert not list(out.rglob("ca.key")), "ca.key jamás debe salir del volumen de estado"
    assert (tmp_path / "state" / "ca" / "ca.key").exists()
    # el secreto maestro es el mismo en los 3 ControlNodes; cada clave privada es distinta
    keys = {(out / "sec" / n / "cluster.key").read_text() for n in ("c1", "c2", "c3")}
    assert len(keys) == 1
    assert len({(out / "pki" / n / f"{n}.key").read_text() for n in ("c1", "dn1", "dn2")}) == 3
    # roles correctos en los certificados (OU) y cadena válida contra la CA
    ca = x509.load_pem_x509_certificate((out / "pki" / "dn1" / "ca.pem").read_bytes())
    for n, role in (("c1", "controlnode"), ("dn1", "datanode")):
        crt = x509.load_pem_x509_certificate((out / "pki" / n / f"{n}.crt").read_bytes())
        assert crt.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)[0].value == role
        crt.verify_directly_issued_by(ca)
    # idempotencia: reiniciar el stack NO regenera la CA ni el secreto (si no, se perderían los datos)
    ca_before, master_before = (out / "pki" / "dn1" / "ca.pem").read_bytes(), (out / "sec" / "c1" / "cluster.key").read_text()
    run()
    assert (out / "pki" / "dn1" / "ca.pem").read_bytes() == ca_before
    assert (out / "sec" / "c1" / "cluster.key").read_text() == master_before


@pytest.mark.parametrize("name", ["c1", "dn1"])
def test_healthcheck_script_is_valid_python(name):
    compile((ROOT / "docker" / "healthcheck.py").read_text(), "healthcheck.py", "exec")
