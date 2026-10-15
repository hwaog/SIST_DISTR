"""Las secciones del informe que dependen de evidencia externa (nube / Docker) se generan bien en ambos estados."""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


cs = _mod("cloud_section", ROOT / "report" / "cloud_section.py")
sys.path.insert(0, str(ROOT / "report"))
figs = _mod("figs_mod", ROOT / "report" / "figs.py")

CLOUD = {
    "fecha": "2026-10-20 15:00:00", "ok": True, "endpoints": ["https://x:8441"], "size_mib": 64,
    "topologia": [{"role": "control", "name": "c1", "az": "us-east-1a", "public_host": "ec2-1.example.com"}, {"role": "data", "name": "dn1", "az": "us-east-1b", "public_host": "ec2-2.example.com"}, {"role": "data", "name": "dn2", "az": "us-east-1c", "public_host": "ec2-3.example.com"}],
    "pasos": [
        {"name": "4. put/get con verificación SHA-256", "status": "PASS", "detail": "ok", "seconds": 3.1, "data": {"size_mib": 64, "blocks": 16, "upload_MiBps": 18.5, "download_MiBps": 30.2}},
        {"name": "7. Failover del líder", "status": "PASS", "detail": "ok | con barra", "seconds": 9, "data": {"old_leader": "c1", "new_leader": "c2", "failover_s": 2.3, "rejoin_s": 11}},
        {"name": "8. Caída de un DataNode", "status": "PASS", "detail": "ok", "seconds": 40, "data": {"victim": "dn1", "read_s": 2.0, "detected_s": 14, "repaired_s": 31, "target_replicas": 3}},
    ],
}
SCALING = {"env": {"datanodes": 4, "fecha": "2026-10-20"}, "result": {"repl1": {str(k): {"upload_MiBps": {"mean": 20.0 * k, "sd": 2.0}, "download_MiBps": {"mean": 30.0 * k, "sd": 3.0}} for k in (1, 2, 4)}, "repl3": {}}}


def test_cloud_section_without_evidence_is_honest():
    md = cs.render_cloud(None, {}, None)
    assert "pendiente de ejecución" in md and "no se había desplegado" in md and "|" not in md


def test_cloud_section_with_evidence_contains_measured_values():
    md = cs.render_cloud(CLOUD, {"scaling": SCALING}, None)
    assert "ec2-2.example.com" in md and "us-east-1c" in md
    assert "**Failover del líder en la nube**" in md and "2,3 s" in md and "`c2`" in md
    assert "subida 18,5 MiB/s" in md and "31,0 s" in md
    assert "(4,0×)" in md  # de 20 a 80 MiB/s al pasar de 1 a 4 DataNodes
    assert "ok / con barra" in md, "las barras verticales del detalle se escapan para no romper la tabla"
    assert "cloud_escalado.png" in md


def test_status_table_flips_with_evidence():
    pending = cs.render_status(None, None)
    assert pending.count("**Pendiente.**") == 2
    done = cs.render_status(CLOUD, {"fecha": "2026-10-21", "ok": True})
    assert "**Pendiente.**" not in done and "Verificado el 2026-10-21" in done and "Verificado el 2026-10-20" in done
    failed = cs.render_status(CLOUD, {"fecha": "2026-10-21", "ok": False})
    assert "con fallos" in failed


def test_cloud_scaling_figure_is_rendered(tmp_path):
    c = tmp_path / "scaling.json"
    c.write_text(json.dumps(SCALING))
    out = tmp_path / "f.png"
    assert figs.fig_cloud_scaling(cloud=c, local=tmp_path / "no.json", out=out) is True
    assert out.stat().st_size > 5000
    assert figs.fig_cloud_scaling(cloud=tmp_path / "nada.json") is False
