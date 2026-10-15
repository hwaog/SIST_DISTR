"""Máquina de estados replicada del ControlNode (metadatos de DFSha).

Contiene, replicado por Raft en los N ControlNodes:

* el **espacio de nombres** jerárquico (inodos, permisos POSIX + ACL),
* el **mapa archivo → bloques → réplicas** (qué DataNodes tienen cada bloque),
* las **sesiones de escritura** (subidas en curso, invisibles hasta ``commit``),
* **locks** compartidos/exclusivos con *lease* y *fencing token*,
* **usuarios**, grupos, API keys y secretos 2FA (envueltos),
* el **registro de DataNodes** y la cola de bloques a borrar (GC).

Reglas de diseño (imprescindibles para la corrección con Raft)
--------------------------------------------------------------
1. ``apply`` es **determinista**: no lee reloj, azar ni red.  El líder inyecta
   en cada comando todo lo no determinista (``ts``, ids, claves, ubicaciones).
2. Todas las comprobaciones de permisos/invariantes viven en ``apply``: dos
   comandos concurrentes se serializan por el log y el segundo ve el efecto
   del primero.
3. Los errores de dominio se lanzan como ``DfshaError``; Raft los entrega al
   solicitante idéntico en todas las réplicas.
4. Idempotencia: cada comando lleva un ``req_id``; si el líder cae después de
   comprometer pero antes de responder, el reintento obtiene el *mismo*
   resultado sin re-ejecutar (tabla ``dedup`` acotada, incluida en snapshots).
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from typing import Any

from ..common.errors import (
    Conflict,
    DfshaError,
    Exists,
    InvalidArgument,
    IsADirectory,
    Locked,
    NotADirectory,
    NotEmpty,
    NotFound,
    PermissionDenied,
    StaleHandle,
    Unauthenticated,
    from_dict,
)
from ..common.util import normpath, split_path

ROOT = 1
R, W, X = 4, 2, 1
NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
DEDUP_SIZE = 20_000
MAX_BLOCKS_PER_SESSION = 2_000_000
DIR_MODE = 0o755
FILE_MODE = 0o644
EVERYONE = "users"  # grupo al que pertenece todo usuario no-admin


class MetadataStore:
    """Estado + transiciones.  Las consultas (``q_*``) no mutan el estado."""

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self.inodes: dict[int, dict] = {}
        self.next_inode = ROOT + 1
        self.users: dict[str, dict] = {}
        self.apikeys: dict[str, dict] = {}
        self.blocks: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.locks: dict[str, dict] = {}
        self.fence = 0
        self.datanodes: dict[str, dict] = {}
        self.pending_deletes: dict[str, dict] = {}
        self.dedup: OrderedDict[str, tuple[str, Any]] = OrderedDict()
        self.clock = 0.0
        self.applied_index = 0
        self.inodes[ROOT] = self._new_dir(ROOT, ROOT, "", "admin", "admin", 0o755, 0.0)
        self._lock_idx: dict[int, set[str]] = {}

    # ------------------------------------------------------------------ plumbing
    @staticmethod
    def _new_dir(iid: int, parent: int, name: str, owner: str, group: str, mode: int, ts: float) -> dict:
        return {
            "id": iid, "type": "dir", "parent": parent, "name": name, "owner": owner,
            "group": group, "mode": mode, "acl": [], "ctime": ts, "mtime": ts, "ch": {},
        }  # fmt: skip

    def apply(self, index: int, cmd: dict) -> Any:
        self.applied_index = index
        rid = cmd.get("req_id")
        if rid and rid in self.dedup:
            kind, val = self.dedup[rid]
            if kind == "err":
                raise from_dict(val)
            return val
        self.clock = max(self.clock, float(cmd.get("ts", 0.0)))
        fn = getattr(self, "_op_" + cmd["op"], None)
        if fn is None:
            raise InvalidArgument(f"operación desconocida: {cmd['op']}")
        try:
            res = fn(cmd)
        except DfshaError as e:
            self._remember(rid, ("err", e.to_dict()))
            raise
        self._remember(rid, ("ok", res))
        return res

    def _remember(self, rid: str | None, val: tuple[str, Any]) -> None:
        if not rid:
            return
        self.dedup[rid] = val
        while len(self.dedup) > DEDUP_SIZE:
            self.dedup.popitem(last=False)

    def snapshot(self) -> bytes:
        return json.dumps(
            {
                "inodes": list(self.inodes.values()),
                "next_inode": self.next_inode,
                "users": self.users,
                "apikeys": self.apikeys,
                "blocks": self.blocks,
                "sessions": self.sessions,
                "locks": self.locks,
                "fence": self.fence,
                "datanodes": self.datanodes,
                "pending_deletes": self.pending_deletes,
                "dedup": list(self.dedup.items()),
                "clock": self.clock,
            },
            separators=(",", ":"),
        ).encode()

    def restore(self, data: bytes) -> None:
        o = json.loads(data)
        self._reset()
        self.inodes = {i["id"]: i for i in o["inodes"]}
        self.next_inode = o["next_inode"]
        self.users, self.apikeys, self.blocks = o["users"], o["apikeys"], o["blocks"]
        self.sessions, self.locks, self.fence = o["sessions"], o["locks"], o["fence"]
        self.datanodes, self.pending_deletes = o["datanodes"], o["pending_deletes"]
        self.dedup = OrderedDict((k, tuple(v)) for k, v in o["dedup"])  # type: ignore[arg-type]
        self.clock = o["clock"]
        self._lock_idx = {}
        for lid, lk in self.locks.items():
            self._lock_idx.setdefault(lk["inode"], set()).add(lid)

    # ----------------------------------------------------------------- seguridad
    def _user(self, name: str | None) -> dict:
        u = self.users.get(name or "")
        if not u or not u.get("enabled", True):
            raise Unauthenticated(f"usuario desconocido o deshabilitado: {name!r}")
        return u

    def _admin(self, name: str | None) -> dict:
        u = self._user(name)
        if not u["admin"]:
            raise PermissionDenied("se requiere rol administrador")
        return u

    @staticmethod
    def _allowed(inode: dict, u: dict, want: int) -> bool:
        if u["admin"]:
            return True
        mode = inode["mode"]
        if inode["owner"] == u["name"]:
            return ((mode >> 6) & 7) & want == want
        groups = set(u["groups"])
        for ace in inode["acl"]:
            if ace["t"] == "user" and ace["n"] == u["name"]:
                return ace["p"] & want == want
        matched, perms = False, 0
        if inode["group"] in groups:
            matched, perms = True, perms | ((mode >> 3) & 7)
        for ace in inode["acl"]:
            if ace["t"] == "group" and ace["n"] in groups:
                matched, perms = True, perms | ace["p"]
        if matched:
            return perms & want == want
        return (mode & 7) & want == want

    def _need(self, inode: dict, u: dict, want: int, what: str = "") -> None:
        if not self._allowed(inode, u, want):
            raise PermissionDenied(f"permiso denegado: {what or inode['name'] or '/'}")

    # ------------------------------------------------------------ resolución de paths
    def _lookup(self, path: str, u: dict, *, follow_x: bool = True) -> dict:
        norm = normpath(path)
        cur = self.inodes[ROOT]
        for name in split_path(norm):
            if cur["type"] != "dir":
                raise NotADirectory(norm)
            if follow_x:
                self._need(cur, u, X, norm)
            cid = cur["ch"].get(name)
            if cid is None:
                raise NotFound(norm)
            cur = self.inodes[cid]
        return cur

    def _lookup_parent(self, path: str, u: dict) -> tuple[dict, str]:
        norm = normpath(path)
        parts = split_path(norm)
        if not parts:
            raise InvalidArgument("la raíz no tiene directorio padre")
        parent = self._lookup("/" + "/".join(parts[:-1]), u)
        if parent["type"] != "dir":
            raise NotADirectory(norm)
        return parent, parts[-1]

    def _path_of(self, inode: dict) -> str:
        parts = []
        cur = inode
        while cur["id"] != ROOT:
            parts.append(cur["name"])
            cur = self.inodes[cur["parent"]]
        return "/" + "/".join(reversed(parts))

    def _stat(self, i: dict) -> dict:
        d = {
            "id": i["id"], "type": i["type"], "name": i["name"], "owner": i["owner"],
            "group": i["group"], "mode": i["mode"], "mtime": i["mtime"], "ctime": i["ctime"],
            "acl": [dict(a) for a in i["acl"]],
        }  # fmt: skip
        if i["type"] == "file":
            d.update(size=i["size"], version=i["ver"], repl=i["repl"], block_size=i["bs"], nblocks=len(i["blocks"]))
        else:
            d.update(size=0, nchildren=len(i["ch"]))
        return d

    # ------------------------------------------------------------------ inodos
    def _alloc_inode(self) -> int:
        i = self.next_inode
        self.next_inode += 1
        return i

    def _link(self, parent: dict, name: str, inode: dict, ts: float) -> None:
        parent["ch"][name] = inode["id"]
        parent["mtime"] = ts
        self.inodes[inode["id"]] = inode

    def _unlink(self, parent: dict, name: str, ts: float) -> dict:
        iid = parent["ch"].pop(name)
        parent["mtime"] = ts
        return self.inodes[iid]

    def _schedule_delete(self, block_id: str, dns: list[str], grace: float) -> None:
        e = self.pending_deletes.setdefault(block_id, {"dns": [], "nb": self.clock + grace})
        for d in dns:
            if d not in e["dns"]:
                e["dns"].append(d)

    def _drop_block(self, block_id: str, grace: float) -> None:
        b = self.blocks.pop(block_id, None)
        if b is not None:
            self._schedule_delete(block_id, list(b["reps"]) or list(b.get("alloc", [])), grace)

    def _drop_inode(self, inode: dict, grace: float) -> None:
        """Libera un inodo ya desvinculado: bloques a GC, locks, índice."""
        stack = [inode]
        while stack:
            cur = stack.pop()
            if cur["type"] == "dir":
                stack.extend(self.inodes[c] for c in cur["ch"].values())
            else:
                for bid in cur["blocks"]:
                    self._drop_block(bid, grace)
            for lid in list(self._lock_idx.get(cur["id"], ())):
                self._del_lock(lid)
            self.inodes.pop(cur["id"], None)

    def _is_ancestor(self, anc: dict, node: dict) -> bool:
        cur = node
        while True:
            if cur["id"] == anc["id"]:
                return True
            if cur["id"] == ROOT:
                return False
            cur = self.inodes[cur["parent"]]

    # --------------------------------------------------------------------- locks
    def _del_lock(self, lid: str) -> None:
        lk = self.locks.pop(lid, None)
        if lk:
            s = self._lock_idx.get(lk["inode"])
            if s:
                s.discard(lid)
                if not s:
                    self._lock_idx.pop(lk["inode"], None)

    def _live_locks(self, iid: int) -> list[tuple[str, dict]]:
        out = []
        for lid in list(self._lock_idx.get(iid, ())):
            lk = self.locks[lid]
            if lk["expires"] <= self.clock:
                self._del_lock(lid)
            else:
                out.append((lid, lk))
        return out

    def _check_write_lock(self, iid: int, lock_id: str | None) -> None:
        """Escribir exige no haber locks ajenos (o presentar el exclusivo propio)."""
        for lid, lk in self._live_locks(iid):
            if lid != lock_id or lk["mode"] != "x":
                raise Locked(f"archivo bloqueado por {lk['user']} ({'exclusivo' if lk['mode']=='x' else 'compartido'})")

    # =============================================================== OPS: usuarios
    def _op_bootstrap_admin(self, c: dict) -> dict:
        if self.users:
            return {"created": False}
        ts = self.clock
        self.users["admin"] = {
            "name": "admin", "pw": c["pw"], "groups": ["admin", EVERYONE], "admin": True,
            "enabled": True, "ver": 1, "totp": None, "totp_step": 0, "created": ts,
        }  # fmt: skip
        root = self.inodes[ROOT]
        for name, mode, group in (("home", 0o755, "admin"), ("shared", 0o775, EVERYONE)):
            self._link(root, name, self._new_dir(self._alloc_inode(), ROOT, name, "admin", group, mode, ts), ts)
        home = self.inodes[root["ch"]["home"]]
        self._link(home, "admin", self._new_dir(self._alloc_inode(), home["id"], "admin", "admin", "admin", 0o750, ts), ts)
        return {"created": True}

    def _op_user_add(self, c: dict) -> dict:
        self._admin(c["actor"])
        name = c["name"]
        if not NAME_RE.match(name):
            raise InvalidArgument("nombre de usuario inválido (a-z, 0-9, _ y -; máx. 32)")
        if name in self.users:
            raise Exists(f"usuario {name}")
        gset = {name, EVERYONE, *c.get("groups", [])}
        if c.get("admin"):
            gset.add("admin")
        groups = sorted(gset)
        self.users[name] = {
            "name": name, "pw": c["pw"], "groups": groups, "admin": bool(c.get("admin")),
            "enabled": True, "ver": 1, "totp": None, "totp_step": 0, "created": self.clock,
        }  # fmt: skip
        home = self.inodes[self.inodes[ROOT]["ch"]["home"]]
        if name not in home["ch"]:
            self._link(home, name, self._new_dir(self._alloc_inode(), home["id"], name, name, name, 0o750, self.clock), self.clock)
        return {"user": name, "groups": groups}

    def _op_user_del(self, c: dict) -> dict:
        self._admin(c["actor"])
        name = c["name"]
        if name not in self.users:
            raise NotFound(f"usuario {name}")
        if name == c["actor"]:
            raise InvalidArgument("un administrador no puede eliminarse a sí mismo")
        del self.users[name]
        for kid in [k for k, v in self.apikeys.items() if v["user"] == name]:
            del self.apikeys[kid]
        return {"deleted": name}

    def _op_user_set(self, c: dict) -> dict:
        """Cambia clave / grupos / estado.  Uno mismo (clave) o un admin."""
        actor, name = self._user(c["actor"]), c["name"]
        if name not in self.users:
            raise NotFound(f"usuario {name}")
        u = self.users[name]
        is_self = actor["name"] == name
        if "pw" in c:
            if not (is_self or actor["admin"]):
                raise PermissionDenied("sólo el propio usuario o un admin cambian la clave")
            u["pw"] = c["pw"]
            u["ver"] += 1  # invalida todas las sesiones previas
        if "groups" in c:
            self._admin(c["actor"])
            u["groups"] = sorted({name, EVERYONE, *c["groups"]} | ({"admin"} if u["admin"] else set()))
        if "enabled" in c:
            self._admin(c["actor"])
            u["enabled"] = bool(c["enabled"])
            u["ver"] += 1
        return {"user": name}

    def _op_totp_set(self, c: dict) -> dict:
        actor, name = self._user(c["actor"]), c["name"]
        if not (actor["name"] == name or actor["admin"]):
            raise PermissionDenied("sólo el propio usuario o un admin gestionan el 2FA")
        u = self.users.get(name)
        if not u:
            raise NotFound(f"usuario {name}")
        u["totp"] = c.get("secret")  # envuelto; None => desactivar
        u["totp_step"] = 0
        u["ver"] += 1
        return {"user": name, "enabled": bool(c.get("secret"))}

    def _op_totp_use(self, c: dict) -> dict:
        """Marca un código TOTP como usado: impide reutilizar el mismo step (replay)."""
        u = self._user(c["name"])
        if c["step"] <= u["totp_step"]:
            raise Unauthenticated("código 2FA ya utilizado")
        u["totp_step"] = c["step"]
        return {}

    def _op_apikey_add(self, c: dict) -> dict:
        actor = self._user(c["actor"])
        target = c.get("for") or actor["name"]
        if target != actor["name"]:
            self._admin(c["actor"])
        self._user(target)
        self.apikeys[c["id"]] = {"id": c["id"], "user": target, "hash": c["hash"], "label": c.get("label", ""), "created": self.clock}
        return {"id": c["id"], "user": target}

    def _op_apikey_revoke(self, c: dict) -> dict:
        actor = self._user(c["actor"])
        k = self.apikeys.get(c["id"])
        if not k:
            raise NotFound("API key")
        if k["user"] != actor["name"] and not actor["admin"]:
            raise PermissionDenied("API key ajena")
        del self.apikeys[c["id"]]
        return {"revoked": c["id"]}

    # ============================================================ OPS: espacio de nombres
    def _op_mkdir(self, c: dict) -> dict:
        u = self._user(c["user"])
        norm = normpath(c["path"])
        parts = split_path(norm)
        if not parts:
            if c.get("parents"):
                return self._stat(self.inodes[ROOT])
            raise Exists("/")
        cur = self.inodes[ROOT]
        created = False
        for i, name in enumerate(parts):
            if cur["type"] != "dir":
                raise NotADirectory("/" + "/".join(parts[:i]))
            self._need(cur, u, X, "/" + "/".join(parts[: i + 1]))
            cid = cur["ch"].get(name)
            last = i == len(parts) - 1
            if cid is not None:
                cur = self.inodes[cid]
                if last and not c.get("parents"):
                    raise Exists(norm)
                if last and cur["type"] != "dir":
                    raise Exists(norm)
                continue
            if not last and not c.get("parents"):
                raise NotFound("/" + "/".join(parts[: i + 1]))
            self._need(cur, u, W, "/" + "/".join(parts[:i]) or "/")
            d = self._new_dir(self._alloc_inode(), cur["id"], name, u["name"], u["name"], c.get("mode", DIR_MODE), self.clock)
            self._link(cur, name, d, self.clock)
            cur, created = d, True
        return {**self._stat(cur), "created": created}

    def _op_rmdir(self, c: dict) -> dict:
        u = self._user(c["user"])
        parent, name = self._lookup_parent(c["path"], u)
        cid = parent["ch"].get(name)
        if cid is None:
            raise NotFound(c["path"])
        node = self.inodes[cid]
        if node["type"] != "dir":
            raise NotADirectory(c["path"])
        if node["ch"]:
            raise NotEmpty(c["path"])
        self._need(parent, u, W | X, c["path"])
        self._unlink(parent, name, self.clock)
        self._drop_inode(node, 0)
        return {"removed": normpath(c["path"])}

    def _op_rm(self, c: dict) -> dict:
        u = self._user(c["user"])
        parent, name = self._lookup_parent(c["path"], u)
        cid = parent["ch"].get(name)
        if cid is None:
            raise NotFound(c["path"])
        node = self.inodes[cid]
        self._need(parent, u, W | X, c["path"])
        grace = float(c.get("grace", 120))
        if node["type"] == "dir":
            if not c.get("recursive"):
                raise IsADirectory(f"{c['path']} (use -r para borrar directorios)")
            stack = [node]
            while stack:  # se exige w+x en cada directorio del subárbol
                d = stack.pop()
                self._need(d, u, W | X, self._path_of(d))
                stack.extend(self.inodes[x] for x in d["ch"].values() if self.inodes[x]["type"] == "dir")
        else:
            self._check_write_lock(node["id"], c.get("lock_id"))
        self._unlink(parent, name, self.clock)
        self._drop_inode(node, grace)
        return {"removed": normpath(c["path"])}

    def _op_mv(self, c: dict) -> dict:
        u = self._user(c["user"])
        sp, sname = self._lookup_parent(c["src"], u)
        sid = sp["ch"].get(sname)
        if sid is None:
            raise NotFound(c["src"])
        node = self.inodes[sid]
        dp, dname = self._lookup_parent(c["dst"], u)
        existing_id = dp["ch"].get(dname)
        if existing_id is not None and self.inodes[existing_id]["type"] == "dir" and node["id"] != existing_id:
            dp, dname = self.inodes[existing_id], sname  # mv a/f b/  => b/f
            existing_id = dp["ch"].get(dname)
        self._need(sp, u, W | X, c["src"])
        self._need(dp, u, W | X, c["dst"])
        if node["type"] == "dir" and self._is_ancestor(node, dp):
            raise InvalidArgument("no se puede mover un directorio dentro de sí mismo")
        if existing_id == node["id"]:
            return self._stat(node)
        grace = float(c.get("grace", 120))
        if existing_id is not None:
            ex = self.inodes[existing_id]
            if ex["type"] == "dir":
                if node["type"] != "dir":
                    raise IsADirectory(c["dst"])
                if ex["ch"]:
                    raise NotEmpty(c["dst"])
            elif node["type"] == "dir":
                raise NotADirectory(c["dst"])
            else:
                self._check_write_lock(ex["id"], None)
            self._unlink(dp, dname, self.clock)
            self._drop_inode(ex, grace)
        self._unlink(sp, sname, self.clock)
        node["name"], node["parent"], node["mtime"] = dname, dp["id"], self.clock
        dp["ch"][dname] = node["id"]
        dp["mtime"] = self.clock
        return self._stat(node)

    def _op_chmod(self, c: dict) -> dict:
        u = self._user(c["user"])
        node = self._lookup(c["path"], u)
        if not (u["admin"] or node["owner"] == u["name"]):
            raise PermissionDenied("sólo el dueño o un admin cambian permisos")
        if not isinstance(c["mode"], int) or not 0 <= c["mode"] <= 0o777:
            raise InvalidArgument("modo inválido")
        node["mode"], node["ctime"] = c["mode"], self.clock
        return self._stat(node)

    def _op_chown(self, c: dict) -> dict:
        u = self._user(c["user"])
        node = self._lookup(c["path"], u)
        if c.get("owner"):
            self._admin(c["user"])
            self._user(c["owner"])
            node["owner"] = c["owner"]
        if c.get("group"):
            if not (u["admin"] or (node["owner"] == u["name"] and c["group"] in u["groups"])):
                raise PermissionDenied("sólo el dueño (miembro del grupo) o un admin cambian el grupo")
            node["group"] = c["group"]
        node["ctime"] = self.clock
        return self._stat(node)

    def _op_setacl(self, c: dict) -> dict:
        u = self._user(c["user"])
        node = self._lookup(c["path"], u)
        if not (u["admin"] or node["owner"] == u["name"]):
            raise PermissionDenied("sólo el dueño o un admin cambian la ACL")
        acl = []
        for e in c["entries"]:
            if e["t"] not in ("user", "group") or not isinstance(e["p"], int) or not 0 <= e["p"] <= 7:
                raise InvalidArgument(f"entrada ACL inválida: {e}")
            if e["t"] == "user" and e["n"] not in self.users:
                raise NotFound(f"usuario {e['n']}")
            acl.append({"t": e["t"], "n": e["n"], "p": e["p"]})
        node["acl"], node["ctime"] = acl, self.clock
        return self._stat(node)

    def _op_setrepl(self, c: dict) -> dict:
        u = self._user(c["user"])
        node = self._lookup(c["path"], u)
        if node["type"] != "file":
            raise IsADirectory(c["path"])
        self._need(node, u, W, c["path"])
        if not 1 <= c["repl"] <= 16:
            raise InvalidArgument("replicación fuera de rango [1,16]")
        node["repl"] = c["repl"]
        for bid in node["blocks"]:
            self.blocks[bid]["repl"] = c["repl"]
        return self._stat(node)

    # ================================================================== OPS: archivos
    def _op_begin_write(self, c: dict) -> dict:
        u = self._user(c["user"])
        mode = c.get("mode", "replace")
        if mode not in ("replace", "append", "update"):
            raise InvalidArgument("modo de escritura inválido")
        sid = c["sid"]
        if sid in self.sessions:
            raise Exists("sesión de escritura duplicada")
        parent, name = self._lookup_parent(c["path"], u)
        cid = parent["ch"].get(name)
        inode_id, base_ver, existing_blocks = None, 0, []
        dek = c["dek"]
        if cid is not None:
            node = self.inodes[cid]
            if node["type"] == "dir":
                raise IsADirectory(c["path"])
            if mode == "replace" and not c.get("overwrite", True):
                raise Exists(c["path"])
            self._need(node, u, W, c["path"])
            self._check_write_lock(node["id"], c.get("lock_id"))
            inode_id, base_ver, dek = node["id"], node["ver"], node["dek"]
            if mode != "replace":
                existing_blocks = [self._block_view(self.blocks[b]) for b in node["blocks"]]
        else:
            if mode != "replace":
                raise NotFound(c["path"])
            self._need(parent, u, W | X, c["path"])
        if len(self.sessions) > 200_000:
            raise InvalidArgument("demasiadas sesiones de escritura abiertas")
        repl = int(c["repl"])
        if not 1 <= repl <= 16:
            raise InvalidArgument("replicación fuera de rango [1,16]")
        self.sessions[sid] = {
            "sid": sid, "user": u["name"], "path": normpath(c["path"]), "mode": mode, "inode": inode_id,
            "base_ver": base_ver, "repl": repl if inode_id is None else self.inodes[inode_id]["repl"],
            "min_repl": int(c["min_repl"]), "bs": int(c["bs"]) if inode_id is None else self.inodes[inode_id]["bs"],
            "dek": dek, "overwrite": bool(c.get("overwrite", True)), "file_mode": c.get("file_mode", FILE_MODE),
            "blocks": [], "alloc": {}, "expires": self.clock + float(c["ttl"]), "created": self.clock,
        }  # fmt: skip
        s = self.sessions[sid]
        return {
            "sid": sid, "inode": inode_id, "version": base_ver, "repl": s["repl"], "block_size": s["bs"],
            "min_repl": s["min_repl"], "blocks": existing_blocks, "expires": s["expires"],
        }  # fmt: skip

    @staticmethod
    def _block_view(b: dict) -> dict:
        return {"id": b["id"], "len": b["len"], "sha": b["sha"], "reps": list(b["reps"])}

    def _session(self, sid: str, user: str) -> dict:
        s = self.sessions.get(sid)
        if s is None or s["expires"] <= self.clock:
            raise StaleHandle("sesión de escritura inexistente o expirada")
        if s["user"] != user:
            raise PermissionDenied("sesión de otro usuario")
        return s

    def _op_alloc_blocks(self, c: dict) -> dict:
        s = self._session(c["sid"], c["user"])
        if len(s["blocks"]) + len(c["blocks"]) > MAX_BLOCKS_PER_SESSION:
            raise InvalidArgument("demasiados bloques en una sesión")
        for b in c["blocks"]:
            for dn in b["dns"]:
                if dn not in self.datanodes:
                    raise InvalidArgument(f"DataNode desconocido: {dn}")
            self.blocks[b["id"]] = {
                "id": b["id"], "state": "pending", "sid": s["sid"], "file": None, "len": 0, "sha": "",
                "reps": [], "alloc": list(b["dns"]), "repl": s["repl"], "gen": 1,
            }  # fmt: skip
            s["blocks"].append(b["id"])
            s["alloc"][b["id"]] = list(b["dns"])
        s["expires"] = max(s["expires"], self.clock + float(c.get("ttl", 0)))
        return {"allocated": len(c["blocks"])}

    def _op_realloc_block(self, c: dict) -> dict:
        s = self._session(c["sid"], c["user"])
        bid = c["block_id"]
        if bid not in s["blocks"]:
            raise NotFound(f"bloque {bid[:8]} en la sesión")
        for dn in c["dns"]:
            if dn not in self.datanodes:
                raise InvalidArgument(f"DataNode desconocido: {dn}")
        prev = s["alloc"].get(bid, [])
        s["alloc"][bid] = list(c["dns"])
        self.blocks[bid]["alloc"] = sorted({*c["dns"], *prev})  # GC alcanzará intentos fallidos
        return {"ok": True}

    def _op_renew_session(self, c: dict) -> dict:
        s = self._session(c["sid"], c["user"])
        s["expires"] = self.clock + float(c["ttl"])
        return {"expires": s["expires"]}

    def _op_abort_write(self, c: dict) -> dict:
        s = self.sessions.get(c["sid"])
        if s is None:
            return {"aborted": False}
        if s["user"] != c["user"] and not self._user(c["user"])["admin"]:
            raise PermissionDenied("sesión de otro usuario")
        self._release_session(s, float(c.get("grace", 0)))
        return {"aborted": True}

    def _release_session(self, s: dict, grace: float, keep: set[str] | None = None) -> None:
        keep = keep or set()
        for bid in s["blocks"]:
            if bid in keep:
                continue
            b = self.blocks.pop(bid, None)
            targets = sorted({*s["alloc"].get(bid, []), *(b["alloc"] if b else []), *(b["reps"] if b else [])})
            self._schedule_delete(bid, targets, grace)
        self.sessions.pop(s["sid"], None)

    def _op_commit_write(self, c: dict) -> dict:
        u = self._user(c["user"])
        s = self._session(c["sid"], c["user"])
        grace = float(c.get("grace", 120))
        layout = c["layout"]
        new_ids = set(s["blocks"])
        parent, name = self._lookup_parent(s["path"], u)
        cid = parent["ch"].get(name)

        # ---- resolver el inodo destino y comprobar concurrencia -------------------
        # replace : último-escritor-gana, salvo que se pida CAS con if_version.
        # append/update : control optimista; la versión base debe seguir vigente.
        iv = c.get("if_version")
        if s["mode"] == "replace":
            if cid is None:
                node = None
                self._need(parent, u, W | X, s["path"])
                if iv not in (None, 0):
                    raise Conflict("if_version especificado pero el archivo no existe")
            else:
                node = self.inodes[cid]
                if node["type"] == "dir":
                    raise IsADirectory(s["path"])
                if not s["overwrite"] or iv == 0:
                    raise Exists(s["path"])
                if iv is not None and node["ver"] != iv:
                    raise Conflict(f"if_version={iv} pero la versión actual es {node['ver']}")
                self._need(node, u, W, s["path"])
        else:
            node = self.inodes.get(s["inode"])
            if node is None or cid != s["inode"]:
                raise StaleHandle("el archivo fue borrado o renombrado durante la escritura")
            if node["ver"] != s["base_ver"]:
                raise Conflict(f"versión {node['ver']} != base {s['base_ver']} (otro cliente confirmó antes)")
            if iv is not None and node["ver"] != iv:
                raise Conflict(f"if_version={iv} pero la versión actual es {node['ver']}")
            self._need(node, u, W, s["path"])
        if node is not None:
            self._check_write_lock(node["id"], c.get("lock_id"))

        # ---- validar el layout ----------------------------------------------------
        old_blocks = list(node["blocks"]) if node else []
        reusable = set(old_blocks) if s["mode"] != "replace" else set()
        total, seen = 0, set()
        for ent in layout:
            bid = ent["id"]
            if bid in seen:
                raise InvalidArgument("bloque repetido en el layout")
            seen.add(bid)
            if bid in new_ids:
                reps = list(dict.fromkeys(ent["reps"]))
                allowed = set(self.blocks[bid].get("alloc", ())) | set(s["alloc"].get(bid, ()))
                if not set(reps) <= allowed:  # el cliente sólo puede declarar réplicas en nodos que se le asignaron
                    raise InvalidArgument(f"bloque {bid[:8]}: réplicas declaradas que no fueron asignadas")
                if len(reps) < s["min_repl"]:
                    raise InvalidArgument(f"bloque {bid}: {len(reps)} réplicas confirmadas < mínimo {s['min_repl']}")
                if any(r not in self.datanodes for r in reps):
                    raise InvalidArgument("réplica en DataNode desconocido")
                if not 0 < int(ent["len"]) <= s["bs"]:
                    raise InvalidArgument(f"longitud de bloque inválida: {ent['len']}")
                total += int(ent["len"])
            elif bid in reusable:
                total += self.blocks[bid]["len"]
            else:
                raise InvalidArgument(f"el bloque {bid} no pertenece a la sesión ni es reutilizable")
        if total != int(c["size"]):
            raise InvalidArgument(f"size={c['size']} no coincide con la suma de bloques ({total})")

        # ---- aplicar (todo o nada: ya no hay errores posibles) ---------------------
        if node is None:
            node = {
                "id": self._alloc_inode(), "type": "file", "parent": parent["id"], "name": name,
                "owner": u["name"], "group": u["name"], "mode": s["file_mode"], "acl": [],
                "ctime": self.clock, "mtime": self.clock, "size": 0, "ver": 0, "repl": s["repl"],
                "bs": s["bs"], "blocks": [], "dek": s["dek"],
            }  # fmt: skip
            self._link(parent, name, node, self.clock)
        for ent in layout:
            bid = ent["id"]
            if bid in new_ids:
                b = self.blocks[bid]
                b.update(state="ok", file=node["id"], len=int(ent["len"]), sha=ent["sha"], reps=list(dict.fromkeys(ent["reps"])), repl=node["repl"])
                b.pop("alloc", None)
                b.pop("sid", None)
        keep = {e["id"] for e in layout}
        for bid in old_blocks:
            if bid not in keep:
                self._drop_block(bid, grace)
        self._release_session(s, grace, keep=keep)  # bloques de la sesión que no entraron
        if s["mode"] == "replace":
            node["dek"] = s["dek"]  # todo el contenido anterior se descarta: se adopta la DEK de la sesión
        node.update(blocks=[e["id"] for e in layout], size=total, mtime=self.clock)
        node["ver"] += 1
        return {"inode": node["id"], "version": node["ver"], "size": total, "path": s["path"]}

    # ==================================================================== OPS: réplicas
    def _op_replica_update(self, c: dict) -> dict:
        added = removed = 0
        for bid, dn in c.get("adds", []):
            b = self.blocks.get(bid)
            if b and b["state"] == "ok" and dn in self.datanodes and dn not in b["reps"]:
                b["reps"].append(dn)
                added += 1
        for bid, dn, physical in c.get("removes", []):
            b = self.blocks.get(bid)
            if b and dn in b["reps"] and (len(b["reps"]) > 1 or c.get("force")):
                b["reps"].remove(dn)
                removed += 1
                if physical:
                    self._schedule_delete(bid, [dn], 0)
            elif not b and physical:
                self._schedule_delete(bid, [dn], 0)
        return {"added": added, "removed": removed}

    def _op_gc_ack(self, c: dict) -> dict:
        for bid, dn in c["items"]:
            e = self.pending_deletes.get(bid)
            if e:
                if dn in e["dns"]:
                    e["dns"].remove(dn)
                if not e["dns"]:
                    del self.pending_deletes[bid]
        return {}

    def _op_gc_forget(self, c: dict) -> dict:
        """Olvida entradas de GC cuyos DataNodes dejaron de existir."""
        for bid in c["blocks"]:
            self.pending_deletes.pop(bid, None)
        return {}

    def _op_dn_register(self, c: dict) -> dict:
        prev = self.datanodes.get(c["dn"], {})
        self.datanodes[c["dn"]] = {
            "id": c["dn"], "zone": c.get("zone", "default"), "public_url": c["public_url"],
            "internal_url": c["internal_url"], "state": prev.get("state", "active"),
            "registered": prev.get("registered", self.clock), "last_register": self.clock,
        }  # fmt: skip
        return {"dn": c["dn"], "state": self.datanodes[c["dn"]]["state"]}

    def _op_dn_state(self, c: dict) -> dict:
        self._admin(c["actor"])
        d = self.datanodes.get(c["dn"])
        if not d:
            raise NotFound(f"DataNode {c['dn']}")
        if c["state"] not in ("active", "decommissioning", "removed"):
            raise InvalidArgument("estado inválido")
        d["state"] = c["state"]
        if c["state"] == "removed":
            for b in self.blocks.values():
                if c["dn"] in b["reps"]:
                    b["reps"].remove(c["dn"])
            del self.datanodes[c["dn"]]
        return {"dn": c["dn"], "state": c["state"]}

    # ===================================================================== OPS: locks
    def _op_lock_acquire(self, c: dict) -> dict:
        u = self._user(c["user"])
        node = self._lookup(c["path"], u)
        if node["type"] != "file":
            raise IsADirectory(c["path"])
        mode = c["mode"]
        if mode not in ("shared", "exclusive"):
            raise InvalidArgument("modo de lock inválido")
        self._need(node, u, R if mode == "shared" else W, c["path"])
        lid = c["lock_id"]
        live = self._live_locks(node["id"])
        others = [(i, lk) for i, lk in live if i != lid]
        if mode == "exclusive" and others:
            raise Locked(f"bloqueado por {others[0][1]['user']}")
        if mode == "shared" and any(lk["mode"] == "x" for _i, lk in others):
            raise Locked(f"bloqueado en exclusiva por {others[0][1]['user']}")
        self.fence += 1
        self.locks[lid] = {
            "id": lid, "inode": node["id"], "mode": "x" if mode == "exclusive" else "s",
            "user": u["name"], "expires": self.clock + float(c["ttl"]), "fence": self.fence,
        }  # fmt: skip
        self._lock_idx.setdefault(node["id"], set()).add(lid)
        return {"lock_id": lid, "fence": self.fence, "expires": self.locks[lid]["expires"]}

    def _op_lock_renew(self, c: dict) -> dict:
        lk = self.locks.get(c["lock_id"])
        if not lk or lk["expires"] <= self.clock:
            raise NotFound("lock (inexistente o expirado)")
        if lk["user"] != c["user"]:
            raise PermissionDenied("lock de otro usuario")
        lk["expires"] = self.clock + float(c["ttl"])
        return {"expires": lk["expires"], "fence": lk["fence"]}

    def _op_lock_release(self, c: dict) -> dict:
        lk = self.locks.get(c["lock_id"])
        if lk is None:
            return {"released": False}
        if lk["user"] != c["user"] and not self._user(c["user"])["admin"]:
            raise PermissionDenied("lock de otro usuario")
        self._del_lock(c["lock_id"])
        return {"released": True}

    # ================================================================= OPS: mantenimiento
    def _op_expire(self, c: dict) -> dict:
        """Purga sesiones y locks vencidos (lo propone periódicamente el líder)."""
        grace = float(c.get("grace", 120))
        n_s = n_l = 0
        for s in [s for s in self.sessions.values() if s["expires"] <= self.clock]:
            self._release_session(s, grace)
            n_s += 1
        for lid in [i for i, lk in self.locks.items() if lk["expires"] <= self.clock]:
            self._del_lock(lid)
            n_l += 1
        return {"sessions": n_s, "locks": n_l}

    # ============================================================== CONSULTAS (sin log)
    def q_stat(self, user: str, path: str) -> dict:
        u = self._user(user)
        return self._stat(self._lookup(path, u))

    def q_ls(self, user: str, path: str) -> list[dict]:
        u = self._user(user)
        node = self._lookup(path, u)
        if node["type"] != "dir":
            return [self._stat(node)]
        self._need(node, u, R, path)
        return [self._stat(self.inodes[i]) for _n, i in sorted(node["ch"].items())]

    def q_getacl(self, user: str, path: str) -> dict:
        u = self._user(user)
        return self._stat(self._lookup(path, u))

    def q_file(self, user: str, path: str, want: int = R) -> tuple[dict, list[dict]]:
        """(inodo, bloques) de un archivo, tras comprobar permisos."""
        u = self._user(user)
        node = self._lookup(path, u)
        if node["type"] != "file":
            raise IsADirectory(path)
        self._need(node, u, want, path)
        return node, [self._block_view(self.blocks[b]) for b in node["blocks"]]

    def q_user(self, name: str) -> dict | None:
        return self.users.get(name)

    def q_df(self) -> dict:
        files = [i for i in self.inodes.values() if i["type"] == "file"]
        return {
            "files": len(files), "dirs": len(self.inodes) - len(files),
            "bytes": sum(i["size"] for i in files), "blocks": len(self.blocks),
            "sessions": len(self.sessions), "locks": len(self.locks), "users": len(self.users),
        }  # fmt: skip

    def q_lock(self, lock_id: str) -> dict | None:
        return self.locks.get(lock_id)

    # --- consultas para los monitores del líder
    def replication_report(self, is_alive) -> dict:
        """Bloques sub/sobre-replicados según la vivacidad actual de los DataNodes."""
        under, over, lost = [], [], []
        for b in self.blocks.values():
            if b["state"] != "ok":
                continue
            live = [d for d in b["reps"] if is_alive(d) and self.datanodes.get(d, {}).get("state") == "active"]
            usable = [d for d in b["reps"] if is_alive(d)]  # incluye 'decommissioning'
            if not usable:
                lost.append(b["id"])
            if len(live) < b["repl"]:
                under.append((b["id"], live, usable, b["repl"], b["len"]))
            elif len(live) > b["repl"]:
                over.append((b["id"], live, b["repl"]))
        return {"under": under, "over": over, "lost": lost}

    def expiring(self, now: float) -> bool:
        """¿Hay sesiones/locks vencidos a la hora real ``now``? (lo consulta el líder)"""
        return any(s["expires"] <= now for s in self.sessions.values()) or any(
            lk["expires"] <= now for lk in self.locks.values()
        )
