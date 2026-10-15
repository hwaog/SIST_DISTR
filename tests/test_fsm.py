import random
import uuid

import pytest

from dfsha.common import errors as E
from dfsha.control.fsm import MetadataStore

R_, W_, X_ = 4, 2, 1


class H:
    """Arnés: aplica comandos como lo haría Raft (índice creciente, ts del líder)."""

    def __init__(self, fs=None):
        self.fs = fs or MetadataStore()
        self.i = 0
        self.t = 1000.0

    def do(self, op, **kw):
        self.i += 1
        self.t += 0.01
        kw.setdefault("req_id", f"auto-{self.i}")
        return self.fs.apply(self.i, {"op": op, "ts": self.t, **kw})

    def tick(self, s):
        self.t += s

    def setup(self):
        self.do("bootstrap_admin", pw="x")
        for d in ("d1", "d2", "d3", "d4"):
            self.do("dn_register", dn=d, zone="z", public_url=f"https://{d}", internal_url=f"https://{d}i")
        for u in ("alice", "bob", "carol"):
            self.do("user_add", actor="admin", name=u, pw="x")
        return self

    def put(self, user, path, sizes=(100, 50), mode="replace", reps=("d1", "d2"), **kw):
        s = self.do(
            "begin_write", user=user, path=path, mode=mode, sid=uuid.uuid4().hex, repl=3, min_repl=2,
            bs=100, ttl=60, dek="dek", **kw,
        )  # fmt: skip
        ids = [uuid.uuid4().hex for _ in sizes]
        self.do("alloc_blocks", user=user, sid=s["sid"], blocks=[{"id": b, "dns": list(reps)} for b in ids])
        layout = [{"id": b, "len": n, "sha": "h", "reps": list(reps)} for b, n in zip(ids, sizes, strict=True)]
        r = self.do("commit_write", user=user, sid=s["sid"], layout=layout, size=sum(sizes))
        return r, ids


@pytest.fixture
def h():
    return H().setup()


def test_bootstrap_creates_layout_and_is_idempotent(h):
    assert h.do("bootstrap_admin", pw="y") == {"created": False}
    names = [e["name"] for e in h.fs.q_ls("admin", "/")]
    assert names == ["home", "shared"]
    assert "alice" in [e["name"] for e in h.fs.q_ls("admin", "/home")]


def test_user_validation_and_admin_only(h):
    with pytest.raises(E.PermissionDenied):
        h.do("user_add", actor="alice", name="mallory", pw="x")
    with pytest.raises(E.InvalidArgument):
        h.do("user_add", actor="admin", name="Bad Name!", pw="x")
    with pytest.raises(E.Exists):
        h.do("user_add", actor="admin", name="alice", pw="x")


def test_mkdir_ls_stat_and_permissions(h):
    h.do("mkdir", user="alice", path="/home/alice/proj/src", parents=True)
    assert h.fs.q_stat("alice", "/home/alice/proj")["owner"] == "alice"
    with pytest.raises(E.NotFound):
        h.do("mkdir", user="alice", path="/home/alice/a/b/c")
    with pytest.raises(E.Exists):
        h.do("mkdir", user="alice", path="/home/alice/proj")
    # bob no puede entrar ni listar el home de alice (0750, grupo alice)
    with pytest.raises(E.PermissionDenied):
        h.fs.q_ls("bob", "/home/alice")
    with pytest.raises(E.PermissionDenied):
        h.do("mkdir", user="bob", path="/home/alice/hack")
    # pero /shared (0775, grupo users) es de todos
    h.do("mkdir", user="bob", path="/shared/bobdir")
    assert any(e["name"] == "bobdir" for e in h.fs.q_ls("carol", "/shared"))


def test_path_traversal_is_neutralised(h):
    # el '..' se resuelve léxicamente: no se puede salir de la jerarquía de permisos
    with pytest.raises(E.PermissionDenied):
        h.fs.q_ls("bob", "/home/bob/../alice")
    with pytest.raises(E.InvalidArgument):
        h.do("mkdir", user="bob", path="/home/bob/\x00evil")


def test_acl_grants_and_group_semantics(h):
    h.do("mkdir", user="alice", path="/home/alice/docs")
    h.do("chmod", user="alice", path="/home/alice", mode=0o700)
    h.do("chmod", user="alice", path="/home/alice/docs", mode=0o700)
    with pytest.raises(E.PermissionDenied):
        h.fs.q_ls("bob", "/home/alice/docs")
    # ACL: bob r-x en home/alice y docs
    h.do("setacl", user="alice", path="/home/alice", entries=[{"t": "user", "n": "bob", "p": R_ | X_}])
    h.do("setacl", user="alice", path="/home/alice/docs", entries=[{"t": "user", "n": "bob", "p": R_ | X_}])
    assert h.fs.q_ls("bob", "/home/alice/docs") == []
    with pytest.raises(E.PermissionDenied):  # bob no tiene w
        h.do("mkdir", user="bob", path="/home/alice/docs/x")
    # sólo el dueño cambia ACL
    with pytest.raises(E.PermissionDenied):
        h.do("setacl", user="bob", path="/home/alice/docs", entries=[])
    # ACL de grupo
    h.do("user_set", actor="admin", name="carol", groups=["devs"])
    h.do("setacl", user="alice", path="/home/alice", entries=[{"t": "group", "n": "devs", "p": R_ | X_}])
    h.do("setacl", user="alice", path="/home/alice/docs", entries=[{"t": "group", "n": "devs", "p": R_ | W_ | X_}])
    h.do("mkdir", user="carol", path="/home/alice/docs/carol_was_here")


def test_chmod_chown_rules(h):
    h.do("mkdir", user="alice", path="/home/alice/d")
    with pytest.raises(E.PermissionDenied):
        h.do("chmod", user="bob", path="/home/alice/d", mode=0o777)
    with pytest.raises(E.PermissionDenied):
        h.do("chown", user="alice", path="/home/alice/d", owner="bob")  # sólo admin cambia dueño
    h.do("chown", user="admin", path="/home/alice/d", owner="bob")
    assert h.fs.q_stat("admin", "/home/alice/d")["owner"] == "bob"


def test_rm_rmdir_mv(h):
    h.do("mkdir", user="alice", path="/home/alice/a/b", parents=True)
    with pytest.raises(E.NotEmpty):
        h.do("rmdir", user="alice", path="/home/alice/a")
    with pytest.raises(E.IsADirectory):
        h.do("rm", user="alice", path="/home/alice/a")
    h.put("alice", "/home/alice/a/f.txt")
    h.do("mv", user="alice", src="/home/alice/a/f.txt", dst="/home/alice/a/b/")
    assert [e["name"] for e in h.fs.q_ls("alice", "/home/alice/a/b")] == ["f.txt"]
    with pytest.raises(E.InvalidArgument):  # un dir dentro de sí mismo
        h.do("mv", user="alice", src="/home/alice/a", dst="/home/alice/a/b/a")
    h.do("rm", user="alice", path="/home/alice/a", recursive=True)
    with pytest.raises(E.NotFound):
        h.fs.q_stat("alice", "/home/alice/a")
    assert h.fs.pending_deletes, "los bloques del árbol borrado deben encolarse para GC"
    assert not h.fs.blocks


def test_write_commit_is_atomic_and_invisible_until_commit(h):
    s = h.do("begin_write", user="alice", path="/home/alice/big", sid="s1", repl=3, min_repl=2, bs=100, ttl=60, dek="d", mode="replace")
    h.do("alloc_blocks", user="alice", sid="s1", blocks=[{"id": "b1", "dns": ["d1", "d2", "d3"]}])
    with pytest.raises(E.NotFound):  # aún no visible
        h.fs.q_stat("alice", "/home/alice/big")
    h.do("commit_write", user="alice", sid="s1", size=80, layout=[{"id": "b1", "len": 80, "sha": "x", "reps": ["d1", "d2"]}])
    st = h.fs.q_stat("alice", "/home/alice/big")
    assert st["size"] == 80 and st["version"] == 1
    node, blocks = h.fs.q_file("alice", "/home/alice/big")
    assert blocks[0]["reps"] == ["d1", "d2"]
    assert s["sid"] not in h.fs.sessions


def test_commit_requires_min_replicas_and_valid_sizes(h):
    h.do("begin_write", user="alice", path="/home/alice/f", sid="s", repl=3, min_repl=2, bs=100, ttl=60, dek="d")
    h.do("alloc_blocks", user="alice", sid="s", blocks=[{"id": "b", "dns": ["d1", "d2", "d3"]}])
    with pytest.raises(E.InvalidArgument, match="mínimo"):
        h.do("commit_write", user="alice", sid="s", size=10, layout=[{"id": "b", "len": 10, "sha": "x", "reps": ["d1"]}])
    with pytest.raises(E.InvalidArgument, match="size"):
        h.do("commit_write", user="alice", sid="s", size=11, layout=[{"id": "b", "len": 10, "sha": "x", "reps": ["d1", "d2"]}])
    with pytest.raises(E.InvalidArgument, match="longitud"):
        h.do("commit_write", user="alice", sid="s", size=500, layout=[{"id": "b", "len": 500, "sha": "x", "reps": ["d1", "d2"]}])
    with pytest.raises(E.InvalidArgument):  # bloque ajeno
        h.do("commit_write", user="alice", sid="s", size=1, layout=[{"id": "zzz", "len": 1, "sha": "x", "reps": ["d1", "d2"]}])


def test_overwrite_replaces_content_and_gcs_old_blocks(h):
    _, old = h.put("alice", "/home/alice/f", sizes=(100, 100))
    r, new = h.put("alice", "/home/alice/f", sizes=(30,))
    assert r["version"] == 2 and h.fs.q_stat("alice", "/home/alice/f")["size"] == 30
    assert set(old) <= set(h.fs.pending_deletes)
    assert all(b not in h.fs.blocks for b in old)
    with pytest.raises(E.Exists):
        h.do("begin_write", user="alice", path="/home/alice/f", sid="x", repl=3, min_repl=2, bs=100, ttl=60, dek="d", overwrite=False)


def test_append_and_update_modes_with_optimistic_concurrency(h):
    _, ids = h.put("alice", "/home/alice/log", sizes=(100, 100, 40))
    # append: nuevo bloque al final
    s = h.do("begin_write", user="alice", path="/home/alice/log", sid="sa", mode="append", repl=3, min_repl=2, bs=100, ttl=60, dek="d")
    assert [b["id"] for b in s["blocks"]] == ids and s["version"] == 1
    h.do("alloc_blocks", user="alice", sid="sa", blocks=[{"id": "nb", "dns": ["d1", "d2"]}])
    r = h.do("commit_write", user="alice", sid="sa", size=100 + 100 + 40 + 25,
             layout=[*({"id": i} for i in ids), {"id": "nb", "len": 25, "sha": "x", "reps": ["d1", "d2"]}])  # fmt: skip
    assert r["version"] == 2
    # update (COW de un bloque): reutiliza ids[0], ids[2]; reemplaza ids[1]
    s = h.do("begin_write", user="alice", path="/home/alice/log", sid="su", mode="update", repl=3, min_repl=2, bs=100, ttl=60, dek="d")
    h.do("alloc_blocks", user="alice", sid="su", blocks=[{"id": "mid", "dns": ["d3", "d4"]}])
    # otro cliente confirma antes => conflicto
    s2 = h.do("begin_write", user="alice", path="/home/alice/log", sid="su2", mode="append", repl=3, min_repl=2, bs=100, ttl=60, dek="d")
    h.do("alloc_blocks", user="alice", sid="su2", blocks=[{"id": "n2", "dns": ["d1", "d2"]}])
    h.do("commit_write", user="alice", sid="su2", size=265 + 5,
         layout=[*({"id": i} for i in [*ids, "nb"]), {"id": "n2", "len": 5, "sha": "x", "reps": ["d1", "d2"]}])  # fmt: skip
    with pytest.raises(E.Conflict):
        h.do("commit_write", user="alice", sid="su", size=100 + 100 + 40 + 25,
             layout=[{"id": ids[0]}, {"id": "mid", "len": 100, "sha": "x", "reps": ["d3", "d4"]}, {"id": ids[2]}, {"id": "nb"}])  # fmt: skip
    assert s2["version"] == 2


def test_concurrent_replace_is_last_writer_wins_but_if_version_is_cas(h):
    h.put("alice", "/home/alice/c", sizes=(10,))
    sids = []
    for k in range(2):  # dos escritores abiertos a la vez
        s = h.do("begin_write", user="alice", path="/home/alice/c", sid=f"w{k}", repl=3, min_repl=2, bs=100, ttl=60, dek="d")
        h.do("alloc_blocks", user="alice", sid=f"w{k}", blocks=[{"id": f"x{k}", "dns": ["d1", "d2"]}])
        sids.append(s["sid"])
    h.do("commit_write", user="alice", sid="w0", size=7, layout=[{"id": "x0", "len": 7, "sha": "a", "reps": ["d1", "d2"]}])
    h.do("commit_write", user="alice", sid="w1", size=9, layout=[{"id": "x1", "len": 9, "sha": "b", "reps": ["d1", "d2"]}])
    assert h.fs.q_stat("alice", "/home/alice/c")["size"] == 9  # último gana
    # con CAS el segundo falla
    for k in range(2):
        h.do("begin_write", user="alice", path="/home/alice/c", sid=f"c{k}", repl=3, min_repl=2, bs=100, ttl=60, dek="d")
        h.do("alloc_blocks", user="alice", sid=f"c{k}", blocks=[{"id": f"y{k}", "dns": ["d1", "d2"]}])
    cur = h.fs.q_stat("alice", "/home/alice/c")["version"]
    h.do("commit_write", user="alice", sid="c0", size=1, if_version=cur, layout=[{"id": "y0", "len": 1, "sha": "a", "reps": ["d1", "d2"]}])
    with pytest.raises(E.Conflict):
        h.do("commit_write", user="alice", sid="c1", size=1, if_version=cur, layout=[{"id": "y1", "len": 1, "sha": "a", "reps": ["d1", "d2"]}])


def test_abort_and_session_expiry_free_blocks(h):
    h.do("begin_write", user="alice", path="/home/alice/g", sid="sx", repl=3, min_repl=2, bs=100, ttl=10, dek="d")
    h.do("alloc_blocks", user="alice", sid="sx", blocks=[{"id": "bx", "dns": ["d1", "d2"]}])
    assert "bx" in h.fs.blocks
    h.tick(30)
    assert h.fs.expiring(h.t)
    assert h.do("expire", grace=0)["sessions"] == 1
    assert "bx" not in h.fs.blocks and "bx" in h.fs.pending_deletes
    with pytest.raises(E.StaleHandle):
        h.do("commit_write", user="alice", sid="sx", size=1, layout=[])
    # abort explícito
    h.do("begin_write", user="alice", path="/home/alice/g", sid="sy", repl=3, min_repl=2, bs=100, ttl=60, dek="d")
    assert h.do("abort_write", user="alice", sid="sy")["aborted"]


def test_session_cannot_be_used_by_another_user(h):
    h.do("begin_write", user="alice", path="/home/alice/f", sid="s", repl=3, min_repl=2, bs=100, ttl=60, dek="d")
    with pytest.raises(E.PermissionDenied):
        h.do("alloc_blocks", user="bob", sid="s", blocks=[])
    with pytest.raises(E.PermissionDenied):
        h.do("begin_write", user="bob", path="/home/alice/zzz", sid="s2", repl=3, min_repl=2, bs=100, ttl=60, dek="d")


def test_locks_exclusive_shared_fencing_and_enforced_on_writes(h):
    h.put("alice", "/shared/doc", sizes=(10,))
    h.do("chmod", user="alice", path="/shared/doc", mode=0o666)
    a = h.do("lock_acquire", user="alice", path="/shared/doc", mode="exclusive", ttl=30, lock_id="LA")
    with pytest.raises(E.Locked):
        h.do("lock_acquire", user="bob", path="/shared/doc", mode="shared", ttl=30, lock_id="LB")
    with pytest.raises(E.Locked):  # bob no puede escribir mientras alice tiene el exclusivo
        h.do("begin_write", user="bob", path="/shared/doc", sid="sb", repl=3, min_repl=2, bs=100, ttl=60, dek="d")
    # alice sí, presentando su lock_id
    h.do("begin_write", user="alice", path="/shared/doc", sid="sa", repl=3, min_repl=2, bs=100, ttl=60, dek="d", lock_id="LA")
    h.do("abort_write", user="alice", sid="sa")
    h.do("lock_release", user="alice", lock_id="LA")
    b = h.do("lock_acquire", user="bob", path="/shared/doc", mode="shared", ttl=30, lock_id="LB")
    c = h.do("lock_acquire", user="carol", path="/shared/doc", mode="shared", ttl=30, lock_id="LC")
    assert a["fence"] < b["fence"] < c["fence"], "fencing token monótono"
    with pytest.raises(E.Locked):
        h.do("lock_acquire", user="alice", path="/shared/doc", mode="exclusive", ttl=30, lock_id="LA2")


def test_lock_lease_expires_deterministically(h):
    h.put("alice", "/home/alice/l", sizes=(10,))
    h.do("lock_acquire", user="alice", path="/home/alice/l", mode="exclusive", ttl=5, lock_id="L1")
    h.do("lock_renew", user="alice", lock_id="L1", ttl=5)
    h.tick(10)
    h.do("lock_acquire", user="alice", path="/home/alice/l", mode="exclusive", ttl=5, lock_id="L2")  # L1 venció
    with pytest.raises(E.NotFound):
        h.do("lock_renew", user="alice", lock_id="L1", ttl=5)


def test_rm_drops_locks_and_blocks_foreign_writers(h):
    h.put("alice", "/shared/l", sizes=(10,))
    h.do("chmod", user="alice", path="/shared/l", mode=0o666)
    h.do("lock_acquire", user="alice", path="/shared/l", mode="exclusive", ttl=50, lock_id="L1")
    with pytest.raises(E.Locked):
        h.do("begin_write", user="bob", path="/shared/l", sid="s", repl=3, min_repl=2, bs=100, ttl=5, dek="d")
    with pytest.raises(E.Locked):
        h.do("rm", user="bob", path="/shared/l")
    h.do("rm", user="alice", path="/shared/l", lock_id="L1")
    assert not h.fs.locks


def test_idempotent_replay_returns_same_result_and_error(h):
    r1 = h.do("mkdir", user="alice", path="/home/alice/idem", req_id="R1")
    r2 = h.do("mkdir", user="alice", path="/home/alice/idem", req_id="R1")  # reintento tras failover
    assert r1 == r2
    with pytest.raises(E.Exists):
        h.do("mkdir", user="alice", path="/home/alice/idem", req_id="R2")
    with pytest.raises(E.Exists):
        h.do("mkdir", user="alice", path="/home/alice/idem", req_id="R2")  # mismo error cacheado


def test_replica_update_gc_and_report(h):
    _, ids = h.put("alice", "/home/alice/r", sizes=(10,), reps=("d1", "d2"))
    alive = {"d1": True, "d2": False, "d3": True, "d4": True}.get
    rep = h.fs.replication_report(lambda d: alive(d, False))
    assert [u[0] for u in rep["under"]] == ids and rep["under"][0][1] == ["d1"]
    h.do("replica_update", adds=[(ids[0], "d3")])
    assert h.fs.blocks[ids[0]]["reps"] == ["d1", "d2", "d3"]
    # sobre-replicación: quitar d2 con borrado físico
    h.do("replica_update", adds=[(ids[0], "d4")], removes=[(ids[0], "d2", True)])
    assert "d2" not in h.fs.blocks[ids[0]]["reps"] and h.fs.pending_deletes[ids[0]]["dns"] == ["d2"]
    h.do("gc_ack", items=[(ids[0], "d2")])
    assert ids[0] not in h.fs.pending_deletes
    # jamás se elimina la última réplica salvo force
    h.do("replica_update", removes=[(ids[0], "d1", False), (ids[0], "d3", False), (ids[0], "d4", False)])
    assert len(h.fs.blocks[ids[0]]["reps"]) == 1


def test_users_tokens_version_and_totp_replay(h):
    v0 = h.fs.users["alice"]["ver"]
    h.do("user_set", actor="alice", name="alice", pw="nuevo")
    assert h.fs.users["alice"]["ver"] == v0 + 1  # invalida JWT previos
    with pytest.raises(E.PermissionDenied):
        h.do("user_set", actor="bob", name="alice", pw="hack")
    h.do("totp_set", actor="alice", name="alice", secret="wrapped")
    h.do("totp_use", name="alice", step=100)
    with pytest.raises(E.Unauthenticated):
        h.do("totp_use", name="alice", step=100)  # replay
    h.do("totp_use", name="alice", step=101)
    h.do("user_set", actor="admin", name="bob", enabled=False)
    with pytest.raises(E.Unauthenticated):
        h.do("mkdir", user="bob", path="/shared/x")


def test_apikeys(h):
    h.do("apikey_add", actor="alice", id="k1", hash="h", label="ci")
    with pytest.raises(E.PermissionDenied):
        h.do("apikey_revoke", actor="bob", id="k1")
    h.do("apikey_revoke", actor="alice", id="k1")
    assert "k1" not in h.fs.apikeys


def test_snapshot_restore_roundtrip_is_exact(h):
    h.put("alice", "/home/alice/f", sizes=(100, 20))
    h.do("lock_acquire", user="alice", path="/home/alice/f", mode="shared", ttl=500, lock_id="L")
    h.do("mkdir", user="bob", path="/home/bob/d")
    snap = h.fs.snapshot()
    other = MetadataStore()
    other.restore(snap)
    assert other.snapshot() == snap
    # y siguen evolucionando igual
    h2 = H(other)
    h2.i, h2.t = h.i, h.t
    for hh in (h, h2):
        hh.do("mkdir", user="alice", path="/home/alice/z", req_id="same")
        hh.do("lock_release", user="alice", lock_id="L", req_id="same2")
    assert h.fs.snapshot() == h2.fs.snapshot()


def test_determinism_two_replicas_same_log_same_state():
    """Propiedad central de la replicación: mismo log => mismo estado (fuzz)."""
    rnd = random.Random(42)
    ops = []
    users = ["alice", "bob", "carol", "admin"]
    paths = ["/home/alice/a", "/home/alice/a/b", "/shared/x", "/shared/y", "/home/bob/q", "/shared/x/z"]
    for n in range(400):
        u = rnd.choice(users)
        k = rnd.choice(["mkdir", "mkdir", "rm", "rmdir", "mv", "chmod", "lock", "unlock", "setacl"])
        p, q = rnd.choice(paths), rnd.choice(paths)
        cmd = {
            "mkdir": {"op": "mkdir", "user": u, "path": p, "parents": rnd.random() < 0.5},
            "rm": {"op": "rm", "user": u, "path": p, "recursive": rnd.random() < 0.5},
            "rmdir": {"op": "rmdir", "user": u, "path": p},
            "mv": {"op": "mv", "user": u, "src": p, "dst": q},
            "chmod": {"op": "chmod", "user": u, "path": p, "mode": rnd.choice([0o700, 0o755, 0o777])},
            "lock": {"op": "lock_acquire", "user": u, "path": p, "mode": "shared", "ttl": 20, "lock_id": f"L{n}"},
            "unlock": {"op": "lock_release", "user": u, "lock_id": f"L{rnd.randrange(n+1)}"},
            "setacl": {"op": "setacl", "user": u, "path": p, "entries": [{"t": "user", "n": rnd.choice(users), "p": 5}]},
        }[k]
        ops.append({**cmd, "ts": 2000 + n * 0.5, "req_id": f"r{n}"})
    a, b = H().setup().fs, H().setup().fs
    ia = ib = 100
    for c in ops:
        for fs in (a, b):
            try:
                fs.apply(ia, c)
            except E.DfshaError:
                pass
        ia += 1
        ib += 1
    assert a.snapshot() == b.snapshot()
    assert len(a.inodes) > 10  # el fuzz hizo cosas de verdad


def test_client_cannot_claim_replicas_on_unassigned_datanodes(h):
    h.do("begin_write", user="alice", path="/home/alice/f", sid="s", repl=3, min_repl=2, bs=100, ttl=60, dek="d")
    h.do("alloc_blocks", user="alice", sid="s", blocks=[{"id": "b", "dns": ["d1", "d2"]}])
    with pytest.raises(E.InvalidArgument, match="asignadas"):
        h.do("commit_write", user="alice", sid="s", size=10, layout=[{"id": "b", "len": 10, "sha": "x", "reps": ["d1", "d3"]}])
    # tras una reubicación, los nodos nuevos y los previos son válidos
    h.do("realloc_block", user="alice", sid="s", block_id="b", dns=["d3", "d4"])
    h.do("commit_write", user="alice", sid="s", size=10, layout=[{"id": "b", "len": 10, "sha": "x", "reps": ["d1", "d3", "d4"]}])
