"""Los archivos .env.example existen, no tienen variables inventadas y reflejan lo que el código y los scripts leen de verdad."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def keys(path: Path) -> list[str]:
    return re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]+)=", path.read_text(), flags=re.M)


def active_keys(path: Path) -> list[str]:
    return re.findall(r"^([A-Z][A-Z0-9_]+)=", path.read_text(), flags=re.M)


def click_env_names(cmd) -> set[str]:
    prefix = (cmd.context_settings or {}).get("auto_envvar_prefix")
    out = set()
    for p in cmd.params:
        if getattr(p, "envvar", None):
            out.add(p.envvar)
        if prefix and p.name:
            out.add(f"{prefix}_{p.name.upper()}")
    return out


def test_every_example_file_exists_and_is_not_ignored_by_git():
    files = [".env.example", ".env.client.example", "deploy/aws/.env.example", "docker/control.env.example", "docker/data.env.example"]
    for f in files:
        assert (ROOT / f).is_file(), f
        r = subprocess.run(["git", "check-ignore", "-q", f], cwd=ROOT)
        assert r.returncode == 1, f"{f} no debe estar en .gitignore (git check-ignore devolvió {r.returncode})"
    # y los .env reales sí están ignorados (nunca se suben claves)
    for f in (".env", ".env.client", "deploy/aws/.env"):
        assert subprocess.run(["git", "check-ignore", "-q", f], cwd=ROOT).returncode == 0, f"{f} debería estar ignorado"


def test_compose_env_example_only_declares_variables_the_compose_uses():
    compose = (ROOT / "docker-compose.yml").read_text()
    for k in active_keys(ROOT / ".env.example"):
        assert f"${{{k}" in compose, f"{k} está en .env.example pero docker-compose.yml no la usa"
    assert "ADMIN_PASSWORD" in active_keys(ROOT / ".env.example"), "ADMIN_PASSWORD es obligatoria en el compose"


def test_node_env_examples_match_what_the_commands_read():
    from dfsha.control.main import main as control
    from dfsha.data.main import main as data

    for name, cmd, required in (
        ("docker/control.env.example", control, {"DFSHA_NODE_ID", "DFSHA_PEERS", "DFSHA_CERT", "DFSHA_KEY", "DFSHA_CA", "DFSHA_CLUSTER_SECRET_FILE"}),
        ("docker/data.env.example", data, {"DFSHA_NODE_ID", "DFSHA_CONTROLS", "DFSHA_CERT", "DFSHA_KEY", "DFSHA_CA", "DFSHA_ADVERTISE_PUBLIC", "DFSHA_ADVERTISE_INTERNAL"}),
    ):
        known = click_env_names(cmd)
        declared = set(keys(ROOT / name))
        assert declared <= known, f"{name}: variables que el comando no lee: {sorted(declared - known)}"
        assert required <= set(active_keys(ROOT / name)), f"{name}: faltan obligatorias: {sorted(required - set(active_keys(ROOT / name)))}"


def test_data_example_never_contains_the_cluster_master_key():
    assert "CLUSTER_SECRET" not in (ROOT / "docker/data.env.example").read_text().replace("NUNCA", "")  # un DataNode jamás la recibe


def test_client_env_example_uses_variables_the_client_reads():
    src = "\n".join(p.read_text() for p in (ROOT / "src" / "dfsha" / "client").glob("*.py"))
    for k in keys(ROOT / ".env.client.example"):
        assert k in src, f"{k} no la lee el cliente"
    assert {"DFSHA_ENDPOINTS", "DFSHA_CA", "DFSHA_USER", "DFSHA_PASSWORD"} <= set(active_keys(ROOT / ".env.client.example"))


def test_aws_env_example_uses_variables_the_scripts_read():
    scripts = "\n".join(p.read_text() for p in (ROOT / "deploy" / "aws").glob("*.sh"))
    for k in keys(ROOT / "deploy/aws/.env.example"):
        assert re.search(rf"\b{k}\b", scripts), f"{k} no la usa ningún script de deploy/aws"
