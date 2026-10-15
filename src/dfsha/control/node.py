"""Lógica del ControlNode: consenso + metadatos + autenticación + coordinación de DataNodes."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import Counter
from dataclasses import dataclass

import httpx

from ..common.errors import (
    DfshaError,
    InvalidArgument,
    NoSpace,
    NotLeader,
    PermissionDenied,
    StaleHandle,
    TooManyAttempts,
    Unauthenticated,
)
from ..common.net import internal_client
from ..common.util import b64e, new_id
from ..raft.core import RaftNode, Role
from ..raft.storage import RaftStorage
from ..security import auth, crypto
from . import placement
from .config import ControlConfig
from .fsm import MetadataStore, R

log = logging.getLogger("dfsha.control")
audit_log = logging.getLogger("dfsha.audit")

DEK_AAD = b"dfsha-dek"
_DUMMY_HASH = auth.hash_password("dfsha-dummy-password-for-timing")  # igualar tiempos de login


@dataclass
class DnLive:
    """Estado volátil de un DataNode (vivacidad y carga), por heartbeat."""

    id: str
    last_seen: float = 0.0
    used: int = 0
    capacity: int = 0
    nblocks: int = 0
    load: float = 0.0
    dead_since: float | None = None
    announced_dead: bool = False


class HttpTransport:
    """Transporte Raft: JSON sobre HTTPS con mTLS."""

    def __init__(self, urls: dict[str, str], client: httpx.AsyncClient) -> None:
        self.urls, self.client = urls, client

    async def send(self, peer: str, rpc: str, payload: dict, timeout: float) -> dict:
        r = await self.client.post(f"{self.urls[peer]}/raft/{rpc}", json=payload, timeout=timeout)
        if r.status_code != 200:
            raise ConnectionError(f"{peer}: HTTP {r.status_code}")
        return r.json()


class ControlNode:
    def __init__(self, cfg: ControlConfig) -> None:
        self.cfg = cfg
        self.keys = crypto.ClusterKeys.from_file(cfg.cluster_secret)
        self.fsm = MetadataStore()
        self.http = internal_client(cfg.ca, cfg.cert, cfg.key, timeout=cfg.propose_timeout)
        self.pub = {p.id: p.public_url for p in cfg.peers}
        self.internal = {p.id: p.internal_url for p in cfg.peers}
        self.raft = RaftNode(
            cfg.node_id,
            [p.id for p in cfg.peers],
            RaftStorage(cfg.data_dir / "raft", fsync=cfg.fsync),
            self.fsm,
            HttpTransport(self.internal, self.http),
            cfg.raft,
            on_role_change=self._on_role,
        )
        self.dn: dict[str, DnLive] = {}
        self.boot = time.monotonic()
        self.metrics: Counter = Counter()
        self._throttle: dict[str, tuple[int, float]] = {}
        self._missing: dict[tuple[str, str], int] = {}
        self.monitors = None  # type: ignore[assignment]  # se crea en start()
        self._setup_audit()

    # ----------------------------------------------------------- ciclo de vida
    def _setup_audit(self) -> None:
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        h = logging.FileHandler(self.cfg.data_dir / "audit.log")
        h.setFormatter(logging.Formatter("%(message)s"))
        audit_log.addHandler(h)
        audit_log.setLevel(logging.INFO)
        audit_log.propagate = False

    def audit(self, event: str, **kv) -> None:
        audit_log.info(json.dumps({"t": round(time.time(), 3), "node": self.cfg.node_id, "event": event, **kv}))

    async def start(self) -> None:
        from .monitor import Monitors

        await self.raft.start()
        self.monitors = Monitors(self)
        self.monitors.start()

    async def stop(self) -> None:
        if self.monitors:
            await self.monitors.stop()
        await self.raft.stop()
        await self.http.aclose()

    def _on_role(self, role: Role, term: int, leader: str | None) -> None:
        log.info("%s: rol=%s término=%d líder=%s", self.cfg.node_id, role.value, term, leader)
        self.metrics[f"role_changes_{role.value}"] += 1

    # --------------------------------------------------------------- consenso
    def is_leader(self) -> bool:
        return self.raft.role == Role.LEADER

    def nl(self) -> NotLeader:
        lid = self.raft.leader_id
        return NotLeader(self.pub.get(lid) if lid else None, leader_internal=self.internal.get(lid) if lid else None)

    def ensure_leader(self) -> None:
        if not self.is_leader():
            raise self.nl()

    async def propose(self, op: str, *, req_id: str | None = None, **fields):
        cmd = {"op": op, "ts": time.time(), "req_id": req_id or new_id(), **fields}
        try:
            return await self.raft.propose(cmd, timeout=self.cfg.propose_timeout)
        except NotLeader:
            raise self.nl() from None

    async def read(self, fn, *, stale: bool = False):
        """Lectura de metadatos.  Por defecto linealizable (sólo el líder, con lease)."""
        if not stale:
            try:
                await self.raft.read_barrier()
            except NotLeader:
                raise self.nl() from None
        return fn()

    # ------------------------------------------------------------ DataNodes (vivacidad)
    def heartbeat(self, dn_id: str, p: dict) -> dict:
        live = self.dn.setdefault(dn_id, DnLive(dn_id))
        was_dead = live.last_seen == 0.0 or not self.is_alive(dn_id)
        live.last_seen = time.monotonic()
        live.used, live.capacity = int(p.get("used", 0)), int(p.get("capacity", 0))
        live.nblocks, live.load = int(p.get("nblocks", 0)), float(p.get("load", 0.0))
        live.dead_since, live.announced_dead = None, False
        if was_dead:
            log.info("DataNode %s vivo (usado=%d cap=%d)", dn_id, live.used, live.capacity)
        return {"registered": dn_id in self.fsm.datanodes}

    def is_alive(self, dn_id: str) -> bool:
        live = self.dn.get(dn_id)
        return live is not None and live.last_seen > 0 and time.monotonic() - live.last_seen < self.cfg.dn_dead_after

    def dead_since(self, dn_id: str) -> float:
        """Instante (monotónico) desde el que se considera muerto el DataNode.

        Se DERIVA del último heartbeat (``last_seen + dn_dead_after``), no de la anotación del bucle de vivacidad
        (que corre cada 1 s): si se usara ésta, durante hasta 1 s tras morir el nodo la gracia ``repl_grace`` se
        saltaría y se movería data por un fallo transitorio.  Un nodo jamás visto desde el arranque de este
        ControlNode se asume muerto desde ese arranque.
        """
        live = self.dn.get(dn_id)
        if live is not None and live.last_seen:
            return live.last_seen + self.cfg.dn_dead_after
        return self.boot

    def candidates(self) -> list[placement.Candidate]:
        out = []
        for dn_id, rec in self.fsm.datanodes.items():
            if rec["state"] != "active" or not self.is_alive(dn_id):
                continue
            live = self.dn[dn_id]
            out.append(placement.Candidate(dn_id, rec["zone"], live.used, live.capacity, live.load))
        return out

    # ------------------------------------------------------------ autenticación
    def authenticate(self, headers) -> dict:
        authz = headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            claims = auth.decode_jwt(self.keys.jwt, authz[7:].strip())
            u = self.fsm.users.get(claims["sub"])
            if not u or not u.get("enabled", True) or u["ver"] != claims.get("ver"):
                raise Unauthenticated("sesión inválida o revocada")
            return u
        key = headers.get("x-api-key")
        if key:
            kid, secret = auth.parse_api_key(key)
            rec = self.fsm.apikeys.get(kid)
            if not rec or not auth.api_key_matches(secret, rec["hash"]):
                raise Unauthenticated("API key inválida")
            u = self.fsm.users.get(rec["user"])
            if not u or not u.get("enabled", True):
                raise Unauthenticated("usuario deshabilitado")
            return u
        raise Unauthenticated("se requiere autenticación")

    def _throttle_limit(self, key: str) -> int:
        # Por usuario: umbral estricto.  Por IP: 10x (varias personas pueden compartir NAT).
        return self.cfg.login_max_failures * (10 if key.startswith("ip:") else 1)

    def _throttle_check(self, key: str) -> None:
        _fails, until = self._throttle.get(key, (0, 0.0))
        if until > time.time():
            raise TooManyAttempts(f"demasiados intentos fallidos; reintente en {until - time.time():.0f}s")

    def _throttle_fail(self, key: str) -> None:
        fails, _ = self._throttle.get(key, (0, 0.0))
        fails += 1
        until = 0.0
        limit = self._throttle_limit(key)
        if fails >= limit:
            until = time.time() + min(900.0, self.cfg.login_lock_base * 2 ** (fails - limit))
        self._throttle[key] = (fails, until)

    async def login(self, username: str, password: str, otp: str | None, ip: str) -> dict:
        self.ensure_leader()
        for key in (f"u:{username}", f"ip:{ip}"):
            self._throttle_check(key)
        u = self.fsm.users.get(username)
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, auth.verify_password, password, u["pw"] if u else _DUMMY_HASH)
        if not (ok and u and u.get("enabled", True)):
            for key in (f"u:{username}", f"ip:{ip}"):
                self._throttle_fail(key)
            self.audit("login_failed", user=username, ip=ip)
            raise Unauthenticated("usuario o clave inválidos")
        method = "password"
        if u["totp"]:
            if not otp:
                raise Unauthenticated("se requiere el código 2FA", otp_required=True)
            secret = crypto.unwrap(self.keys.secrets, u["totp"], f"totp|{username}".encode()).decode()
            step = auth.totp_verify(secret, otp)
            if step is None:
                for key in (f"u:{username}", f"ip:{ip}"):
                    self._throttle_fail(key)
                self.audit("login_failed_otp", user=username, ip=ip)
                raise Unauthenticated("código 2FA inválido", otp_required=True)
            await self.propose("totp_use", name=username, step=step)
            method = "password+totp"
        for key in (f"u:{username}", f"ip:{ip}"):
            self._throttle.pop(key, None)
        tok, exp = auth.issue_jwt(self.keys.jwt, user=username, ver=u["ver"], ttl=self.cfg.token_ttl, method=method)
        self.audit("login", user=username, ip=ip, method=method)
        return {"token": tok, "expires_at": exp, "user": username, "groups": u["groups"], "admin": u["admin"]}

    def user_public(self, u: dict) -> dict:
        return {"name": u["name"], "groups": u["groups"], "admin": u["admin"], "enabled": u.get("enabled", True), "totp": bool(u["totp"])}

    # ------------------------------------------------------------------- tokens
    def _dek_b64(self, wrapped: str) -> str:
        return b64e(crypto.unwrap(self.keys.kek, wrapped, DEK_AAD))

    def read_token(self, dek_b64: str, block_id: str, user: str) -> str:
        return crypto.seal(
            self.keys.block_token, {"op": "r", "bid": block_id, "dek": dek_b64, "uid": user}, ttl=self.cfg.block_token_ttl
        )

    def write_token(self, dek_b64: str, block_id: str, pipeline: list[str], user: str, max_len: int) -> str:
        pipe = [{"id": d, "iu": self.fsm.datanodes[d]["internal_url"]} for d in pipeline]
        return crypto.seal(
            self.keys.block_token,
            {"op": "w", "bid": block_id, "dek": dek_b64, "pipe": pipe, "uid": user, "max": max_len, "gen": 1},
            ttl=self.cfg.block_token_ttl,
        )

    def _pub_loc(self, dn_id: str) -> dict:
        return {"id": dn_id, "url": self.fsm.datanodes[dn_id]["public_url"]}

    # ---------------------------------------------------------- operaciones de archivo
    async def begin_write(self, user: str, body: dict, req_id: str | None) -> dict:
        self.ensure_leader()
        repl = int(body.get("repl") or self.cfg.default_repl)
        bs = int(body.get("block_size") or self.cfg.block_size)
        if not 64 * 1024 <= bs <= 256 * 1024 * 1024:
            raise InvalidArgument("block_size debe estar entre 64 KiB y 256 MiB")
        dek = crypto.wrap(self.keys.kek, crypto.new_dek(), DEK_AAD)
        res = await self.propose(
            "begin_write", req_id=req_id, user=user, path=body["path"], mode=body.get("mode", "replace"),
            sid=new_id(), repl=repl, min_repl=self.cfg.min_replicas(repl), bs=bs, ttl=self.cfg.session_ttl,
            dek=dek, overwrite=body.get("overwrite", True), lock_id=body.get("lock_id"),
            file_mode=body.get("mode_bits", 0o644),
        )  # fmt: skip
        res["session_ttl"] = self.cfg.session_ttl
        return res

    async def alloc_blocks(self, user: str, sid: str, count: int, zone: str | None, exclude: list[str]) -> list[dict]:
        self.ensure_leader()
        s = self.fsm.sessions.get(sid)
        if s is None or s["user"] != user:
            raise StaleHandle("sesión de escritura inexistente")
        if not 1 <= count <= 512:
            raise InvalidArgument("count debe estar en [1,512]")
        cands = self.candidates()
        picks_per_block: list[list[str]] = []
        for _ in range(count):
            picks = placement.choose(cands, s["repl"], exclude=set(exclude), zone_hint=zone, size=s["bs"])
            if len(picks) < s["min_repl"]:
                raise NoSpace(f"DataNodes vivos insuficientes: {len(picks)} < mínimo {s['min_repl']}")
            for c in picks:  # el contador de carga local evita apilar toda la ráfaga en un nodo
                c.used += s["bs"]
            picks_per_block.append([c.id for c in picks])
        ids = [new_id() for _ in range(count)]
        await self.propose(
            "alloc_blocks", user=user, sid=sid, ttl=self.cfg.session_ttl,
            blocks=[{"id": i, "dns": dns} for i, dns in zip(ids, picks_per_block, strict=True)],
        )  # fmt: skip
        dek = self._dek_b64(s["dek"])
        return [
            {
                "block_id": i,
                "pipeline": [self._pub_loc(d) for d in dns],
                "token": self.write_token(dek, i, dns, user, s["bs"]),
            }
            for i, dns in zip(ids, picks_per_block, strict=True)
        ]

    async def realloc_block(self, user: str, sid: str, block_id: str, exclude: list[str], zone: str | None) -> dict:
        self.ensure_leader()
        s = self.fsm.sessions.get(sid)
        if s is None or s["user"] != user:
            raise StaleHandle("sesión de escritura inexistente")
        picks = placement.choose(self.candidates(), s["repl"], exclude=set(exclude), zone_hint=zone, size=s["bs"])
        if len(picks) < s["min_repl"]:
            raise NoSpace("DataNodes vivos insuficientes para reubicar el bloque")
        dns = [c.id for c in picks]
        await self.propose("realloc_block", user=user, sid=sid, block_id=block_id, dns=dns)
        return {
            "block_id": block_id,
            "pipeline": [self._pub_loc(d) for d in dns],
            "token": self.write_token(self._dek_b64(s["dek"]), block_id, dns, user, s["bs"]),
        }

    async def locations(self, user: str, path: str, start: int, limit: int, zone: str | None, stale: bool) -> dict:
        def build():
            node, blocks = self.fsm.q_file(user, path, R)
            dek = self._dek_b64(node["dek"])
            out = []
            for b in blocks[start : start + limit]:
                reps = [d for d in b["reps"] if d in self.fsm.datanodes]
                random.shuffle(reps)
                reps.sort(key=lambda d: (not self.is_alive(d), self.fsm.datanodes[d]["zone"] != zone))
                out.append(
                    {
                        "block_id": b["id"], "len": b["len"], "sha": b["sha"],
                        "locs": [self._pub_loc(d) for d in reps],
                        "token": self.read_token(dek, b["id"], user),
                    }
                )  # fmt: skip
            return {
                "inode": node["id"], "size": node["size"], "version": node["ver"], "block_size": node["bs"],
                "repl": node["repl"], "nblocks": len(blocks), "start": start, "blocks": out,
            }  # fmt: skip

        return await self.read(build, stale=stale)

    # ------------------------------------------------------------ reportes de DataNodes
    async def register_dn(self, dn_id: str, zone: str, public_url: str, internal_url: str) -> dict:
        self.ensure_leader()
        await self.propose("dn_register", dn=dn_id, zone=zone, public_url=public_url.rstrip("/"), internal_url=internal_url.rstrip("/"))
        self.audit("dn_register", dn=dn_id, zone=zone)
        return {"block_key": b64e(self.keys.block_token), "heartbeat_interval": 2.0}

    async def block_report(self, dn_id: str, items: list[dict]) -> dict:
        """Reconcilia lo que el DataNode dice tener con lo que el metadato espera."""
        self.ensure_leader()
        ids = {i["id"] for i in items}
        adds, removes = [], []
        for bid in ids:
            b = self.fsm.blocks.get(bid)
            if b is None:
                removes.append((bid, dn_id, True))  # huérfano: se borra
            elif b["state"] == "ok" and dn_id not in b["reps"]:
                adds.append((bid, dn_id))
            self._missing.pop((bid, dn_id), None)
        pending = False
        for b in self.fsm.blocks.values():
            if b["state"] == "ok" and dn_id in b["reps"] and b["id"] not in ids:
                key = (b["id"], dn_id)
                n = self._missing.get(key, 0) + 1
                if n >= self.cfg.report_missing_confirmations:
                    removes.append((b["id"], dn_id, False))
                    self._missing.pop(key, None)
                else:
                    self._missing[key] = n
                    pending = True
        if adds or removes:
            await self.propose("replica_update", adds=adds, removes=removes, force=True)
        return {"adds": len(adds), "removes": len(removes), "resend": pending}

    async def corrupt_report(self, dn_id: str, block_ids: list[str]) -> dict:
        self.ensure_leader()
        removes = [(b, dn_id, True) for b in block_ids]
        await self.propose("replica_update", removes=removes)
        self.audit("corrupt_replica", dn=dn_id, blocks=len(block_ids))
        return {"removed": len(block_ids)}

    # ------------------------------------------------------------------ reportes
    def fsck(self, sample: int = 20) -> dict:
        rep = self.fsm.replication_report(self.is_alive)
        total = len(self.fsm.blocks)
        return {
            "blocks": total,
            "under_replicated": len(rep["under"]),
            "over_replicated": len(rep["over"]),
            "lost": len(rep["lost"]),
            "healthy": total - len(rep["under"]) - len(rep["over"]) - len(rep["lost"]),
            "under_sample": [{"block": u[0], "live": u[1], "target": u[3]} for u in rep["under"][:sample]],
            "lost_sample": rep["lost"][:sample],
        }

    def datanodes_view(self) -> list[dict]:
        out = []
        now = time.monotonic()
        for dn_id, rec in sorted(self.fsm.datanodes.items()):
            live = self.dn.get(dn_id)
            out.append(
                {
                    "id": dn_id, "zone": rec["zone"], "state": rec["state"], "alive": self.is_alive(dn_id),
                    "url": rec["public_url"], "used": live.used if live else 0,
                    "capacity": live.capacity if live else 0, "blocks": live.nblocks if live else 0,
                    "load": live.load if live else 0.0,
                    "last_seen_s": round(now - live.last_seen, 1) if live and live.last_seen else None,
                }
            )  # fmt: skip
        return out

    def node_info(self) -> dict:
        st = self.raft.status()
        lid = st["leader"]
        return {
            "id": self.cfg.node_id, "role": st["role"], "term": st["term"], "leader": lid,
            "leader_url": self.pub.get(lid) if lid else None, "applied": st["last_applied"],
            "commit": st["commit_index"], "log_len": st["last_index"], "snapshot_index": st["snapshot_index"],
        }  # fmt: skip

    def cluster_status(self) -> dict:
        return {
            "node": self.node_info(),
            "controlnodes": [{"id": p.id, "url": p.public_url} for p in self.cfg.peers],
            "datanodes": self.datanodes_view(),
            "df": self.fsm.q_df(),
            "fsck": self.fsck(),
            "raft": self.raft.status(),
        }

    def metrics_text(self) -> str:
        lines = []
        for k, v in sorted(self.metrics.items()):
            lines.append(f"dfsha_control_{k} {v}")
        st = self.raft.status()
        lines += [
            f"dfsha_raft_term {st['term']}",
            f"dfsha_raft_is_leader {1 if st['role'] == 'leader' else 0}",
            f"dfsha_raft_commit_index {st['commit_index']}",
            f"dfsha_raft_log_entries {len(self.raft.log)}",
            f"dfsha_blocks_total {len(self.fsm.blocks)}",
            f"dfsha_inodes_total {len(self.fsm.inodes)}",
            f"dfsha_datanodes_alive {sum(1 for d in self.fsm.datanodes if self.is_alive(d))}",
        ]
        return "\n".join(lines) + "\n"


__all__ = ["ControlNode", "DfshaError", "PermissionDenied"]
