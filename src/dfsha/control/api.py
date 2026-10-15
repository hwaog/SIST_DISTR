"""API HTTP del ControlNode: pública (usuarios, TLS) e interna (nodos, mTLS)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..common.errors import (
    InvalidArgument,
    NotFound,
    PermissionDenied,
    Unauthenticated,
    Unavailable,
)
from ..common.net import install_error_handlers, peer_of, require_peer
from ..common.util import new_id
from ..security import auth, crypto
from .fsm import R
from .node import ControlNode

API_VERSION = "1"
MAX_JSON_BODY = 32 * 1024 * 1024

# ------------------------------------------------------------------- modelos


class LoginBody(BaseModel):
    username: str
    password: str
    otp: str | None = None


class PathBody(BaseModel):
    path: str


class MkdirBody(PathBody):
    parents: bool = False
    mode: int = 0o755


class RmBody(PathBody):
    recursive: bool = False
    lock_id: str | None = None


class MvBody(BaseModel):
    src: str
    dst: str


class ChmodBody(PathBody):
    mode: int


class ChownBody(PathBody):
    owner: str | None = None
    group: str | None = None


class AclEntry(BaseModel):
    t: str
    n: str
    p: int


class AclBody(PathBody):
    entries: list[AclEntry]


class ReplBody(PathBody):
    repl: int


class BeginBody(PathBody):
    mode: str = "replace"
    overwrite: bool = True
    repl: int | None = None
    block_size: int | None = None
    lock_id: str | None = None
    mode_bits: int = 0o644


class AllocBody(BaseModel):
    count: int = Field(ge=1, le=512)
    zone: str | None = None
    exclude: list[str] = []


class ReallocBody(BaseModel):
    block_id: str
    zone: str | None = None
    exclude: list[str] = []


class CommitBody(BaseModel):
    layout: list[dict[str, Any]]
    size: int
    if_version: int | None = None
    lock_id: str | None = None


class LockBody(PathBody):
    mode: str = "exclusive"
    ttl: float = 30.0
    lock_id: str | None = None


class LockRef(BaseModel):
    lock_id: str
    ttl: float = 30.0


class NewUser(BaseModel):
    name: str
    password: str = Field(min_length=8)
    groups: list[str] = []
    admin: bool = False


class UserPatch(BaseModel):
    password: str | None = Field(default=None, min_length=8)
    groups: list[str] | None = None
    enabled: bool | None = None


class PasswordBody(BaseModel):
    old: str
    new: str = Field(min_length=8)


class PasswordConfirm(BaseModel):
    password: str
    otp: str | None = None


class ApiKeyBody(BaseModel):
    label: str = ""


class DnState(BaseModel):
    state: str


# ----------------------------------------------------------- API pública (usuarios)


def build_public_app(node: ControlNode) -> FastAPI:
    app = FastAPI(
        title="DFSha — API del ControlNode",
        version=API_VERSION,
        description="Metadatos, autenticación y coordinación del sistema de archivos distribuido DFSha.",
    )
    install_error_handlers(app)

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        # Protección básica contra cuerpos gigantes en la API de metadatos (los bloques van a los DataNodes).
        cl = request.headers.get("content-length")
        if cl is not None and cl.isdigit() and int(cl) > MAX_JSON_BODY:
            from fastapi.responses import JSONResponse

            return JSONResponse({"error": {"code": "EINVAL", "message": "cuerpo demasiado grande"}}, status_code=413)
        return await call_next(request)

    async def me(request: Request) -> dict:
        node.metrics["requests"] += 1
        if request.query_params.get("stale") != "1" and not node.is_leader():
            # Un seguidor puede ir atrasado unos ms: autenticar con su estado daría falsos
            # rechazos (p. ej. una API key recién creada).  Se redirige al líder primero.
            raise node.nl()
        return node.authenticate(request.headers)

    async def admin(request: Request) -> dict:
        u = await me(request)
        if not u["admin"]:
            raise PermissionDenied("se requiere rol administrador")
        return u

    def rid(request: Request) -> str | None:
        return request.headers.get("x-request-id")

    def stale(request: Request) -> bool:
        return request.query_params.get("stale") == "1"

    async def mutate(request: Request, user: dict, op: str, **fields):
        r = await node.propose(op, req_id=rid(request), user=user["name"], **fields)
        node.metrics["mutations"] += 1
        node.audit(op, user=user["name"], ip=request.client.host if request.client else "?", **{k: v for k, v in fields.items() if k in ("path", "src", "dst", "recursive")})
        return r

    # ----- sin autenticación
    @app.get("/health", tags=["sistema"])
    async def health():
        i = node.node_info()
        return {"ok": True, "id": i["id"], "role": i["role"], "term": i["term"], "leader": i["leader_url"]}

    @app.get("/v1/node", tags=["sistema"])
    async def node_info():
        return node.node_info()

    # ----- autenticación
    @app.post("/v1/auth/login", tags=["auth"])
    async def login(body: LoginBody, request: Request):
        return await node.login(body.username, body.password, body.otp, request.client.host if request.client else "?")

    @app.post("/v1/auth/refresh", tags=["auth"])
    async def refresh(u: dict = Depends(me)):
        tok, exp = auth.issue_jwt(node.keys.jwt, user=u["name"], ver=u["ver"], ttl=node.cfg.token_ttl)
        return {"token": tok, "expires_at": exp, "user": u["name"], "groups": u["groups"], "admin": u["admin"]}

    @app.get("/v1/whoami", tags=["auth"])
    async def whoami(u: dict = Depends(me)):
        return node.user_public(u)

    @app.post("/v1/auth/password", tags=["auth"])
    async def change_password(body: PasswordBody, request: Request, u: dict = Depends(me)):
        node.ensure_leader()
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, auth.verify_password, body.old, u["pw"]):
            raise Unauthenticated("clave actual incorrecta")
        pw = await loop.run_in_executor(None, auth.hash_password, body.new)
        await node.propose("user_set", req_id=rid(request), actor=u["name"], name=u["name"], pw=pw)
        node.audit("password_change", user=u["name"])
        return {"ok": True, "message": "clave cambiada; las sesiones previas quedaron invalidadas"}

    @app.post("/v1/auth/totp/enable", tags=["auth"])
    async def totp_enable(body: PasswordConfirm, u: dict = Depends(me)):
        node.ensure_leader()
        if not await asyncio.get_running_loop().run_in_executor(None, auth.verify_password, body.password, u["pw"]):
            raise Unauthenticated("clave incorrecta")
        secret = auth.new_totp_secret()
        wrapped = crypto.wrap(node.keys.secrets, secret.encode(), f"totp|{u['name']}".encode())
        await node.propose("totp_set", actor=u["name"], name=u["name"], secret=wrapped)
        node.audit("totp_enable", user=u["name"])
        return {"secret": secret, "uri": auth.totp_uri(secret, u["name"]), "message": "2FA activado; vuelva a iniciar sesión"}

    @app.post("/v1/auth/totp/disable", tags=["auth"])
    async def totp_disable(body: PasswordConfirm, u: dict = Depends(me)):
        node.ensure_leader()
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, auth.verify_password, body.password, u["pw"]):
            raise Unauthenticated("clave incorrecta")
        if u["totp"]:
            secret = crypto.unwrap(node.keys.secrets, u["totp"], f"totp|{u['name']}".encode()).decode()
            if auth.totp_verify(secret, body.otp or "") is None:
                raise Unauthenticated("código 2FA inválido")
        await node.propose("totp_set", actor=u["name"], name=u["name"], secret=None)
        node.audit("totp_disable", user=u["name"])
        return {"ok": True}

    @app.post("/v1/auth/apikeys", tags=["auth"])
    async def apikey_new(body: ApiKeyBody, u: dict = Depends(me)):
        node.ensure_leader()
        kid, full, h = auth.new_api_key()
        await node.propose("apikey_add", actor=u["name"], id=kid, hash=h, label=body.label)
        node.audit("apikey_create", user=u["name"], id=kid)
        return {"id": kid, "key": full, "message": "guárdela ahora: no se vuelve a mostrar"}

    @app.get("/v1/auth/apikeys", tags=["auth"])
    async def apikey_list(u: dict = Depends(me)):
        return [
            {"id": k["id"], "label": k["label"], "created": k["created"]}
            for k in node.fsm.apikeys.values()
            if k["user"] == u["name"]
        ]

    @app.delete("/v1/auth/apikeys/{key_id}", tags=["auth"])
    async def apikey_revoke(key_id: str, u: dict = Depends(me)):
        node.ensure_leader()
        r = await node.propose("apikey_revoke", actor=u["name"], id=key_id)
        node.audit("apikey_revoke", user=u["name"], id=key_id)
        return r

    # ----- sistema de archivos (RF1)
    @app.get("/v1/fs/stat", tags=["fs"])
    async def stat(path: str, request: Request, u: dict = Depends(me)):
        return await node.read(lambda: node.fsm.q_stat(u["name"], path), stale=stale(request))

    @app.get("/v1/fs/ls", tags=["fs"])
    async def ls(path: str, request: Request, u: dict = Depends(me)):
        return await node.read(lambda: node.fsm.q_ls(u["name"], path), stale=stale(request))

    @app.get("/v1/fs/df", tags=["fs"])
    async def df(request: Request, u: dict = Depends(me)):
        return await node.read(node.fsm.q_df, stale=stale(request))

    @app.post("/v1/fs/mkdir", tags=["fs"])
    async def mkdir(b: MkdirBody, request: Request, u: dict = Depends(me)):
        return await mutate(request, u, "mkdir", path=b.path, parents=b.parents, mode=b.mode)

    @app.post("/v1/fs/rmdir", tags=["fs"])
    async def rmdir(b: PathBody, request: Request, u: dict = Depends(me)):
        return await mutate(request, u, "rmdir", path=b.path)

    @app.post("/v1/fs/rm", tags=["fs"])
    async def rm(b: RmBody, request: Request, u: dict = Depends(me)):
        return await mutate(request, u, "rm", path=b.path, recursive=b.recursive, lock_id=b.lock_id, grace=node.cfg.gc_grace)

    @app.post("/v1/fs/mv", tags=["fs"])
    async def mv(b: MvBody, request: Request, u: dict = Depends(me)):
        return await mutate(request, u, "mv", src=b.src, dst=b.dst, grace=node.cfg.gc_grace)

    @app.post("/v1/fs/chmod", tags=["fs"])
    async def chmod(b: ChmodBody, request: Request, u: dict = Depends(me)):
        return await mutate(request, u, "chmod", path=b.path, mode=b.mode)

    @app.post("/v1/fs/chown", tags=["fs"])
    async def chown(b: ChownBody, request: Request, u: dict = Depends(me)):
        return await mutate(request, u, "chown", path=b.path, owner=b.owner, group=b.group)

    @app.post("/v1/fs/setacl", tags=["fs"])
    async def setacl(b: AclBody, request: Request, u: dict = Depends(me)):
        return await mutate(request, u, "setacl", path=b.path, entries=[e.model_dump() for e in b.entries])

    @app.post("/v1/fs/setrepl", tags=["fs"])
    async def setrepl(b: ReplBody, request: Request, u: dict = Depends(me)):
        return await mutate(request, u, "setrepl", path=b.path, repl=b.repl)

    # ----- transferencia y acceso a archivos (RF2, RF3)
    @app.post("/v1/files/begin", tags=["files"])
    async def begin(b: BeginBody, request: Request, u: dict = Depends(me)):
        return await node.begin_write(u["name"], b.model_dump(), rid(request))

    @app.post("/v1/files/{sid}/blocks", tags=["files"])
    async def alloc(sid: str, b: AllocBody, u: dict = Depends(me)):
        return {"blocks": await node.alloc_blocks(u["name"], sid, b.count, b.zone, b.exclude)}

    @app.post("/v1/files/{sid}/realloc", tags=["files"])
    async def realloc(sid: str, b: ReallocBody, u: dict = Depends(me)):
        return await node.realloc_block(u["name"], sid, b.block_id, b.exclude, b.zone)

    @app.post("/v1/files/{sid}/commit", tags=["files"])
    async def commit(sid: str, b: CommitBody, request: Request, u: dict = Depends(me)):
        node.ensure_leader()
        res = await node.propose(
            "commit_write", req_id=rid(request), user=u["name"], sid=sid, layout=b.layout, size=b.size,
            if_version=b.if_version, lock_id=b.lock_id, grace=node.cfg.gc_grace,
        )  # fmt: skip
        node.metrics["commits"] += 1
        node.audit("commit", user=u["name"], path=res["path"], size=res["size"], version=res["version"])
        return res

    @app.post("/v1/files/{sid}/abort", tags=["files"])
    async def abort(sid: str, u: dict = Depends(me)):
        return await node.propose("abort_write", user=u["name"], sid=sid)

    @app.post("/v1/files/{sid}/renew", tags=["files"])
    async def renew(sid: str, u: dict = Depends(me)):
        return await node.propose("renew_session", user=u["name"], sid=sid, ttl=node.cfg.session_ttl)

    @app.get("/v1/files/locations", tags=["files"])
    async def locations(path: str, request: Request, start: int = 0, limit: int = 256, zone: str | None = None, u: dict = Depends(me)):
        node.metrics["locations"] += 1
        # Las ubicaciones y los tokens de bloque NUNCA se sirven desde estado atrasado (respetan revocaciones).
        return await node.locations(u["name"], path, start, min(limit, 1024), zone, False)

    # ----- locks
    @app.post("/v1/locks/acquire", tags=["locks"])
    async def lock_acquire(b: LockBody, u: dict = Depends(me)):
        return await node.propose("lock_acquire", user=u["name"], path=b.path, mode=b.mode, ttl=b.ttl, lock_id=b.lock_id or new_id())

    @app.post("/v1/locks/renew", tags=["locks"])
    async def lock_renew(b: LockRef, u: dict = Depends(me)):
        return await node.propose("lock_renew", user=u["name"], lock_id=b.lock_id, ttl=b.ttl)

    @app.post("/v1/locks/release", tags=["locks"])
    async def lock_release(b: LockRef, u: dict = Depends(me)):
        return await node.propose("lock_release", user=u["name"], lock_id=b.lock_id)

    # ----- administración
    @app.get("/v1/admin/users", tags=["admin"])
    async def users(u: dict = Depends(admin)):
        await node.read(lambda: None)
        return [node.user_public(x) for x in node.fsm.users.values()]

    @app.post("/v1/admin/users", tags=["admin"])
    async def user_add(b: NewUser, request: Request, u: dict = Depends(admin)):
        node.ensure_leader()
        pw = await asyncio.get_running_loop().run_in_executor(None, auth.hash_password, b.password)
        r = await node.propose("user_add", req_id=rid(request), actor=u["name"], name=b.name, pw=pw, groups=b.groups, admin=b.admin)
        node.audit("user_add", actor=u["name"], user=b.name, admin=b.admin)
        return r

    @app.patch("/v1/admin/users/{name}", tags=["admin"])
    async def user_patch(name: str, b: UserPatch, u: dict = Depends(admin)):
        node.ensure_leader()
        fields: dict[str, Any] = {}
        if b.password is not None:
            fields["pw"] = await asyncio.get_running_loop().run_in_executor(None, auth.hash_password, b.password)
        if b.groups is not None:
            fields["groups"] = b.groups
        if b.enabled is not None:
            fields["enabled"] = b.enabled
        if not fields:
            raise InvalidArgument("nada que cambiar")
        r = await node.propose("user_set", actor=u["name"], name=name, **fields)
        node.audit("user_set", actor=u["name"], user=name, fields=sorted(fields))
        return r

    @app.delete("/v1/admin/users/{name}", tags=["admin"])
    async def user_del(name: str, u: dict = Depends(admin)):
        r = await node.propose("user_del", actor=u["name"], name=name)
        node.audit("user_del", actor=u["name"], user=name)
        return r

    @app.get("/v1/admin/datanodes", tags=["admin"])
    async def datanodes(u: dict = Depends(me)):
        return node.datanodes_view()

    @app.post("/v1/admin/datanodes/{dn_id}/state", tags=["admin"])
    async def dn_state(dn_id: str, b: DnState, u: dict = Depends(admin)):
        r = await node.propose("dn_state", actor=u["name"], dn=dn_id, state=b.state)
        node.audit("dn_state", actor=u["name"], dn=dn_id, state=b.state)
        return r

    @app.post("/v1/admin/datanodes/{dn_id}/scrub", tags=["admin"])
    async def dn_scrub(dn_id: str, u: dict = Depends(admin)):
        """Pide al DataNode una verificación inmediata de integridad de todos sus bloques."""
        rec = node.fsm.datanodes.get(dn_id)
        if rec is None:
            raise NotFound(f"DataNode {dn_id}")
        r = await node.http.post(f"{rec['internal_url']}/internal/scrub", timeout=300)
        if r.status_code != 200:
            raise Unavailable(f"el DataNode {dn_id} respondió HTTP {r.status_code}")
        node.audit("scrub", actor=u["name"], dn=dn_id, corrupt=len(r.json()["corrupt"]))
        return r.json()

    @app.get("/v1/admin/fsck", tags=["admin"])
    async def fsck(u: dict = Depends(me)):
        return await node.read(node.fsck)

    @app.get("/v1/cluster", tags=["sistema"])
    async def cluster(u: dict = Depends(me)):
        st = node.cluster_status()
        st["maintenance"] = node.monitors.stats if node.monitors else {}
        st["time"] = time.time()
        return st

    return app


# ------------------------------------------------------- API interna (nodos, mTLS)


class DnRegister(BaseModel):
    dn: str
    zone: str = "default"
    public_url: str
    internal_url: str


class DnHeartbeat(BaseModel):
    dn: str
    used: int = 0
    capacity: int = 0
    nblocks: int = 0
    load: float = 0.0


class DnBlockReport(BaseModel):
    dn: str
    blocks: list[dict[str, Any]]


class DnCorrupt(BaseModel):
    dn: str
    blocks: list[str]


_RAFT_RPCS = {
    "pre_vote": "handle_pre_vote",
    "request_vote": "handle_request_vote",
    "append_entries": "handle_append_entries",
    "install_snapshot": "handle_install_snapshot",
}


def build_internal_app(node: ControlNode) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_error_handlers(app)

    def dn_identity(request: Request, claimed: str) -> None:
        cn = require_peer(request, "datanode")
        if cn != claimed:
            raise PermissionDenied(f"el certificado ({cn}) no corresponde al DataNode declarado ({claimed})")

    @app.post("/raft/{rpc}")
    async def raft_rpc(rpc: str, request: Request):
        require_peer(request, "controlnode")
        if rpc not in _RAFT_RPCS:
            raise NotFound(rpc)
        return await getattr(node.raft, _RAFT_RPCS[rpc])(await request.json())

    @app.post("/internal/dn/register")
    async def dn_register(b: DnRegister, request: Request):
        dn_identity(request, b.dn)
        return await node.register_dn(b.dn, b.zone, b.public_url, b.internal_url)

    @app.post("/internal/dn/heartbeat")
    async def dn_heartbeat(b: DnHeartbeat, request: Request):
        dn_identity(request, b.dn)
        r = node.heartbeat(b.dn, b.model_dump())
        r["leader"] = node.internal.get(node.raft.leader_id) if node.raft.leader_id else None
        return r

    @app.post("/internal/dn/blockreport")
    async def dn_blockreport(b: DnBlockReport, request: Request):
        dn_identity(request, b.dn)
        return await node.block_report(b.dn, b.blocks)

    @app.post("/internal/dn/corrupt")
    async def dn_corrupt(b: DnCorrupt, request: Request):
        dn_identity(request, b.dn)
        return await node.corrupt_report(b.dn, b.blocks)

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(request: Request):
        if peer_of(request) is None:
            raise PermissionDenied("mTLS requerido")
        return node.metrics_text()

    return app


__all__ = ["build_public_app", "build_internal_app", "R"]
