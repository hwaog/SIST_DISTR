"""Los scripts de deploy/aws, ejercitados con un 'aws', 'ssh', 'rsync' y 'curl' FALSOS que registran las llamadas.

No sustituyen a una ejecución real en AWS, pero detectan errores de lógica/bash (variables sin definir, orden de
comandos, puertos, qué recibe cada rol, persistencia de la CA...) antes de gastar créditos del laboratorio.
"""

import json
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AWS_DIR = ROOT / "deploy" / "aws"

FAKE_AWS = r'''#!/usr/bin/env python3
import json, os, sys
st_path = os.environ["FAKE_AWS_STATE"]
st = json.load(open(st_path)) if os.path.exists(st_path) else {"instances": [], "calls": [], "sg": None, "n": 0}
a = sys.argv[1:]
st["calls"].append(" ".join(a))
st.setdefault("regions", []).append(os.environ.get("AWS_DEFAULT_REGION"))
def save():
    json.dump(st, open(st_path, "w"))
def opt(name, default=None):
    return a[a.index(name) + 1] if name in a else default
svc, cmd = a[0], a[1]
out = ""
if svc == "sts":
    out = "arn:aws:sts::123:assumed-role/LabRole/user"
elif cmd == "describe-images":
    out = "ami-0123456789"
elif cmd == "describe-vpcs":
    out = "vpc-1"
elif cmd == "describe-subnets":
    out = "subnet-a\tsubnet-b\tsubnet-c"
elif cmd == "describe-security-groups":
    out = st["sg"] or "None"
elif cmd == "create-security-group":
    st["sg"] = "sg-1"; out = "sg-1"
elif cmd == "run-instances":
    tags = opt("--tag-specifications"); role = tags.split("dfsha-role,Value=")[1].split("}")[0]; name = tags.split("dfsha-name,Value=")[1].split("}")[0]
    st["n"] += 1; n = st["n"]; sub = opt("--subnet-id"); az = {"subnet-a": "us-east-1a", "subnet-b": "us-east-1b", "subnet-c": "us-east-1c"}[sub]
    st["instances"].append({"id": f"i-{n:04d}", "role": role, "name": name, "priv": f"10.0.{n}.{n}", "pub": f"ec2-{n}.compute.example.com", "az": az, "type": opt("--instance-type"), "state": "running", "disk": opt("--block-device-mappings")})
    out = f"i-{n:04d}"
elif cmd == "describe-instances":
    q = opt("--query", "")
    flt = " ".join(a)
    if "InstanceId" in q and "PrivateIpAddress" not in q:
        want = "stopped" if "Values=stopped" in flt else "running"
        if "pending,running" in flt:
            out = "\t".join(i["id"] for i in st["instances"] if i["state"] in ("running", "pending", "stopped"))
        else:
            out = "\t".join(i["id"] for i in st["instances"] if i["state"] == want)
    elif "length(" in q:
        out = str(sum(1 for i in st["instances"] if i["state"] == "running"))
    else:
        out = "\n".join("\t".join([i["role"], i["name"], i["priv"], i["pub"], i["az"]]) for i in st["instances"] if i["state"] == "running")
elif cmd == "start-instances":
    for i in st["instances"]:
        if i["id"] in a: i["state"] = "running"
elif cmd == "terminate-instances":
    st["instances"] = [i for i in st["instances"] if i["id"] not in a]
elif cmd == "delete-security-group":
    st["sg"] = None
elif cmd == "describe-addresses":
    out = ""
elif cmd == "describe-key-pairs":
    out = "{}"
save()
if out: print(out)
'''

FAKE_SSH = '#!/bin/sh\necho "SSH $*" >> "$FAKE_LOG"\nif [ ! -t 0 ]; then cat >> "$FAKE_LOG.stdin" 2>/dev/null; fi\nexit 0\n'
FAKE_RSYNC = '#!/bin/sh\necho "RSYNC $*" >> "$FAKE_LOG"\nexit 0\n'
FAKE_CURL = '#!/bin/sh\necho "CURL $*" >> "$FAKE_LOG"\necho \'{"ok":true,"id":"fake","role":"leader"}\'\nexit 0\n'


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Copia de deploy/aws + comandos falsos en el PATH."""
    work = tmp_path / "repo" / "deploy" / "aws"
    work.parent.mkdir(parents=True)
    shutil.copytree(AWS_DIR, work, ignore=shutil.ignore_patterns("bundles", "client", "state", "evidence", "inventory.txt"))
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, body in (("aws", FAKE_AWS), ("ssh", FAKE_SSH), ("rsync", FAKE_RSYNC), ("curl", FAKE_CURL)):
        f = bin_ / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    key = tmp_path / "labsuser.pem"
    key.write_text("fake")
    key.chmod(0o400)
    env = {
        **os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "FAKE_AWS_STATE": str(tmp_path / "aws.json"), "FAKE_LOG": str(tmp_path / "calls.log"),
        "KEY_FILE": str(key), "AWS_REGION": "us-east-1", "DFSHA_REPO": str(ROOT), "HOME": str(tmp_path),
    }  # fmt: skip

    def run(script, *args, check=True, **extra):
        r = subprocess.run(["bash", str(work / script), *args], cwd=work, env={**env, **extra}, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)
        if check and r.returncode != 0:
            raise AssertionError(f"{script} falló ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        return r

    run.work, run.state, run.log = work, tmp_path / "aws.json", tmp_path / "calls.log"
    return run


def test_launch_builds_inventory_with_zone_spread_and_roles(sandbox):
    r = sandbox("launch.sh", N_CONTROL="3", N_DATA="4", N_CLIENT="1")
    inv = [ln.split() for ln in (sandbox.work / "inventory.txt").read_text().splitlines()]
    assert [x[0] for x in inv].count("control") == 3 and [x[0] for x in inv].count("data") == 4 and [x[0] for x in inv].count("client") == 1
    assert {x[1] for x in inv} == {"c1", "c2", "c3", "dn1", "dn2", "dn3", "dn4", "cl1"}
    assert len({x[4] for x in inv if x[0] == "control"}) == 3, "los 3 ControlNodes deben quedar en 3 zonas distintas"
    assert len({x[4] for x in inv if x[0] == "data"}) >= 2, "los DataNodes deben abarcar ≥2 zonas"
    st = json.loads(sandbox.state.read_text())
    types = {i["name"]: i["type"] for i in st["instances"]}
    assert types["c1"] == "t3.small" and types["cl1"] == "t3.medium"
    calls = "\n".join(st["calls"])
    assert "authorize-security-group-ingress" in calls and "--source-group sg-1" in calls  # puertos internos sólo desde el propio SG
    assert "--port 9441-9449 --source-group" in calls and "--port 8441-8449 --cidr" in calls
    # la VM cliente (dentro del VPC) usa las APIs públicas con su IP privada: deben estar abiertas también al propio SG
    for port in ("22", "8441-8449", "7441-7449"):
        assert f"--port {port} --source-group sg-1" in calls, port
    assert "instance-status-ok" in calls and "Siguiente paso" in r.stdout


def test_launch_refuses_to_duplicate_an_existing_deployment(sandbox):
    sandbox("launch.sh", N_DATA="3", N_CLIENT="0")
    again = sandbox("launch.sh", check=False, N_DATA="3", N_CLIENT="0")
    assert again.returncode != 0 and "ya existen instancias" in again.stderr


def _write_inventory(work: Path, with_client=True):
    lines = ["control c1 10.0.1.11 ec2-1.compute.example.com us-east-1a", "control c2 10.0.2.12 ec2-2.compute.example.com us-east-1b",
             "control c3 10.0.3.13 ec2-3.compute.example.com us-east-1c", "data dn1 10.0.1.21 ec2-4.compute.example.com us-east-1a",
             "data dn2 10.0.2.22 ec2-5.compute.example.com us-east-1b"]  # fmt: skip
    if with_client:
        lines.append("client cl1 10.0.1.99 ec2-6.compute.example.com us-east-1a")
    (work / "inventory.txt").write_text("# rol nombre ip dns zona\n" + "\n".join(lines) + "\n")


def test_gen_bundles_least_privilege_and_persistent_state(sandbox):
    w = sandbox.work
    _write_inventory(w)
    sandbox("gen-bundles.sh", ADMIN_PASSWORD="clave-larga-1234")
    b = w / "bundles"
    assert sorted(p.name for p in (b / "c1").iterdir()) == sorted(["admin.pass", "c1.crt", "c1.key", "ca.pem", "cluster.key", "node.env", "ports"])
    assert sorted(p.name for p in (b / "dn1").iterdir()) == sorted(["ca.pem", "dn1.crt", "dn1.key", "node.env", "ports"]), "un DataNode no recibe cluster.key"
    assert sorted(p.name for p in (b / "cl1").iterdir()) == sorted(["ca.pem", "client.env", "ports"])
    assert not list(b.rglob("ca.key")), "la clave de la CA no sale de state/"
    env = dict(ln.split("=", 1) for ln in (b / "dn2" / "node.env").read_text().splitlines())
    assert env["DFSHA_ZONE"] == "us-east-1b" and env["DFSHA_ADVERTISE_PUBLIC"] == "https://ec2-5.compute.example.com:7442"
    assert env["DFSHA_ADVERTISE_INTERNAL"] == "https://10.0.2.22:7542" and env["DFSHA_CONTROLS"].count("https://10.0.") == 3
    assert (b / "dn2" / "ports").read_text().split() == ["7442", "7542", "dfsha-data"]
    peers = dict(ln.split("=", 1) for ln in (b / "c2" / "node.env").read_text().splitlines())["DFSHA_PEERS"]
    assert peers.count(";") == 2 and "c3=https://ec2-3.compute.example.com:8443,https://10.0.3.13:9443" in peers
    # SAN del certificado = IP privada + DNS público
    from cryptography import x509

    crt = x509.load_pem_x509_certificate((b / "dn1" / "dn1.crt").read_bytes())
    san = crt.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    names = {str(x.value) for x in san}
    assert {"10.0.1.21", "ec2-4.compute.example.com", "dn1"} <= names
    # persistencia: reejecutar (p. ej. tras reiniciar el laboratorio) conserva la CA y el secreto maestro => los datos siguen legibles
    ca1, mk1 = (w / "state" / "pki" / "ca.pem").read_bytes(), (w / "state" / "cluster.key").read_text()
    (w / "inventory.txt").write_text((w / "inventory.txt").read_text().replace("ec2-4.compute.example.com", "ec2-NUEVO.compute.example.com"))
    sandbox("gen-bundles.sh")  # sin ADMIN_PASSWORD: ya existe state/admin.pass
    assert (w / "state" / "pki" / "ca.pem").read_bytes() == ca1 and (w / "state" / "cluster.key").read_text() == mk1
    crt2 = x509.load_pem_x509_certificate((b / "dn1" / "dn1.crt").read_bytes())
    assert "ec2-NUEVO.compute.example.com" in {str(x.value) for x in crt2.extensions.get_extension_for_class(x509.SubjectAlternativeName).value}
    ep_lines = [ln for ln in (w / "client" / "env.sh").read_text().splitlines() if ln.startswith("export DFSHA_ENDPOINTS")]
    assert len(ep_lines) == 1 and ep_lines[0].count("https://ec2-") == 3  # una línea con los 3 ControlNodes
    assert "DFSHA_PASSWORD" not in (w / "client" / "env.sh").read_text().replace("# export DFSHA_PASSWORD", "")


def test_gen_bundles_requires_admin_password_on_first_run(sandbox):
    _write_inventory(sandbox.work)
    r = sandbox("gen-bundles.sh", check=False)
    assert r.returncode != 0 and "ADMIN_PASSWORD" in r.stderr


def test_deploy_runs_the_right_container_per_role(sandbox):
    w = sandbox.work
    _write_inventory(w)
    sandbox("gen-bundles.sh", ADMIN_PASSWORD="clave-larga-1234")
    r = sandbox("deploy.sh")
    log = sandbox.log.read_text()
    stdin = Path(str(sandbox.log) + ".stdin").read_text()
    assert "Desplegado." in r.stdout and r.stdout.count("✓") == 5  # health de 3 control + 2 data (el cliente no tiene servicio)
    runs = [x for x in stdin.split("sudo docker rm -f dfsha-node") if "docker run" in x]
    assert len(runs) == 5
    c1 = next(x for x in runs if "-p 8441:8441 -p 9441:9441" in x)
    assert "dfsha-control" in c1 and "-v /opt/dfsha/secrets:/secrets:ro" in c1 and "--cap-drop ALL" in c1 and "--restart unless-stopped" in c1
    dn2 = next(x for x in runs if "-p 7442:7442 -p 7542:7542" in x)
    assert "dfsha-data" in dn2 and "-v /opt/dfsha/secrets" not in dn2, "el contenedor de un DataNode no monta secretos"
    assert "RSYNC" in log and "ec2-user@ec2-4.compute.example.com:/opt/dfsha/src/" in log
    assert "cluster.key" in log and log.count("cluster.key") == 3, "cluster.key sólo se copia a los 3 ControlNodes"
    assert "--cacert" in log and "/health" in log
    # el cliente sólo recibe la imagen, la CA y el .env
    client_rsync = " ".join(ln for ln in log.splitlines() if ln.startswith("RSYNC") and "ec2-6.compute.example.com" in ln)
    assert "client.env" in client_rsync and "ca.pem" in client_rsync and ".key" not in client_rsync and ".crt" not in client_rsync


def test_deploy_only_filter(sandbox):
    _write_inventory(sandbox.work)
    sandbox("gen-bundles.sh", ADMIN_PASSWORD="clave-larga-1234")
    sandbox("deploy.sh", ONLY="dn1")
    stdin = Path(str(sandbox.log) + ".stdin").read_text()
    assert stdin.count("docker run") == 1 and "dfsha-data" in stdin


def test_refresh_inventory_after_lab_restart_and_teardown(sandbox):
    sandbox("launch.sh", N_DATA="3", N_CLIENT="0")
    st = json.loads(sandbox.state.read_text())
    for i in st["instances"]:  # el laboratorio se detuvo
        i["state"] = "stopped"
        i["pub"] = i["pub"].replace("ec2-", "ec2-OLD-")
    sandbox.state.write_text(json.dumps(st))
    r = sandbox("refresh-inventory.sh", "--start")
    assert "Arrancando instancias detenidas" in r.stdout
    assert (sandbox.work / "inventory.txt").read_text().count("\n") == 6
    sandbox("teardown.sh", "--yes")
    st = json.loads(sandbox.state.read_text())
    assert st["instances"] == [] and st["sg"] is None and not (sandbox.work / "inventory.txt").exists()


def test_node_sh_commands(sandbox):
    _write_inventory(sandbox.work)
    sandbox("node.sh", "kill", "c2")
    sandbox("node.sh", "start", "c2")
    sandbox("node.sh", "logs", "dn1", "7")
    bad = sandbox("node.sh", "kill", "nope", check=False)
    log = sandbox.log.read_text()
    assert "ec2-user@ec2-2.compute.example.com sudo docker kill dfsha-node" in log
    assert "sudo docker start dfsha-node" in log and "docker logs --tail 7 dfsha-node" in log
    assert bad.returncode != 0 and "no está en" in bad.stderr


def test_preflight_reports_problems_without_credentials(sandbox):
    r = sandbox("preflight.sh", check=False, KEY_FILE="/no/existe.pem")
    assert r.returncode != 0 and "no existe" in r.stdout


def test_region_needs_no_aws_configure(sandbox):
    """El AWS CLI sólo lee AWS_DEFAULT_REGION: los scripts la derivan de AWS_REGION (o us-east-1) sin tocar ~/.aws/config."""
    for extra, want in (({"AWS_REGION": "", "AWS_DEFAULT_REGION": ""}, "us-east-1"), ({"AWS_REGION": "us-west-2"}, "us-west-2"),
                        ({"AWS_REGION": "", "AWS_DEFAULT_REGION": "us-west-2"}, "us-west-2")):
        sandbox.state.unlink(missing_ok=True)
        sandbox("preflight.sh", check=False, **extra)
        seen = set(json.loads(sandbox.state.read_text())["regions"])
        assert seen == {want}, (extra, seen)


def test_share_client_opens_only_api_ports_and_builds_a_password_free_bundle(sandbox, tmp_path):
    import zipfile

    sandbox("launch.sh", N_CONTROL="3", N_DATA="4", N_CLIENT="1")
    cl = sandbox.work / "client"
    cl.mkdir()
    (cl / "ca.pem").write_text("-----CA-----")
    (cl / "env.sh").write_text("export DFSHA_ENDPOINTS='https://c1.example:8441,https://c2.example:8442'\nexport DFSHA_CA='/x/ca.pem'\nexport DFSHA_PASSWORD='secreto-que-no-debe-salir'\n")
    r = sandbox("share-client.sh", "--with-ssh", "190.1.2.3", "10.9.8.0/24")
    calls = "\n".join(json.loads(sandbox.state.read_text())["calls"])
    for cidr in ("190.1.2.3/32", "10.9.8.0/24"):
        for port in ("8441-8449", "7441-7449"):
            assert f"--port {port} --cidr {cidr}" in calls, (port, cidr)
    assert "--port 22 --cidr 190.1.2.3/32" not in calls and "--port 22 --cidr 10.9.8.0/24" not in calls, "nunca se abre SSH a un compañero"
    assert "CLAVE SSH" in r.stdout
    z = zipfile.ZipFile(sandbox.work / "share" / "dfsha-cliente.zip")
    names = set(z.namelist())
    assert {"dfsha-cliente/ca.pem", "dfsha-cliente/env.sh", "dfsha-cliente/LEEME.txt", "dfsha-cliente/operador/inventory.txt", "dfsha-cliente/operador/labsuser.pem", "dfsha-cliente/operador/instalar.sh"} <= names
    assert "secreto-que-no-debe-salir" not in z.read("dfsha-cliente/env.sh").decode(), "el zip no lleva contraseñas"
    # el env.sh del zip localiza ca.pem junto a sí mismo, venga de la carpeta que venga (bash y zsh)
    z.extractall(tmp_path / "otro-pc")
    for shell in ("bash", "zsh"):
        if shutil.which(shell):
            out = subprocess.run([shell, "-c", f"cd / && source {tmp_path}/otro-pc/dfsha-cliente/env.sh && echo $DFSHA_CA && echo $DFSHA_ENDPOINTS"], capture_output=True, text=True).stdout.split()
            assert out[0] == f"{tmp_path}/otro-pc/dfsha-cliente/ca.pem" and out[1].startswith("https://c1.example:8441"), (shell, out)
    pk = zipfile.ZipFile(sandbox.work / "share" / "paquete-companero.zip")
    pn = set(pk.namelist())
    assert {"paquete-companero/LEEME.txt", "paquete-companero/dfsha/scripts/companero_inicio.sh", "paquete-companero/dfsha/scripts/companero_parte.sh",
            "paquete-companero/dfsha/scripts/demo_cloud.sh", "paquete-companero/dfsha-cliente/env.sh", "paquete-companero/dfsha-cliente/ca.pem"} <= pn
    assert not any(n.endswith((".key", ".env")) or "/state/" in n for n in pn), "el paquete no lleva claves ni .env"
    # sin --with-ssh no hay clave ni inventario; y las IP mal escritas se rechazan
    sandbox("share-client.sh")
    names = set(zipfile.ZipFile(sandbox.work / "share" / "dfsha-cliente.zip").namelist())
    assert not any("operador" in n or n.endswith(".pem") and "ca.pem" not in n for n in names)
    bad = sandbox("share-client.sh", "no-es-una-ip", check=False)
    assert bad.returncode != 0 and "no válido" in bad.stderr


def test_dot_env_file_configures_the_scripts_but_exported_variables_win(sandbox):
    """deploy/aws/.env (ver .env.example) se lee solo; lo exportado en el terminal tiene prioridad; admite comillas y ~."""
    (sandbox.work / ".env").write_text("# comentario\nN_CONTROL=3\nN_DATA=2\nN_CLIENT=0\nINSTANCE_TYPE=\"t3.micro\"\nKEY_FILE=~/mi-clave.pem\n")
    sandbox("launch.sh")
    inv = [ln.split() for ln in (sandbox.work / "inventory.txt").read_text().splitlines() if ln and not ln.startswith("#")]
    assert [x[0] for x in inv].count("data") == 2 and [x[0] for x in inv].count("client") == 0
    st = json.loads(sandbox.state.read_text())
    assert {i["type"] for i in st["instances"]} == {"t3.micro"}, "las comillas del .env se quitan"
    sandbox("teardown.sh", input_yes=True) if False else None  # (el teardown pide confirmación: no hace falta aquí)
    sandbox.state.unlink()
    (sandbox.work / "inventory.txt").unlink()
    sandbox("launch.sh", N_DATA="3")  # lo exportado gana al .env
    inv = [ln.split() for ln in (sandbox.work / "inventory.txt").read_text().splitlines() if ln and not ln.startswith("#")]
    assert [x[0] for x in inv].count("data") == 3
    probe = subprocess.run(["bash", "-c", f"cd {sandbox.work} && source common.sh && echo $KEY_FILE"], capture_output=True, text=True, env={**os.environ, "HOME": "/home/x", "KEY_FILE": ""})
    assert probe.stdout.strip() == "/home/x/mi-clave.pem", "~ se expande a HOME"


def test_all_scripts_pass_bash_syntax_check():
    for f in sorted(AWS_DIR.glob("*.sh")):
        assert subprocess.run(["bash", "-n", str(f)], capture_output=True).returncode == 0, f.name
    assert textwrap.dedent("x")  # evita aviso de import
