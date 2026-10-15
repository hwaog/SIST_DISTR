"""El CLI `dfsha` (RF1/RF2/RF3) ejercitado contra un cluster real con CliRunner."""

import os
import uuid

import pytest
from click.testing import CliRunner

from dfsha.client.cli import cli


@pytest.fixture
def run(cluster, tmp_path, monkeypatch):
    monkeypatch.setenv("DFSHA_ENDPOINTS", ",".join(cluster.endpoints))
    monkeypatch.setenv("DFSHA_CA", cluster.ca)
    monkeypatch.setenv("DFSHA_USER", "admin")
    monkeypatch.setenv("DFSHA_PASSWORD", cluster.admin_password)
    monkeypatch.setenv("DFSHA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLUMNS", "200")

    def _run(*args, ok=True, user=None, password=None):
        saved = {k: os.environ.get(k) for k in ("DFSHA_USER", "DFSHA_PASSWORD", "DFSHA_HOME")}
        if user:  # otro usuario => otra sesión (otro DFSHA_HOME)
            os.environ.update(DFSHA_USER=user, DFSHA_PASSWORD=password, DFSHA_HOME=str(tmp_path / f"home_{user}"))
        try:
            r = CliRunner().invoke(cli, list(args), catch_exceptions=False)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        if ok:
            assert r.exit_code == 0, f"{args}: {r.output}"
        else:
            assert r.exit_code != 0, f"{args} debía fallar: {r.output}"
        return r.output

    return _run


def test_login_pwd_cd_and_relative_paths(run, tmp_path):
    out = run("login", "-u", "admin", "-p", os.environ["DFSHA_PASSWORD"])
    assert "Sesión iniciada" in out and "/home/admin" in run("pwd")
    d = f"cli_{uuid.uuid4().hex[:6]}"
    run("mkdir", "-p", f"{d}/a/b")
    run("cd", f"{d}/a")
    assert run("pwd").strip().endswith(f"{d}/a")  # el cwd persiste entre invocaciones
    assert "b/" in run("ls")
    run("cd", "/")
    assert "home/" in run("ls")


def test_put_get_cat_blocks_verify_cp_mv_rm(run, tmp_path):
    d = f"/home/admin/cli_{uuid.uuid4().hex[:6]}"
    run("mkdir", "-p", d)
    src = tmp_path / "datos.bin"
    src.write_bytes(os.urandom(9 * 1024 * 1024 + 7))
    out = run("put", str(src), f"{d}/datos.bin", "--repl", "2")
    assert "9.0 MiB" in out or "9.0" in out
    out = run("blocks", f"{d}/datos.bin")
    assert out.count("dn") >= 3 and "replicación=2" in out
    assert "íntegros" in run("verify", f"{d}/datos.bin")
    dst = tmp_path / "copia.bin"
    run("get", f"{d}/datos.bin", str(dst))
    assert dst.read_bytes() == src.read_bytes()
    run("cp", f"{d}/datos.bin", f"{d}/otra.bin")
    run("mv", f"{d}/otra.bin", f"{d}/movida.bin")
    assert "movida.bin" in run("ls", d) and "otra.bin" not in run("ls", d)
    txt = tmp_path / "t.txt"
    txt.write_text("hola desde el CLI")
    run("put", str(txt), f"{d}/t.txt")
    assert "hola desde el CLI" in run("cat", f"{d}/t.txt")
    run("rm", f"{d}/movida.bin")
    run("rm", f"{d}/no-existe", ok=False)
    run("rm", "-f", f"{d}/no-existe")
    run("rm", "-r", d)


def test_put_directory_recursive(run, tmp_path):
    d = tmp_path / "arbol"
    (d / "x" / "y").mkdir(parents=True)
    (d / "a.txt").write_text("a")
    (d / "x" / "y" / "b.txt").write_text("b")
    dest = f"/home/admin/arb_{uuid.uuid4().hex[:6]}"
    run("put", "-r", str(d), dest)
    assert "b.txt" in run("tree", dest) and "a.txt" in run("tree", dest)


def test_permissions_chmod_acl_and_errors(run, tmp_path):
    d = f"/home/admin/perm_{uuid.uuid4().hex[:6]}"
    run("mkdir", d)
    run("chmod", "700", d)  # orden como en Linux: chmod MODO RUTA
    assert "drwx------" in run("ls", "-l", "/home/admin")
    run("chmod", "zzz", d, ok=False)
    name = f"u{uuid.uuid4().hex[:6]}"
    run("useradd", name, "-p", "password-1234")
    out = run("ls", d, ok=False, user=name, password="password-1234")
    assert "permiso denegado" in out
    run("setfacl", "-m", f"u:{name}:r-x", d)
    assert f"user:{name}:r-x" in run("getfacl", d)
    run("chown", f"{name}:{name}", d)
    assert name in run("ls", "-l", "/home/admin")


def test_lock_df_fsck_nodes_users(run, tmp_path):
    p = f"/home/admin/lk_{uuid.uuid4().hex[:6]}"
    f = tmp_path / "x.txt"
    f.write_text("x")
    run("put", str(f), p)
    assert "fencing token" in run("lock", p, "--ttl", "5", "--hold", "0")
    assert "archivos=" in run("df")
    assert "sub-replicados=0" in run("fsck")
    out = run("nodes")
    assert "leader" in out and "dn1" in out
    assert "admin" in run("users")


def test_ls_stale_and_long(run):
    assert "admin" in run("ls", "-l", "--stale", "/home")


def test_unauthenticated_cli_fails_cleanly(cluster, tmp_path, monkeypatch):
    monkeypatch.setenv("DFSHA_ENDPOINTS", ",".join(cluster.endpoints))
    monkeypatch.setenv("DFSHA_CA", cluster.ca)
    monkeypatch.delenv("DFSHA_USER", raising=False)
    monkeypatch.delenv("DFSHA_PASSWORD", raising=False)
    monkeypatch.setenv("DFSHA_HOME", str(tmp_path / "vacio"))
    r = CliRunner().invoke(cli, ["ls", "/"], catch_exceptions=False)
    assert r.exit_code == 1 and "dfsha login" in r.output
