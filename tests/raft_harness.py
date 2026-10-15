"""Red en memoria + máquina de estados KV para probar Raft sin sockets."""

from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path

from dfsha.raft.core import RaftConfig, RaftNode, Role
from dfsha.raft.storage import RaftStorage

FAST = RaftConfig(
    election_timeout=(0.25, 0.5),
    heartbeat_interval=0.04,
    rpc_timeout=0.15,
    snapshot_threshold=10_000,
    tick=0.005,
)


class KV:
    """Máquina de estados de prueba: registra el orden exacto de aplicación."""

    def __init__(self) -> None:
        self.data: dict[str, int] = {}
        self.seq: list[int] = []

    def apply(self, index: int, cmd: dict):
        self.data[cmd["k"]] = cmd["v"]
        self.seq.append(cmd["v"])
        return index

    def snapshot(self) -> bytes:
        return json.dumps({"d": self.data, "s": self.seq}).encode()

    def restore(self, b: bytes) -> None:
        o = json.loads(b)
        self.data, self.seq = o["d"], o["s"]


class Net:
    def __init__(self) -> None:
        self.nodes: dict[str, RaftNode] = {}
        self.down: set[str] = set()
        self.blocked: set[tuple[str, str]] = set()

    def isolate(self, nid: str) -> None:
        for other in self.nodes:
            if other != nid:
                self.blocked.add((nid, other))
                self.blocked.add((other, nid))

    def heal(self) -> None:
        self.blocked.clear()

    def transport(self, src: str):
        net = self

        class T:
            async def send(self, peer: str, rpc: str, payload: dict, timeout: float) -> dict:
                if src in net.down or peer in net.down or (src, peer) in net.blocked:
                    await asyncio.sleep(0.01)
                    raise ConnectionError("red caída")
                await asyncio.sleep(random.uniform(0, 0.002))
                node = net.nodes[peer]
                handler = getattr(node, "handle_" + rpc)
                resp = await asyncio.wait_for(handler(json.loads(json.dumps(payload))), timeout)
                if (peer, src) in net.blocked or src in net.down:
                    raise ConnectionError("respuesta perdida")
                return json.loads(json.dumps(resp))

        return T()


class Cluster:
    def __init__(self, root: Path, n: int = 3, cfg: RaftConfig = FAST) -> None:
        self.root, self.cfg, self.net = root, cfg, Net()
        self.ids = [f"n{i}" for i in range(1, n + 1)]
        self.sms: dict[str, KV] = {}

    def _make(self, nid: str) -> RaftNode:
        sm = KV()
        self.sms[nid] = sm
        node = RaftNode(
            nid,
            self.ids,
            RaftStorage(self.root / nid, fsync=False),
            sm,
            self.net.transport(nid),
            self.cfg,
        )
        self.net.nodes[nid] = node
        return node

    async def start(self) -> Cluster:
        for nid in self.ids:
            self._make(nid)
        for node in self.net.nodes.values():
            await node.start()
        return self

    async def stop(self) -> None:
        for node in list(self.net.nodes.values()):
            await node.stop()

    async def crash(self, nid: str) -> None:
        self.net.down.add(nid)
        await self.net.nodes[nid].stop()

    async def restart(self, nid: str) -> RaftNode:
        node = self._make(nid)
        self.net.down.discard(nid)
        await node.start()
        return node

    def leaders(self) -> list[RaftNode]:
        return [
            n
            for nid, n in self.net.nodes.items()
            if nid not in self.net.down and n.role == Role.LEADER
        ]

    async def wait_leader(self, timeout: float = 5.0, exclude: set[str] | None = None) -> RaftNode:
        exclude = exclude or set()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            ls = [n for n in self.leaders() if n.id not in exclude]
            if len(ls) == 1 and ls[0]._ready.is_set():
                return ls[0]
            await asyncio.sleep(0.01)
        raise AssertionError(f"sin líder único: {[n.status() for n in self.net.nodes.values()]}")

    def live(self) -> list[RaftNode]:
        return [n for nid, n in self.net.nodes.items() if nid not in self.net.down]


async def eventually(cond, timeout: float = 5.0, msg: str = "condición no se cumplió"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(msg)
