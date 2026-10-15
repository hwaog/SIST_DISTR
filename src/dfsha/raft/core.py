"""Implementación de Raft (Ongaro & Ousterhout, 2014) para el plano de control.

Qué incluye
-----------
* Elección de líder con timeouts aleatorios, **Pre-Vote** y *leader stickiness*
  (un nodo aislado que reconecta no desestabiliza al líder: tesis §9.6/§4.2.3).
* Replicación de log con optimización de retroceso (``conflict_index/term``),
  *group commit* (un fsync por lote) y regla de commit de la sección 5.4.2
  (sólo entradas del término actual se cuentan por mayoría).
* **Persistencia** de término, voto y log (ver ``storage.py``) antes de responder.
* **Snapshots** + ``InstallSnapshot`` para compactar el log y poner al día a
  réplicas rezagadas o nuevas.
* **Lecturas linealizables** del líder: *lease* basado en acks de mayoría (válido
  porque los seguidores no votan por otro candidato mientras escuchan al líder)
  con *fallback* a una ronda de heartbeats (ReadIndex).
* **Check-quorum**: un líder que pierde contacto con la mayoría renuncia, así una
  partición minoritaria nunca sirve lecturas ni acepta escrituras obsoletas.

Qué NO incluye (documentado como limitación): cambios dinámicos de membresía.

El módulo es agnóstico del transporte (``Transport``) y de la máquina de estados
(``StateMachine``), lo que permite probarlo de forma determinista en memoria.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from ..common.errors import DfshaError, NotLeader, Unavailable
from .storage import LogEntry, RaftStorage

log = logging.getLogger("dfsha.raft")


class Role(StrEnum):
    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"


class Transport(Protocol):
    async def send(self, peer: str, rpc: str, payload: dict, timeout: float) -> dict: ...


class StateMachine(Protocol):
    def apply(self, index: int, command: dict) -> Any: ...
    def snapshot(self) -> bytes: ...
    def restore(self, data: bytes) -> None: ...


@dataclass
class RaftConfig:
    election_timeout: tuple[float, float] = (1.0, 2.0)
    heartbeat_interval: float = 0.15
    rpc_timeout: float = 1.0
    snapshot_threshold: int = 2000
    max_entries_per_rpc: int = 256
    lease_ratio: float = 0.6  # fracción del timeout mínimo en la que el lease es válido
    tick: float = 0.02


class RaftNode:
    def __init__(
        self,
        node_id: str,
        peers: list[str],
        storage: RaftStorage,
        sm: StateMachine,
        transport: Transport,
        cfg: RaftConfig | None = None,
        on_role_change: Callable[[Role, int, str | None], None] | None = None,
    ) -> None:
        self.id = node_id
        self.peers = [p for p in peers if p != node_id]
        self.storage = storage
        self.sm = sm
        self.transport = transport
        self.cfg = cfg or RaftConfig()
        self.on_role_change = on_role_change

        # --- estado persistente
        self.current_term, self.voted_for = storage.load_meta()
        self.snapshot_index = 0
        self.snapshot_term = 0
        self.log: list[LogEntry] = []

        # --- estado volátil
        self.role = Role.FOLLOWER
        self.leader_id: str | None = None
        self.commit_index = 0
        self.last_applied = 0
        self.next_index: dict[str, int] = {}
        self.match_index: dict[str, int] = {}
        self._peer_ack: dict[str, float] = {}
        self._last_leader_contact = 0.0
        self._election_deadline = 0.0
        self._ready = asyncio.Event()  # el líder ya comprometió una entrada de su término
        self._noop_index = 0

        self._pending: list[tuple[dict, asyncio.Future]] = []
        self._waiters: dict[int, asyncio.Future] = {}
        self._batch_event = asyncio.Event()
        self._peer_events: dict[str, asyncio.Event] = {}
        self._leader_tasks: list[asyncio.Task] = []
        self._ticker_task: asyncio.Task | None = None
        self._stopped = False
        self._snap_cache: tuple[int, int, bytes] | None = None

        self._load()

    # ------------------------------------------------------------ arranque/parada
    def _load(self) -> None:
        snap = self.storage.load_snapshot()
        if snap:
            self.snapshot_index, self.snapshot_term, data = snap
            self.sm.restore(data)
            self._snap_cache = snap
        raw = self.storage.load_log()
        by_index = {e.index: e for e in raw}
        # Una caída entre guardar el snapshot y reescribir el log puede dejar un log
        # anterior: sólo se conserva si encadena con el snapshot (mismo término en S).
        if self.snapshot_index in by_index and by_index[self.snapshot_index].term != self.snapshot_term:
            usable: list[LogEntry] = []
        else:
            usable = [e for e in raw if e.index > self.snapshot_index]
        if usable and usable[0].index != self.snapshot_index + 1:
            usable = []
        if len(usable) != len(raw):
            self.storage.rewrite_log(usable)
        self.log = usable
        self.commit_index = self.last_applied = self.snapshot_index
        self._reset_election_deadline()

    async def start(self) -> None:
        self._stopped = False
        self._ticker_task = asyncio.create_task(self._ticker(), name=f"raft-ticker-{self.id}")

    async def stop(self) -> None:
        self._stopped = True
        tasks = [self._ticker_task, *self._leader_tasks]
        for t in tasks:
            if t and t is not asyncio.current_task():
                t.cancel()
        for t in tasks:
            if t and t is not asyncio.current_task():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        self._fail_pending(NotLeader(self.leader_id, "nodo detenido"))
        self.storage.close()

    # ---------------------------------------------------------------- helpers de log
    @property
    def last_index(self) -> int:
        return self.snapshot_index + len(self.log)

    @property
    def last_term(self) -> int:
        return self.log[-1].term if self.log else self.snapshot_term

    def term_at(self, index: int) -> int | None:
        if index == self.snapshot_index:
            return self.snapshot_term
        if index < self.snapshot_index or index > self.last_index:
            return None
        return self.log[index - self.snapshot_index - 1].term

    def entry(self, index: int) -> LogEntry:
        return self.log[index - self.snapshot_index - 1]

    @property
    def majority(self) -> int:
        return (len(self.peers) + 1) // 2 + 1

    def is_leader(self) -> bool:
        return self.role == Role.LEADER

    # ----------------------------------------------------------------- timers
    def _reset_election_deadline(self) -> None:
        lo, hi = self.cfg.election_timeout
        self._election_deadline = time.monotonic() + random.uniform(lo, hi)

    def _leader_recently_heard(self) -> bool:
        return time.monotonic() - self._last_leader_contact < self.cfg.election_timeout[0]

    async def _ticker(self) -> None:
        while not self._stopped:
            await asyncio.sleep(self.cfg.tick)
            try:
                if self.role == Role.LEADER:
                    if time.monotonic() - self._quorum_ack_time() > self.cfg.election_timeout[0] * 1.5:
                        log.warning("%s: sin contacto con mayoría, renuncia al liderazgo", self.id)
                        self._become_follower(self.current_term, None)
                elif time.monotonic() >= self._election_deadline:
                    await self._start_election()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - no debe matar el ticker
                log.exception("%s: error en ticker", self.id)

    # --------------------------------------------------------------- transiciones
    def _set_term(self, term: int, voted_for: str | None) -> None:
        if term != self.current_term or voted_for != self.voted_for:
            self.current_term, self.voted_for = term, voted_for
            self.storage.save_meta(term, voted_for)

    def _become_follower(self, term: int, leader: str | None) -> None:
        was_leader = self.role == Role.LEADER
        if term > self.current_term:
            self._set_term(term, None)
        self.role = Role.FOLLOWER
        if leader is not None or was_leader:
            self.leader_id = leader
        me = asyncio.current_task()
        for t in self._leader_tasks:
            if t is not me:
                t.cancel()
        self._leader_tasks = [me] if me in self._leader_tasks else []
        self._ready.clear()
        if was_leader:
            self._fail_pending(NotLeader(leader, "pérdida de liderazgo"))
        self._reset_election_deadline()
        self._notify_role()

    def _notify_role(self) -> None:
        if self.on_role_change:
            try:
                self.on_role_change(self.role, self.current_term, self.leader_id)
            except Exception:  # pragma: no cover
                log.exception("on_role_change falló")

    def _fail_pending(self, exc: Exception) -> None:
        for _cmd, fut in self._pending:
            if not fut.done():
                fut.set_exception(exc)
        self._pending = []
        for fut in self._waiters.values():
            if not fut.done():
                fut.set_exception(exc)
        self._waiters.clear()

    def _become_leader(self) -> None:
        self.role = Role.LEADER
        self.leader_id = self.id
        now = time.monotonic()
        self._peer_ack = dict.fromkeys(self.peers, now)  # período de gracia inicial
        self.next_index = dict.fromkeys(self.peers, self.last_index + 1)
        self.match_index = dict.fromkeys(self.peers, 0)
        self._peer_events = {p: asyncio.Event() for p in self.peers}
        self._ready.clear()
        term = self.current_term
        # No-op: permite comprometer entradas de términos previos (§5.4.2) y
        # establece un punto de lectura seguro.
        noop = LogEntry(term, self.last_index + 1, None)
        self.log.append(noop)
        self.storage.append([noop])
        self._noop_index = noop.index
        self._leader_tasks = [asyncio.create_task(self._batcher(term), name=f"raft-batch-{self.id}")]
        for p in self.peers:
            self._leader_tasks.append(
                asyncio.create_task(self._replicate_loop(p, term), name=f"raft-repl-{self.id}->{p}")
            )
        log.info("%s: LÍDER del término %d (log hasta %d)", self.id, term, self.last_index)
        self._advance_commit()
        self._kick()
        self._notify_role()

    def _kick(self) -> None:
        for ev in self._peer_events.values():
            ev.set()

    # ----------------------------------------------------------------- elecciones
    async def _start_election(self) -> None:
        self._reset_election_deadline()
        if self.peers:
            if not await self._poll_votes("pre_vote", self.current_term + 1):
                return
            if self.role == Role.LEADER or self._leader_recently_heard():
                return
        self.role = Role.CANDIDATE
        self._set_term(self.current_term + 1, self.id)
        term = self.current_term
        self.leader_id = None
        self._notify_role()
        if not self.peers or await self._poll_votes("request_vote", term):
            if self.role == Role.CANDIDATE and self.current_term == term:
                self._become_leader()

    async def _poll_votes(self, kind: str, term: int) -> bool:
        req = {
            "term": term,
            "candidate_id": self.id,
            "last_log_index": self.last_index,
            "last_log_term": self.last_term,
        }
        votes = 1
        need = self.majority
        tasks = {
            asyncio.create_task(self.transport.send(p, kind, req, self.cfg.rpc_timeout)): p
            for p in self.peers
        }
        try:
            for fut in asyncio.as_completed(tasks):
                try:
                    resp = await fut
                except Exception:
                    continue
                if resp["term"] > self.current_term:
                    self._become_follower(resp["term"], None)
                    return False
                if kind == "request_vote" and (self.role != Role.CANDIDATE or self.current_term != term):
                    return False
                if resp.get("granted"):
                    votes += 1
                    if votes >= need:
                        return True
        finally:
            for t in tasks:
                t.cancel()
        return False

    def _log_up_to_date(self, last_term: int, last_index: int) -> bool:
        return (last_term, last_index) >= (self.last_term, self.last_index)

    async def handle_pre_vote(self, req: dict) -> dict:
        grant = (
            req["term"] >= self.current_term
            and self.role != Role.LEADER
            and not self._leader_recently_heard()
            and self._log_up_to_date(req["last_log_term"], req["last_log_index"])
        )
        return {"term": self.current_term, "granted": grant}

    async def handle_request_vote(self, req: dict) -> dict:
        if req["term"] < self.current_term:
            return {"term": self.current_term, "granted": False}
        # Leader stickiness: ignorar (sin tocar el término) si hay líder vivo.
        if self.role == Role.LEADER or self._leader_recently_heard():
            return {"term": self.current_term, "granted": False}
        if req["term"] > self.current_term:
            self._become_follower(req["term"], None)
        grant = self.voted_for in (None, req["candidate_id"]) and self._log_up_to_date(
            req["last_log_term"], req["last_log_index"]
        )
        if grant:
            self._set_term(self.current_term, req["candidate_id"])
            self._reset_election_deadline()
        return {"term": self.current_term, "granted": grant}

    # ------------------------------------------------------------- replicación (líder)
    async def _batcher(self, term: int) -> None:
        """Agrupa propuestas concurrentes en un único append + fsync."""
        while self.role == Role.LEADER and self.current_term == term:
            await self._batch_event.wait()
            self._batch_event.clear()
            batch, self._pending = self._pending, []
            if not batch or self.role != Role.LEADER or self.current_term != term:
                for _c, fut in batch:
                    if not fut.done():
                        fut.set_exception(NotLeader(self.leader_id))
                continue
            entries = []
            for cmd, fut in batch:
                e = LogEntry(term, self.last_index + 1 + len(entries), cmd)
                entries.append(e)
                self._waiters[e.index] = fut
            self.log.extend(entries)
            self.storage.append(entries)
            self._advance_commit()
            self._kick()

    async def _replicate_loop(self, peer: str, term: int) -> None:
        ev = self._peer_events[peer]
        while self.role == Role.LEADER and self.current_term == term:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ev.wait(), self.cfg.heartbeat_interval)
            ev.clear()
            if self.role != Role.LEADER or self.current_term != term:
                return
            try:
                await self._replicate_once(peer, term)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("%s->%s: replicación falló: %r", self.id, peer, e)
                await asyncio.sleep(0.05)

    async def _replicate_once(self, peer: str, term: int) -> None:
        ni = self.next_index[peer]
        if ni <= self.snapshot_index:
            await self._send_snapshot(peer, term)
            return
        prev_i = ni - 1
        prev_t = self.term_at(prev_i)
        start = ni - self.snapshot_index - 1
        entries = self.log[start : start + self.cfg.max_entries_per_rpc]
        req = {
            "term": term,
            "leader_id": self.id,
            "prev_index": prev_i,
            "prev_term": prev_t,
            "entries": [e.to_obj() for e in entries],
            "leader_commit": self.commit_index,
        }
        t_sent = time.monotonic()
        resp = await self.transport.send(peer, "append_entries", req, self.cfg.rpc_timeout)
        if self.role != Role.LEADER or self.current_term != term:
            return
        if resp["term"] > self.current_term:
            self._become_follower(resp["term"], None)
            return
        self._peer_ack[peer] = max(self._peer_ack.get(peer, 0.0), t_sent)
        if resp["success"]:
            m = resp["match_index"]
            if m > self.match_index[peer]:
                self.match_index[peer] = m
            self.next_index[peer] = max(self.next_index[peer], m + 1)
            self._advance_commit()
            if self.next_index[peer] <= self.last_index:
                self._peer_events[peer].set()
        else:
            ci, ct = resp.get("conflict_index", 0), resp.get("conflict_term", 0)
            new_ni = ci or 1
            if ct:
                # último índice del líder con el término conflictivo
                for i in range(self.last_index, self.snapshot_index, -1):
                    if self.term_at(i) == ct:
                        new_ni = i + 1
                        break
            self.next_index[peer] = max(1, min(new_ni, ni - 1))
            self._peer_events[peer].set()

    async def _send_snapshot(self, peer: str, term: int) -> None:
        snap = self._snap_cache or self.storage.load_snapshot()
        if snap is None:  # pragma: no cover - invariante
            raise RuntimeError("snapshot requerido pero ausente")
        idx, sterm, data = snap
        req = {
            "term": term,
            "leader_id": self.id,
            "last_index": idx,
            "last_term": sterm,
            "data": base64.b64encode(data).decode(),
        }
        t_sent = time.monotonic()
        resp = await self.transport.send(peer, "install_snapshot", req, max(self.cfg.rpc_timeout * 10, 10.0))
        if self.role != Role.LEADER or self.current_term != term:
            return
        if resp["term"] > self.current_term:
            self._become_follower(resp["term"], None)
            return
        self._peer_ack[peer] = max(self._peer_ack.get(peer, 0.0), t_sent)
        self.match_index[peer] = max(self.match_index[peer], idx)
        self.next_index[peer] = idx + 1
        self._peer_events[peer].set()

    def _advance_commit(self) -> None:
        if self.role != Role.LEADER:
            return
        matches = sorted([*self.match_index.values(), self.last_index], reverse=True)
        n = matches[self.majority - 1]
        if n > self.commit_index and self.term_at(n) == self.current_term:
            self.commit_index = n
            self._apply_committed()

    # ---------------------------------------------------------------- aplicar
    def _apply_committed(self) -> None:
        while self.last_applied < self.commit_index:
            i = self.last_applied + 1
            e = self.entry(i)
            result: Any = None
            error: Exception | None = None
            if e.command is not None:
                try:
                    result = self.sm.apply(i, e.command)
                except DfshaError as ex:  # error de dominio: determinista en todas las réplicas
                    error = ex
                except Exception as ex:  # bug: lo registramos fuerte
                    log.exception("%s: la máquina de estados falló en %d", self.id, i)
                    error = ex
            self.last_applied = i
            fut = self._waiters.pop(i, None)
            if fut and not fut.done():
                if error is not None:
                    fut.set_exception(error)
                else:
                    fut.set_result(result)
            if i == self._noop_index and self.role == Role.LEADER:
                self._ready.set()
        if self.last_applied - self.snapshot_index >= self.cfg.snapshot_threshold:
            self.take_snapshot()

    def take_snapshot(self) -> None:
        idx = self.last_applied
        if idx <= self.snapshot_index:
            return
        term = self.term_at(idx)
        assert term is not None
        data = self.sm.snapshot()
        self.storage.save_snapshot(idx, term, data)
        self.log = self.log[idx - self.snapshot_index :]
        self.snapshot_index, self.snapshot_term = idx, term
        self.storage.rewrite_log(self.log)
        self._snap_cache = (idx, term, data)
        log.info("%s: snapshot en índice %d (%d bytes)", self.id, idx, len(data))

    # -------------------------------------------------------- RPC del seguidor
    async def handle_append_entries(self, req: dict) -> dict:
        term = req["term"]
        if term < self.current_term:
            return {"term": self.current_term, "success": False}
        if term > self.current_term or self.role != Role.FOLLOWER:
            self._become_follower(term, req["leader_id"])
        self.leader_id = req["leader_id"]
        self._last_leader_contact = time.monotonic()
        self._reset_election_deadline()

        prev_i, prev_t = req["prev_index"], req["prev_term"]
        if prev_i > self.last_index:
            return {"term": self.current_term, "success": False, "conflict_index": self.last_index + 1, "conflict_term": 0}
        if prev_i >= self.snapshot_index:
            local_t = self.term_at(prev_i)
            if local_t != prev_t:
                ci = prev_i
                while ci - 1 > self.snapshot_index and self.term_at(ci - 1) == local_t:
                    ci -= 1
                return {"term": self.current_term, "success": False, "conflict_index": ci, "conflict_term": local_t}

        to_append: list[LogEntry] = []
        for o in req["entries"]:
            e = LogEntry.from_obj(o)
            if e.index <= self.snapshot_index:
                continue
            if not to_append and e.index <= self.last_index:
                if self.term_at(e.index) == e.term:
                    continue
                assert e.index > self.commit_index, "intento de truncar entradas comprometidas"
                self.log = self.log[: e.index - self.snapshot_index - 1]
                self.storage.rewrite_log(self.log)
            to_append.append(e)
        if to_append:
            self.log.extend(to_append)
            self.storage.append(to_append)

        match = prev_i + len(req["entries"])
        new_commit = min(req["leader_commit"], match)
        if new_commit > self.commit_index:
            self.commit_index = new_commit
            self._apply_committed()
        return {"term": self.current_term, "success": True, "match_index": match}

    async def handle_install_snapshot(self, req: dict) -> dict:
        term = req["term"]
        if term < self.current_term:
            return {"term": self.current_term}
        if term > self.current_term or self.role != Role.FOLLOWER:
            self._become_follower(term, req["leader_id"])
        self.leader_id = req["leader_id"]
        self._last_leader_contact = time.monotonic()
        self._reset_election_deadline()
        idx, sterm = req["last_index"], req["last_term"]
        if idx <= self.commit_index:
            return {"term": self.current_term}
        data = base64.b64decode(req["data"])
        self.storage.save_snapshot(idx, sterm, data)
        self.storage.rewrite_log([])
        self.sm.restore(data)
        self.snapshot_index, self.snapshot_term = idx, sterm
        self.log = []
        self.commit_index = self.last_applied = idx
        self._snap_cache = (idx, sterm, data)
        log.info("%s: snapshot instalado hasta %d", self.id, idx)
        return {"term": self.current_term}

    # -------------------------------------------------------------- API pública
    async def propose(self, command: dict, timeout: float = 5.0) -> Any:
        """Replica ``command`` y devuelve el resultado de ``sm.apply`` (en el líder)."""
        if self.role != Role.LEADER:
            raise NotLeader(self.leader_id)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending.append((command, fut))
        self._batch_event.set()
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            raise Unavailable("tiempo agotado esperando el commit (resultado indeterminado)") from None

    def _quorum_ack_time(self) -> float:
        """Instante más reciente tal que una mayoría (incluido yo) confirmó mi liderazgo."""
        acks = sorted([*self._peer_ack.values(), time.monotonic()], reverse=True)
        return acks[self.majority - 1]

    def lease_valid(self) -> bool:
        return (
            self.role == Role.LEADER
            and self._ready.is_set()
            and time.monotonic() - self._quorum_ack_time() < self.cfg.election_timeout[0] * self.cfg.lease_ratio
        )

    async def read_barrier(self, timeout: float = 3.0) -> None:
        """Garantiza que una lectura local del líder es linealizable."""
        if self.role != Role.LEADER:
            raise NotLeader(self.leader_id)
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            raise Unavailable("el líder aún no confirmó su término") from None
        if self.lease_valid():
            return
        t0 = time.monotonic()
        self._kick()
        deadline = t0 + timeout
        while time.monotonic() < deadline:
            if self.role != Role.LEADER:
                raise NotLeader(self.leader_id)
            if self._quorum_ack_time() >= t0:
                return
            await asyncio.sleep(0.005)
        raise Unavailable("no se pudo confirmar el liderazgo con la mayoría")

    def status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role.value,
            "term": self.current_term,
            "leader": self.leader_id,
            "commit_index": self.commit_index,
            "last_applied": self.last_applied,
            "last_index": self.last_index,
            "snapshot_index": self.snapshot_index,
            "peers": self.peers,
            "match_index": dict(self.match_index) if self.role == Role.LEADER else {},
            "lease_valid": self.lease_valid(),
        }
