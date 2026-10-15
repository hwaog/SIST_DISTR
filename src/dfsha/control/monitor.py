"""Tareas de fondo del líder: vivacidad, re-replicación, GC, expiración, bootstrap.

Sólo actúan en el líder Raft (y tras un breve *warm-up*, para que los
heartbeats de los DataNodes ya hayan llegado y no se confunda «aún no he
oído a nadie» con «todos murieron»).  Todo cambio de metadatos pasa por Raft.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time

from ..common.errors import DfshaError
from ..security import auth
from . import placement

log = logging.getLogger("dfsha.monitor")


class Monitors:
    def __init__(self, node) -> None:
        self.n = node
        self.cfg = node.cfg
        self.tasks: list[asyncio.Task] = []
        self.sem = asyncio.Semaphore(self.cfg.max_repl_streams)
        self.inflight: set[tuple[str, str]] = set()
        self.done: list[tuple[str, str]] = []
        self.stats = {"replicated": 0, "replicate_failed": 0, "gc_deleted": 0, "trimmed": 0, "balanced": 0}
        self.leader_since = 0.0
        self._last_sig: tuple[int, int, int] = (0, 0, 0)

    def start(self) -> None:
        for fn in (self._liveness, self._bootstrap, self._repl, self._gc, self._expire, self._balance):
            self.tasks.append(asyncio.create_task(self._guard(fn), name=f"monitor-{fn.__name__}"))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    async def _guard(self, fn) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("monitor %s falló; reintentando", fn.__name__)
                await asyncio.sleep(1.0)

    def ready(self) -> bool:
        """¿Debe este nodo ejecutar tareas de mantenimiento ahora?"""
        n = self.n
        if not n.is_leader() or not n.raft._ready.is_set():
            self.leader_since = 0.0
            return False
        if self.leader_since == 0.0:
            self.leader_since = time.monotonic()
        warm = max(self.cfg.dn_dead_after, 2.0)
        return time.monotonic() - n.boot > warm and time.monotonic() - self.leader_since > 1.0

    # ------------------------------------------------------------------ liveness
    async def _liveness(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            for dn_id, live in self.n.dn.items():
                if live.last_seen and now - live.last_seen >= self.cfg.dn_dead_after:
                    if live.dead_since is None:
                        live.dead_since = live.last_seen + self.cfg.dn_dead_after
                    if not live.announced_dead:
                        live.announced_dead = True
                        log.warning("DataNode %s sin latidos: declarado MUERTO", dn_id)
                        self.n.audit("dn_dead", dn=dn_id)

    # ------------------------------------------------------------------ bootstrap
    async def _bootstrap(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            n = self.n
            if n.is_leader() and n.raft._ready.is_set() and not n.fsm.users and self.cfg.admin_password:
                pw = await asyncio.get_running_loop().run_in_executor(None, auth.hash_password, self.cfg.admin_password)
                with contextlib.suppress(DfshaError):
                    r = await n.propose("bootstrap_admin", pw=pw)
                    if r.get("created"):
                        log.info("usuario 'admin' creado")
                        n.audit("bootstrap_admin")

    # ---------------------------------------------------------------- re-replicación
    async def _repl(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.repl_interval)
            if not self.ready():
                continue
            await self._flush_done()
            n = self.n
            rep = n.fsm.replication_report(n.is_alive)
            launched = 0
            now = time.monotonic()
            sig = (len(rep["under"]), len(rep["over"]), len(rep["lost"]))
            if sig != self._last_sig and any(sig):
                log.info("reparación: %d bloques sub-replicados, %d sobre-replicados, %d sin ninguna réplica viva", *sig)
            self._last_sig = sig
            for bid, live, usable, repl, size in rep["under"]:
                if launched >= 4 * self.cfg.max_repl_streams:
                    break
                if not usable:
                    continue  # perdido: nada que copiar (fsck lo informa)
                b = n.fsm.blocks[bid]
                dead = [d for d in b["reps"] if not n.is_alive(d)]
                if dead and all(now - n.dead_since(d) < self.cfg.repl_grace for d in dead):
                    log.debug("bloque %s: espera de gracia por %s", bid[:8], dead)
                    continue  # fallo posiblemente transitorio: no mover datos aún
                missing = repl - len(live)
                exclude = set(b["reps"]) | {t for (bb, t) in self.inflight if bb == bid}
                targets = placement.choose(n.candidates(), missing, exclude=exclude, size=size)
                if not targets:
                    log.warning("bloque %s: no hay destino para %d réplicas (excluidos=%s, candidatos=%s)", bid[:8], missing, sorted(exclude), [c.id for c in n.candidates()])
                for t in targets:
                    log.info("re-replicando bloque %s: %s -> %s", bid[:8], "|".join(d for d in usable), t.id)
                    srcs = [n.fsm.datanodes[d]["internal_url"] for d in usable if d in n.fsm.datanodes]
                    random.shuffle(srcs)
                    if (bid, t.id) in self.inflight:
                        continue
                    self.inflight.add((bid, t.id))
                    asyncio.create_task(self._copy(bid, srcs, t.id))
                    launched += 1
            # sobre-replicación: recortar donde hay menos espacio libre, SIN perder diversidad de zonas
            trims = []
            nz = self._zones_alive()
            for bid, live, repl in rep["over"][:50]:
                keep = list(live)
                for d in sorted(live, key=lambda d: -(n.dn[d].used / n.dn[d].capacity if n.dn[d].capacity else 0)):
                    if len(keep) <= repl:
                        break
                    rest = [x for x in keep if x != d]
                    if self._zone_count(rest) >= min(2, nz, len(rest)):
                        keep = rest
                        trims.append((bid, d, True))
            if trims:
                with contextlib.suppress(DfshaError):
                    await n.propose("replica_update", removes=trims)
                    self.stats["trimmed"] += len(trims)

    def _zone_count(self, dns: list[str]) -> int:
        return len({self.n.fsm.datanodes[d]["zone"] for d in dns if d in self.n.fsm.datanodes})

    def _zones_alive(self) -> int:
        n = self.n
        return len({rec["zone"] for d, rec in n.fsm.datanodes.items() if rec["state"] == "active" and n.is_alive(d)})

    # --------------------------------------------------------------------- balanceo
    async def _balance(self) -> None:
        """Escala el plano de datos: mueve réplicas desde nodos llenos hacia nodos vacíos.

        Se *añade* una réplica en el nodo menos usado; al quedar el bloque
        sobre-replicado, el recorte de ``_repl`` retira la réplica del nodo más
        usado.  Así el balanceo nunca reduce la redundancia durante el movimiento.
        """
        if self.cfg.balance_interval <= 0:
            await asyncio.sleep(3600 * 24 * 365)
        while True:
            await asyncio.sleep(self.cfg.balance_interval)
            if not self.ready() or self.inflight or self.done:
                continue
            n = self.n
            rep = n.fsm.replication_report(n.is_alive)
            if rep["under"] or rep["over"]:
                continue  # primero reparar, luego balancear
            nodes = [(d, n.dn[d]) for d, rec in n.fsm.datanodes.items() if rec["state"] == "active" and n.is_alive(d) and n.dn[d].capacity]
            if len(nodes) < 2:
                continue
            used = {d: live.used for d, live in nodes}
            util = {d: live.used / live.capacity for d, live in nodes}
            hi, lo = max(util, key=util.get), min(util, key=util.get)
            mean_used = sum(used.values()) / len(used)
            if used[hi] - used[lo] <= max(2 * self.cfg.block_size, self.cfg.balance_tolerance * mean_used):
                continue
            moved = 0
            for b in n.fsm.blocks.values():
                if moved >= self.cfg.balance_batch:
                    break
                if b["state"] != "ok" or hi not in b["reps"] or lo in b["reps"]:
                    continue
                after = [d for d in b["reps"] if d != hi] + [lo]
                if self._zone_count(after) < min(2, self._zones_alive(), len(after)):
                    continue  # el movimiento degradaría la diversidad de zonas
                self.inflight.add((b["id"], lo))
                src = n.fsm.datanodes[hi]["internal_url"]
                asyncio.create_task(self._copy(b["id"], [src], lo, kind="balanced"))
                moved += 1
            if moved:
                log.info("balanceador: %d bloques %s(%.1f%%) -> %s(%.1f%%)", moved, hi, 100 * util[hi], lo, 100 * util[lo])

    async def _copy(self, bid: str, sources: list[str], target: str, kind: str = "replicated") -> None:
        n = self.n
        try:
            async with self.sem:
                rec = n.fsm.datanodes.get(target)
                if not rec:
                    return
                r = await n.http.post(
                    f"{rec['internal_url']}/internal/replicate", json={"block_id": bid, "sources": sources}, timeout=120
                )
                if r.status_code == 200:
                    self.done.append((bid, target))
                    self.stats[kind] += 1
                else:
                    self.stats["replicate_failed"] += 1
                    log.warning("replicar %s -> %s falló: HTTP %s %s", bid[:8], target, r.status_code, r.text[:120])
        except Exception as e:
            self.stats["replicate_failed"] += 1
            log.warning("replicar %s -> %s falló: %r", bid[:8], target, e)
        finally:
            self.inflight.discard((bid, target))
            if self.done:
                with contextlib.suppress(Exception):
                    await self._flush_done()

    async def _flush_done(self) -> None:
        if not self.done or not self.n.is_leader():
            return
        batch, self.done = self.done, []
        try:
            await self.n.propose("replica_update", adds=batch)
        except DfshaError:
            self.done = batch + self.done  # reintentar en el siguiente ciclo

    # ------------------------------------------------------------------------- GC
    async def _gc(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.gc_interval)
            if not self.ready():
                continue
            n = self.n
            now = time.time()
            per_dn: dict[str, list[str]] = {}
            forget = []
            for bid, e in list(n.fsm.pending_deletes.items()):
                if e["nb"] > now:
                    continue
                if not e["dns"]:
                    forget.append(bid)
                for dn in e["dns"]:
                    if dn not in n.fsm.datanodes:
                        continue
                    per_dn.setdefault(dn, []).append(bid)
            acks = []
            for dn, bids in per_dn.items():
                if not n.is_alive(dn):
                    continue
                for i in range(0, len(bids), 200):
                    chunk = bids[i : i + 200]
                    try:
                        r = await n.http.post(
                            f"{n.fsm.datanodes[dn]['internal_url']}/internal/delete", json={"blocks": chunk}, timeout=30
                        )
                        if r.status_code == 200:
                            acks.extend((b, dn) for b in chunk)
                            self.stats["gc_deleted"] += len(chunk)
                    except Exception as e:
                        log.debug("gc %s: %r", dn, e)
            # DataNodes que ya no existen en el registro
            for bid, e in list(n.fsm.pending_deletes.items()):
                if e["dns"] and all(d not in n.fsm.datanodes for d in e["dns"]):
                    forget.append(bid)
            with contextlib.suppress(DfshaError):
                if acks:
                    await n.propose("gc_ack", items=acks)
                if forget:
                    await n.propose("gc_forget", blocks=forget)

    # --------------------------------------------------------------------- expiración
    async def _expire(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.expire_interval)
            if not self.ready():
                continue
            if self.n.fsm.expiring(time.time()):
                with contextlib.suppress(DfshaError):
                    await self.n.propose("expire", grace=self.cfg.gc_grace)
