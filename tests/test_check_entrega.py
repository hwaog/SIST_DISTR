"""El verificador de entrega detecta material sensible versionado y no da falsos positivos."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_entrega", ROOT / "scripts" / "check_entrega.py")
ce = importlib.util.module_from_spec(_spec)
sys.modules["check_entrega"] = ce
_spec.loader.exec_module(ce)


def test_flags_keys_env_state_and_venv():
    files = [
        "src/dfsha/cloud.py",
        "deploy/aws/state/ca.key",
        "deploy/aws/bundles/c1/cluster.key",
        ".env",
        "pki/dn1.pem",
        ".venv/lib/python3.12/site.py",
        "secrets/admin.pass",
    ]
    bad = ce.tracked_secret_files(files)
    assert "src/dfsha/cloud.py" not in bad
    assert {"deploy/aws/state/ca.key", "deploy/aws/bundles/c1/cluster.key", ".env", "pki/dn1.pem",
            ".venv/lib/python3.12/site.py", "secrets/admin.pass"} <= set(bad)


def test_allows_examples_and_docs():
    ok = [".env.example", "docs/PRUEBAS.md", "docker/init-pki.sh", "deploy/aws/inventory.example",
          "docs/evidence/docker/latest/log.txt", "README.md"]
    assert ce.tracked_secret_files(ok) == []


def test_the_repository_itself_tracks_no_secrets():
    rc, out = ce.sh("git", "ls-files")
    if rc != 0:  # sin git (p. ej. un zip): nada que comprobar
        return
    assert ce.tracked_secret_files(out.splitlines()) == []
