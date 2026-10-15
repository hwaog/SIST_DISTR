"""Configuración del ControlNode."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..raft.core import RaftConfig


@dataclass(frozen=True)
class PeerSpec:
    id: str
    public_url: str
    internal_url: str


def parse_peers(spec: str) -> list[PeerSpec]:
    """``c1=https://h1:8441,https://h1:9441;c2=...`` -> lista de PeerSpec."""
    out = []
    for item in filter(None, (s.strip() for s in spec.replace("\n", ";").split(";"))):
        nid, urls = item.split("=", 1)
        pub, internal = (u.strip().rstrip("/") for u in urls.split(","))
        out.append(PeerSpec(nid.strip(), pub, internal))
    ids = [p.id for p in out]
    if len(set(ids)) != len(ids):
        raise ValueError("ids de ControlNode duplicados")
    return out


@dataclass
class ControlConfig:
    node_id: str
    peers: list[PeerSpec]
    data_dir: Path
    cert: str
    key: str
    ca: str
    cluster_secret: str
    host: str = "0.0.0.0"
    api_port: int = 8443
    internal_port: int = 9443
    admin_password: str | None = None  # sólo para el bootstrap inicial
    fsync: bool = True
    log_level: str = "info"

    # --- política de datos
    default_repl: int = 3
    block_size: int = 4 * 1024 * 1024
    write_quorum: str = "majority"  # "majority" | "all" | "one"
    session_ttl: float = 900.0
    gc_grace: float = 120.0
    token_ttl: float = 8 * 3600.0
    block_token_ttl: float = 900.0

    # --- vivacidad y reparación
    dn_dead_after: float = 10.0
    repl_grace: float = 15.0
    repl_interval: float = 2.0
    gc_interval: float = 2.0
    expire_interval: float = 5.0
    max_repl_streams: int = 8
    balance_interval: float = 30.0  # 0 = balanceador desactivado
    balance_tolerance: float = 0.2  # desbalance tolerado, relativo al uso medio
    balance_batch: int = 8
    report_missing_confirmations: int = 2
    propose_timeout: float = 8.0

    # --- seguridad
    login_max_failures: int = 5
    login_lock_base: float = 15.0

    raft: RaftConfig = field(default_factory=RaftConfig)

    @property
    def me(self) -> PeerSpec:
        return next(p for p in self.peers if p.id == self.node_id)

    def min_replicas(self, repl: int) -> int:
        if self.write_quorum == "all":
            return repl
        if self.write_quorum == "one":
            return 1
        return min(repl, repl // 2 + 1)
