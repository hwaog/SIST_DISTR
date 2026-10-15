"""scripts/demo_cloud.sh (el guion del video EN LA NUBE) ejecutado de punta a punta contra un cluster local."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_cloud_demo_script_runs_end_to_end_against_a_local_cluster(fresh_cluster, tmp_path):
    c = fresh_cluster
    env = {
        **os.environ,
        "DFSHA_ENDPOINTS": ",".join(c.endpoints), "DFSHA_CA": c.ca, "DFSHA_PASSWORD": c.admin_password, "DFSHA_USER": "admin",
        "NODE_SH": str(ROOT / "tests" / "fixtures" / "node_shim.sh"), "DEMO_CLUSTER_DIR": str(c.base),
        "DFSHA_VENV_BIN": str(Path(sys.executable).parent), "DFSHA_HOME": str(tmp_path / "client"),
    }  # fmt: skip
    r = subprocess.run(["bash", str(ROOT / "scripts" / "demo_cloud.sh"), "--auto"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-3000:]
    for needle in (
        "contenido idéntico", "login sin segundo factor", "login con código TOTP válido", "lectura correcta con un DataNode caído",
        "subida completada pese a la caída del líder", "segundo escritor optimista rechazado", "00000000:"  # volcado hexadecimal del bloque cifrado en disco,
    ):  # fmt: skip
        assert needle in out, f"falta en la salida: {needle!r}"
    assert "sub-replicados=0" in out and "permiso denegado" in out and "HTTP en claro: rechazado" in out


def _env(c, tmp_path, **extra):
    return {
        **os.environ,
        "DFSHA_ENDPOINTS": ",".join(c.endpoints), "DFSHA_CA": c.ca, "DFSHA_PASSWORD": c.admin_password, "DFSHA_USER": "admin",
        "NODE_SH": str(ROOT / "tests" / "fixtures" / "node_shim.sh"), "DEMO_CLUSTER_DIR": str(c.base),
        "DFSHA_VENV_BIN": str(Path(sys.executable).parent), "DFSHA_HOME": str(tmp_path / "client"), **extra,
    }  # fmt: skip


def test_each_teammate_can_run_only_their_own_steps_from_another_device(fresh_cluster, tmp_path):
    """--only: sin login previo, con directorio y tamaño propios, y sin tocar los pasos de los demás."""
    env = _env(fresh_cluster, tmp_path, SIZE_MIB="4", DEMO_DIR="andres")
    r = subprocess.run(["bash", str(ROOT / "scripts" / "demo_cloud.sh"), "--auto", "--only", "3,5"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-3000:]
    assert "contenido idéntico" in out and "login con código TOTP válido" in out and "permiso denegado" in out
    assert "andres/videos/video.bin" in out and "head -c 4194304" in out  # SIZE_MIB y DEMO_DIR se respetan
    for absent in ("caída abrupta", "LÍDER Raft", "CAS rechazado", "El cluster REAL"):
        assert absent not in out, absent
    # la demo anterior sigue sirviendo: un paso de HA aislado prepara solo el archivo de prueba que necesita
    r = subprocess.run(["bash", str(ROOT / "scripts" / "demo_cloud.sh"), "--auto", "--only", "6"], cwd=ROOT, env=_env(fresh_cluster, tmp_path, SIZE_MIB="4", DEMO_DIR="samuel"), capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    assert r.returncode == 0 and "lectura correcta con un DataNode caído" in r.stdout + r.stderr, (r.stdout + r.stderr)[-2000:]


def test_ssh_steps_explain_what_is_missing_when_there_is_no_operator_kit(fresh_cluster, tmp_path):
    env = _env(fresh_cluster, tmp_path, INVENTORY=str(tmp_path / "no-existe.txt"))
    env.pop("NODE_SH")
    r = subprocess.run(["bash", str(ROOT / "scripts" / "demo_cloud.sh"), "--auto", "--only", "6"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 1 and "kit de operador" in r.stderr and "--only 2,3,5,8,9" in r.stderr
    bad = subprocess.run(["bash", str(ROOT / "scripts" / "demo_cloud.sh"), "--nope"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert bad.returncode == 2


def test_teammate_scripts_run_a_step_with_only_the_unzipped_folder(fresh_cluster, tmp_path):
    """companero_parte.sh y companero_inicio.sh: lo que de verdad teclea un compañero (paquete descomprimido + contraseña)."""
    c = fresh_cluster
    cli = tmp_path / "dfsha-cliente"
    cli.mkdir()
    (cli / "env.sh").write_text(f"export DFSHA_ENDPOINTS='{','.join(c.endpoints)}'\nexport DFSHA_CA='{c.ca}'\nexport DFSHA_USER=admin\n")
    env = {**os.environ, "DFSHA_CLIENTE": str(cli), "DFSHA_PASSWORD": c.admin_password, "DFSHA_VENV_BIN": str(Path(sys.executable).parent), "DFSHA_HOME": str(tmp_path / "home")}
    ini = subprocess.run(["bash", str(ROOT / "scripts" / "companero_inicio.sh")], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    assert ini.returncode == 0 and "TODO LISTO" in ini.stdout, (ini.stdout + ini.stderr)[-2000:]
    r = subprocess.run(["bash", str(ROOT / "scripts" / "companero_parte.sh"), "8", "--auto"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    assert r.returncode == 0 and "segundo escritor optimista rechazado" in r.stdout + r.stderr, (r.stdout + r.stderr)[-2000:]
    # sin el zip de dfsha-cliente al lado, el mensaje dice qué hacer en lugar de un error técnico
    bad = subprocess.run(["bash", str(ROOT / "scripts" / "companero_inicio.sh")], cwd=ROOT, env={**env, "DFSHA_CLIENTE": str(tmp_path / "nada")}, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert bad.returncode == 1 and "Descomprima el zip COMPLETO" in bad.stdout
