import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

# Parámetros de scrypt reducidos sólo para la suite de tests (producción: 2**15).
os.environ.setdefault("DFSHA_SCRYPT_N", "1024")

from dfsha.client.sdk import Client  # noqa: E402
from dfsha.cluster import LocalCluster  # noqa: E402


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "cluster" in item.fixturenames or "fresh_cluster" in item.fixturenames:
            item.add_marker(pytest.mark.integration)


def make_cluster(n_control=3, n_data=4, **kw) -> LocalCluster:
    base = Path(tempfile.mkdtemp(prefix="dfsha-test-"))
    kw.setdefault("fsync", False)
    c = LocalCluster(base, n_control=n_control, n_data=n_data, **kw)
    c.prepare()
    c.start_all()
    try:
        c.wait_ready(60)
    except BaseException:
        c.down()
        raise
    return c


@pytest.fixture(scope="session")
def cluster():
    """Cluster compartido (3 ControlNodes + 4 DataNodes en 2 zonas) para pruebas no destructivas."""
    c = make_cluster()
    yield c
    c.down()
    shutil.rmtree(c.base, ignore_errors=True)


@pytest.fixture
def fresh_cluster():
    """Cluster propio por prueba: para caos (matar nodos, corromper datos...)."""
    c = make_cluster()
    yield c
    c.down()
    shutil.rmtree(c.base, ignore_errors=True)


def admin_client(c: LocalCluster, **kw) -> Client:
    cl = Client(c.endpoints, c.ca, credentials=("admin", c.admin_password), **kw)
    cl.login("admin", c.admin_password)
    return cl


def user_client(c: LocalCluster, name: str, password: str = "password-123", admin: Client | None = None, **kw) -> Client:
    admin = admin or admin_client(c)
    try:
        admin.useradd(name, password)
    except Exception:
        pass
    cl = Client(c.endpoints, c.ca, credentials=(name, password), **kw)
    cl.login(name, password)
    return cl


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def eventually(cond, timeout=30.0, step=0.25, msg="condición no se cumplió"):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            last = cond()
            if last:
                return last
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(step)
    raise AssertionError(f"{msg} (último valor: {last!r})")


@pytest.fixture
def admin(cluster):
    return admin_client(cluster)
