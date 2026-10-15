"""La lógica de scripts/docker_verify.py, con un 'docker' simulado (sin Docker real)."""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("docker_verify", ROOT / "scripts" / "docker_verify.py")
dv = importlib.util.module_from_spec(spec)
sys.modules["docker_verify"] = dv
spec.loader.exec_module(dv)


class FakeDocker:
    def __init__(self, *, leak_ca=False, dn_secrets=False, root=False, leak_port=False, bad_sha=False, unhealthy=False):
        self.leak_ca, self.dn_secrets, self.root, self.leak_port, self.bad_sha, self.unhealthy = leak_ca, dn_secrets, root, leak_port, bad_sha, unhealthy
        self.leader, self.stopped, self.calls = "c1", set(), []
        self.mkdir_attempts = 0

    def call(self, *a, timeout=0, check=False):
        self.calls.append(a)
        if a[0] == "version":
            return 0, "27.1.1\n"
        if a[0] == "compose":
            return 0, "2.29.1\n" if a[1] == "version" else ""
        if a[0] == "inspect":
            if "Health" in a[2]:
                return 0, "unhealthy" if self.unhealthy and a[3] == "dfsha-dn3" else "healthy"
            return 0, "[ALL]\n"
        if a[0] == "ps":
            extra = "dfsha-c1 0.0.0.0:9441->9441/tcp\n" if self.leak_port else ""
            return 0, f"dfsha-c1 0.0.0.0:8441->8441/tcp\ndfsha-dn1 0.0.0.0:7441->7441/tcp\n{extra}"
        if a[0] in ("stop", "kill"):
            self.stopped.add(a[1])
            if a[1] == f"dfsha-{self.leader}":
                self.leader = "c2"
            return 0, ""
        if a[0] == "start":
            self.stopped.discard(a[1])
            return 0, ""
        return 1, "comando no simulado"

    def exec(self, container, *cmd, timeout=0):
        n = container.removeprefix("dfsha-")
        if cmd[0] == "id":
            return 0, "0\n" if self.root and n == "dn4" else "10001\n"
        if cmd[0] == "ls" and cmd[1] == "/pki":
            if n == "client":
                return 0, "ca.pem\n"
            files = ["ca.pem", f"{n}.crt", f"{n}.key"] + (["ca.key"] if self.leak_ca and n == "dn1" else [])
            return 0, "\n".join(files) + "\n"
        if cmd[0] == "ls" and cmd[1] == "/secrets":
            if n.startswith("dn") and not (self.dn_secrets and n == "dn2"):
                return 0, ""  # como en la imagen real: el directorio existe pero está vacío
            return 0, "admin.pass\ncluster.key\n"
        if cmd[0] == "sh" and "find / -name 'ca.key'" in cmd[2]:
            return 0, "/pki/ca.key\n" if self.leak_ca and n == "dn1" else ""
        if cmd[0] == "sh" and "od -An" in cmd[2]:
            return 0, " 44 46 53 42\n"
        if cmd[0] == "sh" and "head -c 50331648" in cmd[2]:
            return 0, "a" * 64 + "\n"
        if cmd[0] == "sh" and "sha256sum /tmp/p.out" in cmd[2]:
            return 0, ("b" if self.bad_sha else "a") * 64 + "\n"
        if cmd[0] == "dfsha":
            sub = cmd[1]
            if sub == "blocks":
                return 0, "\n".join(f"│ {i} │ dn1@z dn2@z dn3@z" for i in range(12))
            if sub == "nodes":
                return 0, f"│ {self.leader} │ leader │ 1 │\n│ c3 │ follower │"
            if sub == "mkdir":
                self.mkdir_attempts += 1
                return (1, "dfsha: este ControlNode no es el líder") if self.mkdir_attempts < 3 else (0, "")
            return 0, ""
        return 1, "no simulado"


def run(**kw):
    return dv.Verifier(FakeDocker(**kw), admin_password="x", log=lambda _m: None, poll=0, healthy_timeout=1).run()


def status(res):
    return {s["name"].split(". ", 1)[1]: s["status"] for s in res["pasos"]}


def test_all_steps_pass_on_a_correct_deployment():
    res = run()
    assert res["ok"] and len(res["pasos"]) == 8 and all(s["status"] == "PASS" for s in res["pasos"]), res["pasos"]
    chaos = res["pasos"][-1]["data"]
    assert chaos["old_leader"] == "c1" and chaos["new_leader"] == "c2"


@pytest.mark.parametrize(
    ("kw", "step", "needle"),
    [
        ({"leak_ca": True}, "Aislamiento de claves por contenedor", "ca.key"),
        ({"dn_secrets": True}, "Aislamiento de claves por contenedor", "dn2"),
        ({"root": True}, "Contenedores sin root ni capacidades", "dn4"),
        ({"leak_port": True}, "Sólo puertos públicos publicados", "9441"),
        ({"bad_sha": True}, "Login, put/get verificado, mapa de bloques", "NO coincide"),
    ],
)
def test_each_violation_is_detected(kw, step, needle):
    res = run(**kw)
    st = {s["name"].split(". ", 1)[1]: s for s in res["pasos"]}
    assert not res["ok"] and st[step]["status"] == "FAIL" and needle in st[step]["detail"], st[step]


def test_unhealthy_container_stops_the_run_early():
    res = run(unhealthy=True)
    assert not res["ok"] and len(res["pasos"]) == 2 and status(res)["build + up: 8 contenedores healthy"] == "FAIL"


def test_ensure_env_generates_a_random_password(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_text("ADMIN_PASSWORD=cambie-esta-clave-1234\nDFSHA_REPL=3\n")
    monkeypatch.setattr(dv, "ROOT", tmp_path)
    pw = dv.ensure_env()
    txt = (tmp_path / ".env").read_text()
    assert re.search(r"^ADMIN_PASSWORD=" + re.escape(pw) + "$", txt, re.M) and len(pw) >= 20 and "DFSHA_REPL=3" in txt
    assert oct((tmp_path / ".env").stat().st_mode & 0o777) == "0o600"
    assert dv.ensure_env() == pw  # idempotente
