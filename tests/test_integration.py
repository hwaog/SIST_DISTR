"""Pruebas de integración sobre un cluster real: 3 ControlNodes + 4 DataNodes con TLS/mTLS."""

import io
import os
import threading
import uuid
from pathlib import Path

import httpx
import pytest
from conftest import admin_client, eventually, sha, user_client

from dfsha.common import errors as E
from dfsha.pki import client_context, issue_cert
from dfsha.security import auth

BS = 4 * 1024 * 1024


def rnd(n: int) -> bytes:
    return os.urandom(n)


def uniq(prefix="t") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------- RF2: send / receive
@pytest.mark.parametrize("size", [0, 1, 1000, BS - 1, BS, BS + 1, 3 * BS + 7, 20 * BS])
def test_roundtrip_sizes(cluster, admin, size):
    data = rnd(size)
    p = f"/home/admin/{uniq('rt')}"
    r = admin.send(data, p)
    assert r["size"] == size and r["blocks"] == (size + BS - 1) // BS
    assert admin.stat(p)["size"] == size
    assert sha(admin.receive(p)) == sha(data)


def test_put_get_files_and_overwrite_versions(cluster, admin, tmp_path):
    src, dst = tmp_path / "a.bin", tmp_path / "b.bin"
    src.write_bytes(rnd(9 * BS + 5))
    p = f"/home/admin/{uniq('pg')}"
    r1 = admin.put(str(src), p)
    assert r1["version"] == 1
    admin.get(p, str(dst))
    assert dst.read_bytes() == src.read_bytes()
    src.write_bytes(rnd(100))
    assert admin.put(str(src), p)["version"] == 2  # reemplazo atómico, nueva versión
    assert sha(admin.receive(p)) == sha(src.read_bytes())
    with pytest.raises(E.Exists):
        admin.put(str(src), p, overwrite=False)


def test_blocks_are_partitioned_and_replicated_across_zones(cluster, admin):
    p = f"/home/admin/{uniq('dist')}"
    admin.send(rnd(12 * BS), p)  # 12 bloques
    meta = admin._locations_all(admin.abspath(p))
    zones = {d["id"]: d["zone"] for d in admin.datanodes()}
    eventually(lambda: all(len(admin._locations_all(admin.abspath(p))["blocks"][i]["locs"]) == 3 for i in range(12)), msg="no llegó a 3 réplicas")
    meta = admin._locations_all(admin.abspath(p))
    used_nodes = {loc["id"] for b in meta["blocks"] for loc in b["locs"]}
    assert len(used_nodes) == 4, "el archivo debe distribuirse entre TODOS los DataNodes"
    for b in meta["blocks"]:
        assert len({loc["id"] for loc in b["locs"]}) == 3, "réplicas en nodos distintos"
        assert len({zones[loc["id"]] for loc in b["locs"]}) == 2, "las réplicas abarcan ambas zonas"
    # balance: ningún nodo concentra todas las primeras réplicas
    first = [b["locs"][0]["id"] for b in meta["blocks"]]
    assert len(set(first)) >= 2


def test_data_is_encrypted_at_rest(cluster, admin):
    marker = b"TEXTO-CLARO-SUPER-SECRETO-" * 1000
    p = f"/home/admin/{uniq('enc')}"
    admin.send(marker, p)
    eventually(lambda: any(cluster.base.glob("data/dn*/blocks/*/*.blk")))
    checked = 0
    for f in list(cluster.base.glob("data/dn*/blocks/*/*.blk")):
        try:
            raw = f.read_bytes()
        except FileNotFoundError:  # el GC de otra prueba pudo borrar el bloque entre el glob y la lectura
            continue
        assert b"TEXTO-CLARO" not in raw, f"texto en claro encontrado en disco: {f}"
        assert raw[:4] == b"DFSB"
        checked += 1
    assert checked > 0
    assert admin.receive(p) == marker


def test_read_verifies_sha256_end_to_end(cluster, admin):
    p = f"/home/admin/{uniq('v')}"
    data = rnd(3 * BS)
    admin.send(data, p)
    s = admin.abspath(p)
    meta = admin._locations_all(s)
    for i, b in enumerate(meta["blocks"]):
        assert sha(admin._fetch_block(s, b, i)) == b["sha"]


# ---------------------------------------------------------------- RF3: open/read/write
def test_open_write_read_seek(cluster, admin):
    p = f"/home/admin/{uniq('f')}"
    data = rnd(5 * BS + 17)
    with admin.open(p, "wb") as f:
        for i in range(0, len(data), 1_000_003):
            f.write(data[i : i + 1_000_003])
    with admin.open(p, "rb") as f:
        assert f.read(10) == data[:10]
        f.seek(BS - 5)
        assert f.read(10) == data[BS - 5 : BS + 5]  # cruza el límite de bloque
        f.seek(-20, io.SEEK_END)
        assert f.read() == data[-20:]
        f.seek(0)
        assert f.read() == data
    raw = admin.open(p, "rb").raw
    assert raw.pread(2 * BS + 3, 100) == data[2 * BS + 3 : 2 * BS + 103]
    raw.close()


def test_open_text_mode_and_append(cluster, admin):
    p = f"/home/admin/{uniq('txt')}"
    with admin.open(p, "w") as f:
        f.write("línea 1\n")
    with admin.open(p, "a") as f:
        f.write("línea 2\n")
    with admin.open(p, "r") as f:
        assert f.read() == "línea 1\nlínea 2\n"
    assert admin.stat(p)["version"] == 2


def test_append_creates_new_blocks_keeping_old_ones(cluster, admin):
    p = f"/home/admin/{uniq('ap')}"
    a, b = rnd(BS + 10), rnd(2 * BS + 3)
    admin.send(a, p)
    ids_before = [x["block_id"] for x in admin._locations_all(admin.abspath(p))["blocks"]]
    with admin.open(p, "ab") as f:
        f.write(b)
    meta = admin._locations_all(admin.abspath(p))
    assert [x["block_id"] for x in meta["blocks"]][: len(ids_before)] == ids_before  # bloques previos intactos
    assert sha(admin.receive(p)) == sha(a + b)


def test_update_random_write_is_copy_on_write(cluster, admin):
    p = f"/home/admin/{uniq('upd')}"
    data = bytearray(rnd(4 * BS))
    admin.send(bytes(data), p)
    before = [x["block_id"] for x in admin._locations_all(admin.abspath(p))["blocks"]]
    with admin.open(p, "r+b") as f:
        f.seek(BS + 100)
        f.write(b"PARCHE" * 50)
        data[BS + 100 : BS + 400] = b"PARCHE" * 50
    after = [x["block_id"] for x in admin._locations_all(admin.abspath(p))["blocks"]]
    assert after[0] == before[0] and after[2:] == before[2:] and after[1] != before[1]  # sólo cambió 1 bloque
    assert sha(admin.receive(p)) == sha(bytes(data))
    assert admin.stat(p)["version"] == 2


def test_update_extend_hole_and_truncate(cluster, admin):
    p = f"/home/admin/{uniq('ext')}"
    admin.send(b"abc", p)
    with admin.open(p, "r+b") as f:
        f.seek(10)
        f.write(b"XYZ")  # hueco de ceros entre 3 y 10
    assert admin.receive(p) == b"abc" + bytes(7) + b"XYZ"
    with admin.open(p, "r+b") as f:
        f.truncate(5)
    assert admin.receive(p) == b"abc\x00\x00"
    with admin.open(p, "r+b") as f:  # sin cambios => no se crea versión nueva
        f.read()
    assert admin.stat(p)["version"] == 3


def test_failed_writer_aborts_and_leaves_no_file(cluster, admin):
    p = f"/home/admin/{uniq('ab')}"
    with pytest.raises(RuntimeError):
        with admin.open(p, "wb") as f:
            f.write(rnd(2 * BS))
            raise RuntimeError("boom")
    with pytest.raises(E.NotFound):
        admin.stat(p)


def test_reader_that_failed_to_open_can_still_be_closed():
    """Leer algo sin permiso (o inexistente) falla en el constructor; el lector a medio construir debe poder cerrarse
    (Python lo cierra al destruirlo y, según versión/modo, mostraba «Exception ignored ... _ex»)."""
    from dfsha.client.fileobj import DfshaReader

    class Denied:
        def _locations_all(self, path):
            raise E.PermissionDenied(f"permiso denegado: {path}")

    r = DfshaReader.__new__(DfshaReader)
    with pytest.raises(E.PermissionDenied):
        DfshaReader.__init__(r, Denied(), "/home/admin/x")
    r.close()  # antes: AttributeError: 'DfshaReader' object has no attribute '_ex'
    assert r.closed


def test_empty_file(cluster, admin):
    p = f"/home/admin/{uniq('e')}"
    with admin.open(p, "wb"):
        pass
    assert admin.stat(p)["size"] == 0 and admin.receive(p) == b""


# ----------------------------------------------------------------- RF1: gestión del FS
def test_filesystem_operations(cluster, admin):
    d = f"/home/admin/{uniq('fs')}"
    admin.mkdir(f"{d}/a/b", parents=True)
    admin.send(b"x", f"{d}/a/f1")
    assert [e["name"] for e in admin.ls(f"{d}/a")] == ["b", "f1"]
    admin.mv(f"{d}/a/f1", f"{d}/a/b/f2")
    assert admin.exists(f"{d}/a/b/f2") and not admin.exists(f"{d}/a/f1")
    with pytest.raises(E.NotEmpty):
        admin.rmdir(f"{d}/a")
    with pytest.raises(E.IsADirectory):
        admin.rm(f"{d}/a")
    admin.cwd = d
    assert admin.exists("a/b/f2")  # rutas relativas al directorio de trabajo
    walked = [(p, sorted(ds), sorted(fs)) for p, ds, fs in admin.walk(d)]
    assert walked[0] == (d, ["a"], [])
    admin.rm(d, recursive=True)
    assert not admin.exists(d)
    admin.cwd = "/"


def test_stale_read_from_any_replica(cluster, admin):
    p = f"/home/admin/{uniq('st')}"
    admin.mkdir(p)
    eventually(lambda: admin.ls("/home/admin", stale=True) and p.split("/")[-1] in [e["name"] for e in admin.ls("/home/admin", stale=True)])


# --------------------------------------------------------------------- seguridad
def test_permissions_and_acls_between_users(cluster, admin):
    alice, bob = user_client(cluster, "alice", admin=admin), user_client(cluster, "bob", admin=admin)
    alice.send(b"privado", "/home/alice/secret.txt")
    with pytest.raises(E.PermissionDenied):
        bob.receive("/home/alice/secret.txt")
    with pytest.raises(E.PermissionDenied):
        bob.ls("/home/alice")
    with pytest.raises(E.PermissionDenied):
        bob.send(b"x", "/home/alice/hack")
    # ACL: alice concede r-x a bob sobre su home y lectura sobre el archivo
    alice.setacl("/home/alice", [{"t": "user", "n": "bob", "p": 5}])
    alice.setacl("/home/alice/secret.txt", [{"t": "user", "n": "bob", "p": 4}])
    assert bob.receive("/home/alice/secret.txt") == b"privado"
    with pytest.raises(E.PermissionDenied):
        bob.rm("/home/alice/secret.txt")
    with pytest.raises(E.PermissionDenied):
        bob.send(b"x", "/home/alice/secret.txt")  # sólo lectura
    # sticky de grupo compartido
    alice.send(b"colab", "/shared/doc_alice")
    assert bob.receive("/shared/doc_alice") == b"colab"


def test_block_tokens_are_bound_to_block_and_operation(cluster, admin):
    p = f"/home/admin/{uniq('tok')}"
    admin.send(rnd(2 * BS), p)
    s = admin.abspath(p)
    meta = admin._locations_all(s)
    b0, b1 = meta["blocks"][0], meta["blocks"][1]
    url = b0["locs"][0]["url"]
    http = httpx.Client(verify=client_context(cluster.ca))
    # sin token
    assert http.get(f"{url}/v1/blocks/{b0['block_id']}").status_code == 401
    # token de otro bloque
    r = http.get(f"{url}/v1/blocks/{b0['block_id']}", headers={"Authorization": f"Bearer {b1['token']}"})
    assert r.status_code == 403
    # token de lectura no sirve para escribir
    r = http.put(f"{url}/v1/blocks/{b0['block_id']}", content=b"x", headers={"Authorization": f"Bearer {b0['token']}", "X-Block-Sha256": sha(b"x")})
    assert r.status_code == 403
    # token falsificado
    r = http.get(f"{url}/v1/blocks/{b0['block_id']}", headers={"Authorization": "Bearer v1.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"})
    assert r.status_code == 401
    # id de bloque malicioso (path traversal)
    r = http.get(f"{url}/v1/blocks/..%2F..%2Fetc%2Fpasswd", headers={"Authorization": f"Bearer {b0['token']}"})
    assert r.status_code in (400, 404, 403)
    # el token legítimo funciona
    assert http.get(f"{url}/v1/blocks/{b0['block_id']}", headers={"Authorization": f"Bearer {b0['token']}"}).status_code == 200


def test_mtls_internal_ports_reject_outsiders(cluster, admin):
    internal = cluster.control_urls(1)[1]
    # 1) sin certificado de cliente: el handshake falla
    with pytest.raises(httpx.TransportError):
        httpx.get(f"{internal}/metrics", verify=client_context(cluster.ca), timeout=3)
    # 2) certificado válido pero de rol equivocado (datanode en /raft) -> 403
    crt, key = issue_cert(cluster.pki, name="intruso", role="datanode", sans=["127.0.0.1"])
    c = httpx.Client(verify=client_context(cluster.ca, str(crt), str(key)), timeout=3)
    assert c.post(f"{internal}/raft/append_entries", json={}).status_code == 403
    # 3) certificado de OTRA CA: rechazado en el handshake
    from dfsha.pki import create_ca

    other = cluster.base / "otra-ca"
    create_ca(other)
    crt2, key2 = issue_cert(other, name="falso", role="controlnode", sans=["127.0.0.1"])
    with pytest.raises(httpx.TransportError):
        httpx.get(f"{internal}/metrics", verify=client_context(cluster.ca, str(crt2), str(key2)), timeout=3)
    # 4) un DataNode no puede hacerse pasar por otro en el heartbeat
    r = c.post(f"{internal}/internal/dn/heartbeat", json={"dn": "dn1"})
    assert r.status_code == 403


def test_plain_http_is_not_served(cluster):
    url = cluster.endpoints[0].replace("https://", "http://")
    with pytest.raises(httpx.TransportError):
        httpx.get(f"{url}/health", timeout=3)


def test_api_requires_authentication(cluster):
    http = httpx.Client(verify=client_context(cluster.ca))
    for path in ("/v1/fs/ls?path=/", "/v1/cluster", "/v1/admin/users", "/v1/whoami"):
        assert http.get(cluster.endpoints[0] + path).status_code in (401, 503), path
    r = http.get(cluster.endpoints[0] + "/v1/fs/ls?path=/", headers={"Authorization": "Bearer basura"})
    assert r.status_code in (401, 503)


def test_login_failures_throttle_and_password_change_revokes_tokens(cluster, admin):
    name = uniq("sec")
    admin.useradd(name, "correct-horse-1")
    from dfsha.client.sdk import Client

    c = Client(cluster.endpoints, cluster.ca)
    for _ in range(5):
        with pytest.raises(E.Unauthenticated):
            c.login(name, "mala-clave")
    with pytest.raises(E.TooManyAttempts):  # bloqueo aun con la clave correcta
        c.login(name, "correct-horse-1")
    # cambio de clave invalida sesiones vigentes
    name2 = uniq("pw")
    admin.useradd(name2, "clave-original-1")
    c2 = Client(cluster.endpoints, cluster.ca)
    c2.login(name2, "clave-original-1")
    old_token = c2.token
    c2.change_password("clave-original-1", "clave-nueva-1234")
    c2.token = old_token
    with pytest.raises(E.Unauthenticated):
        c2.whoami()
    c2.login(name2, "clave-nueva-1234")
    assert c2.whoami()["name"] == name2


def test_totp_2fa_flow_and_replay_protection(cluster, admin):
    name = uniq("tf")
    admin.useradd(name, "password-12345")
    from dfsha.client.sdk import Client

    c = Client(cluster.endpoints, cluster.ca)
    c.login(name, "password-12345")
    sec = c.enable_2fa("password-12345")["secret"]
    c2 = Client(cluster.endpoints, cluster.ca)
    with pytest.raises(E.Unauthenticated) as ei:
        c2.login(name, "password-12345")  # falta el segundo factor
    assert ei.value.extra.get("otp_required")
    with pytest.raises(E.Unauthenticated):
        c2.login(name, "password-12345", "000000")
    code = auth.totp_now(sec)
    c2.login(name, "password-12345", code)
    assert c2.whoami()["totp"] is True
    with pytest.raises(E.Unauthenticated, match="utilizado"):
        Client(cluster.endpoints, cluster.ca).login(name, "password-12345", code)  # replay del mismo código


def test_api_keys(cluster, admin):
    from dfsha.client.sdk import Client

    k = admin.create_api_key("ci")
    c = Client(cluster.endpoints, cluster.ca, api_key=k["key"])
    assert c.whoami()["name"] == "admin"
    admin.revoke_api_key(k["id"])
    with pytest.raises(E.Unauthenticated):
        c.whoami()


def test_only_admin_can_manage_users(cluster, admin):
    eve = user_client(cluster, uniq("eve"), admin=admin)
    with pytest.raises(E.PermissionDenied):
        eve.useradd("zz", "password-123")
    with pytest.raises(E.PermissionDenied):
        eve.users()


# -------------------------------------------------------------------- concurrencia
def test_concurrent_uploads_and_downloads(cluster, admin):
    results, errors = {}, []

    def work(i: int) -> None:
        try:
            c = admin_client(cluster)
            data = rnd(2 * BS + i * 1000)
            p = f"/home/admin/conc_{uuid.uuid4().hex[:6]}_{i}"
            c.send(data, p)
            assert sha(c.receive(p)) == sha(data)
            results[i] = True
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    ts = [threading.Thread(target=work, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    assert len(results) == 12


def test_concurrent_writers_last_wins_and_cas(cluster, admin):
    p = f"/home/admin/{uniq('cw')}"
    admin.send(b"v1", p)
    v = admin.stat(p)["version"]
    admin.send(b"v2", p, if_version=v)  # CAS ok
    with pytest.raises(E.Conflict):
        admin.send(b"v3", p, if_version=v)  # versión obsoleta
    assert admin.receive(p) == b"v2"
    # dos 'r+b' abiertos a la vez: el segundo en confirmar pierde (optimista)
    a, b = admin.open(p, "r+b"), admin.open(p, "r+b")
    a.write(b"AA")
    b.write(b"BB")
    a.close()
    with pytest.raises(E.Conflict):
        b.close()
    assert admin.receive(p) == b"AA"


def test_locks_exclusive_shared_and_enforcement(cluster, admin):
    alice, bob = user_client(cluster, "alice", admin=admin), user_client(cluster, "bob", admin=admin)
    p = f"/shared/{uniq('lk')}"
    alice.send(b"datos", p)
    alice.chmod(p, 0o666)
    with alice.lock(p, "exclusive", ttl=10) as lk:
        assert lk.fence > 0
        with pytest.raises(E.Locked):
            bob.lock(p, "shared")
        with pytest.raises(E.Locked):
            bob.send(b"intruso", p)  # el lock se hace cumplir en el servidor
        alice.send(b"mio", p, lock_id=lk.lock_id)  # el dueño sí puede escribir
    assert bob.receive(p) == b"mio"
    l1, l2 = bob.lock(p, "shared"), alice.lock(p, "shared")
    assert l2.fence > l1.fence  # fencing token monótono
    with pytest.raises(E.Locked):
        alice.lock(p, "exclusive")
    l1.release(), l2.release()
    alice.lock(p, "exclusive").release()


def test_lock_lease_expires_without_renewal(cluster, admin):
    import time

    p = f"/home/admin/{uniq('ls')}"
    admin.send(b"x", p)
    lk = admin.lock(p, "exclusive", ttl=2)
    lk._stop.set()  # simula cliente muerto: deja de renovar
    time.sleep(3.5)
    admin.lock(p, "exclusive", ttl=5).release()  # el lease venció: se puede tomar


# ------------------------------------------------------------ redirección de líder
def test_client_pointed_at_follower_is_redirected(cluster, admin):
    from dfsha.client.sdk import Client

    followers = [i for i in range(1, 4) if (cluster.node_info(i) or {}).get("role") == "follower"]
    assert followers
    c = Client([cluster.control_urls(followers[0])[0]], cluster.ca, credentials=("admin", cluster.admin_password))
    c.login("admin", cluster.admin_password)
    p = f"/home/admin/{uniq('rd')}"
    c.send(b"via follower", p)
    assert c.receive(p) == b"via follower"
    assert c._leader != cluster.control_urls(followers[0])[0]  # aprendió quién es el líder


def test_cluster_status_endpoint(cluster, admin):
    st = admin.cluster()
    assert len(st["datanodes"]) == 4 and all(d["alive"] for d in st["datanodes"])
    assert st["raft"]["term"] >= 1
    assert Path(cluster.base / "data" / "c1" / "audit.log").exists()


def test_datanode_rejects_oversized_put_before_reading_body(cluster, admin):
    p = f"/home/admin/{uniq('big')}"
    sess = admin._begin(admin.abspath(p), "replace")
    alloc = admin._alloc(sess["sid"], 1)[0]
    loc = alloc["pipeline"][0]
    http = httpx.Client(verify=client_context(cluster.ca), timeout=10)
    body = os.urandom(BS + 1)  # más que el máximo del token (bloque de 4 MiB)
    r = http.put(f"{loc['url']}/v1/blocks/{alloc['block_id']}", content=body,
                 headers={"Authorization": f"Bearer {alloc['token']}", "X-Block-Sha256": sha(body)})
    assert r.status_code == 400 and r.json()["error"]["code"] == "EINVAL"
    admin._call("POST", f"/v1/files/{sess['sid']}/abort")


def test_open_with_lock_holds_and_releases(cluster, admin):
    alice, bob = user_client(cluster, "alice", admin=admin), user_client(cluster, "bob", admin=admin)
    p = f"/shared/{uniq('ol')}"
    alice.send(b"base", p)
    alice.chmod(p, 0o666)
    with alice.open(p, "r+b", lock="exclusive") as f:
        f.write(b"X")
        with pytest.raises(E.Locked):
            bob.send(b"intruso", p)
    bob.send(b"ya libre", p)  # el lock se liberó al cerrar
    assert bob.receive(p) == b"ya libre"
