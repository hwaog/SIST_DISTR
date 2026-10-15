"""Lógica del DataNode: almacenamiento cifrado, tubería de replicación y vínculo con el plano de control."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections import Counter

from ..common.errors import (
    BlockNotFound,
    CorruptBlock,
    DfshaError,
    InvalidArgument,
    PermissionDenied,
    Unauthenticated,
    Unavailable,
    from_dict,
)
from ..common.net import internal_client
from ..common.util import b64d
from ..security import crypto
from .config import DataConfig
from .store import BlockStore, check_id

log = logging.getLogger("dfsha.data")


class RateLimiter:
    """Emula un enlace/disco de capacidad fija: cada transferencia ocupa el enlace n/rate segundos."""

    def __init__(self, bytes_per_s: float) -> None:
        self.rate = float(bytes_per_s)
        self._free_at = 0.0

    async def acquire(self, n: int) -> None:
        if self.rate <= 0:
            return
        now = time.monotonic()
        start = max(now, self._free_at)
        self._free_at = start + n / self.rate
        if start > now:
            await asyncio.sleep(start - now)


class DataNode:
    def __init__(self, cfg: DataConfig) -> None:
        self.cfg = cfg
        self.store = BlockStore(cfg.data_dir, fsync=cfg.fsync, capacity=cfg.capacity)
        self.http = internal_client(cfg.ca, cfg.cert, cfg.key, timeout=30.0)
        self.block_key: bytes | None = None
        self.leader_hint: str | None = None
        self.inflight = 0
        self.metrics: Counter = Counter()
        self._tasks: list[asyncio.Task] = []
        self._resend = asyncio.Event()
        self._sem = asyncio.Semaphore(cfg.max_inflight)
        self.limiter = RateLimiter(cfg.bandwidth_limit)

    # ----------------------------------------------------------------- ciclo de vida
    async def start(self) -> None:
        for fn in (self._control_loop, self._report_loop, self._scrub_loop):
            self._tasks.append(asyncio.create_task(self._guard(fn), name=f"dn-{fn.__name__}"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self.http.aclose()

    async def _guard(self, fn) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tarea %s falló; reintentando", fn.__name__)
                await asyncio.sleep(1.0)

    # ------------------------------------------------------------- tokens
    def unseal(self, token: str, op: str, block_id: str) -> dict:
        if self.block_key is None:
            raise Unavailable("DataNode aún no registrado en el cluster")
        t = crypto.unseal(self.block_key, token)
        if t.get("op") != op or t.get("bid") != block_id:
            raise PermissionDenied("el token no autoriza esta operación sobre este bloque")
        return t

    @staticmethod
    def _bearer(authz: str) -> str:
        if not authz.lower().startswith("bearer "):
            raise Unauthenticated("se requiere token de bloque")
        return authz[7:].strip()

    # -------------------------------------------------------------- escritura
    def precheck_write(self, block_id: str, authz: str, pos: int, content_length: int) -> None:
        """Valida token y tamaño ANTES de leer el cuerpo (evita agotar memoria con cuerpos enormes)."""
        check_id(block_id)
        t = self.unseal(self._bearer(authz), "w", block_id)
        pipe = t["pipe"]
        if not 0 <= pos < len(pipe) or pipe[pos]["id"] != self.cfg.node_id:
            raise PermissionDenied("este DataNode no ocupa esa posición en la tubería del bloque")
        if not 0 < content_length <= t["max"]:
            raise InvalidArgument(f"tamaño de bloque inválido ({content_length} > {t['max']})")

    async def write_from_client(self, block_id: str, authz: str, body: bytes, sha_hdr: str, pos: int) -> dict:
        check_id(block_id)
        token = self._bearer(authz)
        t = self.unseal(token, "w", block_id)
        pipe = t["pipe"]
        if not 0 <= pos < len(pipe) or pipe[pos]["id"] != self.cfg.node_id:
            raise PermissionDenied("este DataNode no ocupa esa posición en la tubería del bloque")
        if len(body) > t["max"] or not body:
            raise InvalidArgument(f"tamaño de bloque inválido ({len(body)} > {t['max']})")
        if hashlib.sha256(body).hexdigest() != sha_hdr:
            raise CorruptBlock("SHA-256 del cuerpo no coincide: corrupción en tránsito")
        dek = b64d(t["dek"])
        loop = asyncio.get_running_loop()
        async with self._sem:
            self.inflight += 1
            try:
                await self.limiter.acquire(len(body))
                blob = await loop.run_in_executor(None, crypto.encrypt_block, dek, block_id, t.get("gen", 1), body)
                await loop.run_in_executor(None, self.store.put, block_id, blob)
                self.metrics["blocks_written"] += 1
                self.metrics["bytes_in"] += len(body)
                downstream = await self._forward(block_id, token, pipe, pos + 1, blob)
            finally:
                self.inflight -= 1
        return {"acks": [self.cfg.node_id, *downstream]}

    async def write_from_peer(self, block_id: str, token: str, blob: bytes, blob_sha: str, pos: int) -> dict:
        check_id(block_id)
        t = self.unseal(token, "w", block_id)
        pipe = t["pipe"]
        if not 0 <= pos < len(pipe) or pipe[pos]["id"] != self.cfg.node_id:
            raise PermissionDenied("posición de tubería inválida")
        if hashlib.sha256(blob).hexdigest() != blob_sha or not crypto.verify_blob(blob):
            raise CorruptBlock("blob corrupto recibido de la tubería")
        loop = asyncio.get_running_loop()
        async with self._sem:
            self.inflight += 1
            try:
                await self.limiter.acquire(len(blob))
                await loop.run_in_executor(None, self.store.put, block_id, blob)
                self.metrics["blocks_replicated_in"] += 1
                downstream = await self._forward(block_id, token, pipe, pos + 1, blob)
            finally:
                self.inflight -= 1
        return {"acks": [self.cfg.node_id, *downstream]}

    async def _forward(self, block_id: str, token: str, pipe: list[dict], start: int, blob: bytes) -> list[str]:
        """Pasa el blob (ya cifrado) al siguiente nodo vivo de la tubería.

        Si el siguiente no responde se salta: el cliente decide con los *acks*
        recibidos si alcanzó el quórum de escritura.  El resto de réplicas las
        completa el ControlNode en segundo plano (re-replicación).
        """
        sha = hashlib.sha256(blob).hexdigest()
        for k in range(start, len(pipe)):
            try:
                r = await self.http.put(
                    f"{pipe[k]['iu']}/internal/blocks/{block_id}",
                    content=blob,
                    headers={"X-Block-Token": token, "X-Pipe-Pos": str(k), "X-Blob-Sha256": sha},
                    timeout=30,
                )
                if r.status_code == 200:
                    return r.json()["acks"]
                log.warning("tubería %s: %s respondió HTTP %s", block_id[:8], pipe[k]["id"], r.status_code)
            except Exception as e:
                log.warning("tubería %s: %s inalcanzable (%r)", block_id[:8], pipe[k]["id"], e)
        return []

    # ---------------------------------------------------------------- lectura
    async def read_for_client(self, block_id: str, authz: str, offset: int, length: int | None) -> tuple[bytes, str]:
        check_id(block_id)
        t = self.unseal(self._bearer(authz), "r", block_id)
        loop = asyncio.get_running_loop()
        async with self._sem:
            self.inflight += 1
            try:
                blob = await loop.run_in_executor(None, self.store.get, block_id)
                await self.limiter.acquire(len(blob))
                try:
                    plain = await loop.run_in_executor(None, crypto.decrypt_block, b64d(t["dek"]), block_id, blob)
                except CorruptBlock:
                    self.metrics["corrupt_on_read"] += 1
                    self.store.quarantine(block_id)
                    asyncio.create_task(self.report_corrupt([block_id]))
                    raise
            finally:
                self.inflight -= 1
        self.metrics["blocks_read"] += 1
        sha = hashlib.sha256(plain).hexdigest()
        if offset or length is not None:
            plain = plain[offset : offset + length if length is not None else None]
        self.metrics["bytes_out"] += len(plain)
        return plain, sha

    async def raw_blob(self, block_id: str) -> bytes:
        blob = await asyncio.get_running_loop().run_in_executor(None, self.store.get, block_id)
        await self.limiter.acquire(len(blob))
        return blob

    # ------------------------------------------------------- órdenes del ControlNode
    async def replicate_from(self, block_id: str, sources: list[str]) -> dict:
        check_id(block_id)
        loop = asyncio.get_running_loop()
        if await loop.run_in_executor(None, self.store.verify, block_id):
            return {"ok": True, "already": True}
        last: Exception | None = None
        for src in sources:
            if src.rstrip("/") == self.cfg.advertise_internal.rstrip("/"):
                continue
            try:
                r = await self.http.get(f"{src}/internal/blocks/{block_id}/raw", timeout=60)
                if r.status_code != 200:
                    last = RuntimeError(f"HTTP {r.status_code}")
                    continue
                blob = r.content
                if not crypto.verify_blob(blob):
                    last = CorruptBlock("fuente con blob corrupto")
                    continue
                await loop.run_in_executor(None, self.store.put, block_id, blob)
                self.metrics["blocks_replicated_in"] += 1
                return {"ok": True, "size": len(blob)}
            except Exception as e:
                last = e
        raise Unavailable(f"no se pudo copiar {block_id} desde ninguna fuente: {last!r}")

    def delete_blocks(self, ids: list[str]) -> dict:
        n = 0
        for b in ids:
            if self.store.delete(check_id(b)):
                n += 1
        self.metrics["blocks_deleted"] += n
        return {"deleted": n}

    # ------------------------------------------------- vínculo con los ControlNodes
    async def _post_any(self, path: str, body: dict, *, timeout: float = 5.0) -> dict | None:
        """POST al líder: prueba el último líder conocido y luego el resto."""
        urls = list(self.cfg.controls)
        if self.leader_hint in urls:
            urls.remove(self.leader_hint)
            urls.insert(0, self.leader_hint)
        for u in urls:
            try:
                r = await self.http.post(f"{u}{path}", json=body, timeout=timeout)
            except Exception:
                continue
            if r.status_code == 200:
                self.leader_hint = u
                return r.json()
            with contextlib.suppress(Exception):
                err = r.json().get("error", {})
                if err.get("code") == "ENOTLEADER":
                    li = (err.get("extra") or {}).get("leader_internal")
                    if li and li in self.cfg.controls:
                        self.leader_hint = li
                    continue
                log.warning("%s %s -> %s", u, path, from_dict(err))
        return None

    async def register(self) -> bool:
        res = await self._post_any(
            "/internal/dn/register",
            {
                "dn": self.cfg.node_id, "zone": self.cfg.zone, "public_url": self.cfg.advertise_public,
                "internal_url": self.cfg.advertise_internal,
            },
        )  # fmt: skip
        if res is None:
            return False
        self.block_key = b64d(res["block_key"])
        log.info("registrado en el cluster (zona=%s)", self.cfg.zone)
        return True

    def hb_payload(self) -> dict:
        return {
            "dn": self.cfg.node_id, "used": self.store.used, "capacity": self.store.capacity(),
            "nblocks": self.store.count, "load": float(self.inflight),
        }  # fmt: skip

    async def _control_loop(self) -> None:
        """Registro + heartbeat a TODOS los ControlNodes (así los seguidores conocen la vivacidad)."""
        while not await self.register():
            await asyncio.sleep(1.0)
        self._resend.set()
        while True:
            results = await asyncio.gather(
                *(self.http.post(f"{u}/internal/dn/heartbeat", json=self.hb_payload(), timeout=3.0) for u in self.cfg.controls),
                return_exceptions=True,
            )
            need_register = False
            for r in results:
                if isinstance(r, Exception) or r.status_code != 200:
                    continue
                j = r.json()
                if j.get("leader"):
                    self.leader_hint = j["leader"]
                if not j.get("registered"):
                    need_register = True
            if need_register:
                if await self.register():
                    self._resend.set()
            await asyncio.sleep(self.cfg.heartbeat_interval)

    async def send_block_report(self) -> dict | None:
        loop = asyncio.get_running_loop()
        items = await loop.run_in_executor(None, self.store.list)
        res = await self._post_any(
            "/internal/dn/blockreport",
            {"dn": self.cfg.node_id, "blocks": [{"id": b, "n": n} for b, n in items]},
            timeout=30,
        )
        if res is not None:
            self.metrics["block_reports"] += 1
        return res

    async def _report_loop(self) -> None:
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._resend.wait(), self.cfg.report_interval)
            self._resend.clear()
            if self.block_key is None:
                continue
            res = await self.send_block_report()
            if res is None:
                self._resend.set()  # reintentar pronto
                await asyncio.sleep(2.0)
            elif res.get("resend"):
                await asyncio.sleep(2.0)
                self._resend.set()

    async def report_corrupt(self, ids: list[str]) -> None:
        await self._post_any("/internal/dn/corrupt", {"dn": self.cfg.node_id, "blocks": ids})

    async def _scrub_loop(self) -> None:
        """Verificación periódica de integridad en reposo (detecta *bit rot*)."""
        loop = asyncio.get_running_loop()
        await asyncio.sleep(self.cfg.scrub_interval)
        while True:
            bad = []
            for bid, _n in await loop.run_in_executor(None, self.store.list):
                if not await loop.run_in_executor(None, self.store.verify, bid):
                    log.error("scrubber: bloque %s CORRUPTO; en cuarentena", bid)
                    self.store.quarantine(bid)
                    bad.append(bid)
                self.metrics["scrubbed"] += 1
                await asyncio.sleep(self.cfg.scrub_pause)
            if bad:
                self.metrics["scrub_corrupt"] += len(bad)
                await self.report_corrupt(bad)
            await asyncio.sleep(self.cfg.scrub_interval)

    async def scrub_once(self) -> list[str]:
        """Un pase completo inmediato (lo usan tests y el comando de administración)."""
        loop = asyncio.get_running_loop()
        bad = []
        for bid, _n in await loop.run_in_executor(None, self.store.list):
            if not await loop.run_in_executor(None, self.store.verify, bid):
                self.store.quarantine(bid)
                bad.append(bid)
        if bad:
            await self.report_corrupt(bad)
        return bad

    def metrics_text(self) -> str:
        lines = [f"dfsha_data_{k} {v}" for k, v in sorted(self.metrics.items())]
        lines += [
            f"dfsha_data_blocks {self.store.count}",
            f"dfsha_data_used_bytes {self.store.used}",
            f"dfsha_data_inflight {self.inflight}",
        ]
        return "\n".join(lines) + "\n"


__all__ = ["DataNode", "DfshaError", "BlockNotFound"]
