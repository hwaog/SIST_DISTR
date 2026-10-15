"""Configuración del DataNode."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class DataConfig:
    node_id: str
    controls: list[str]  # URLs INTERNAS (mTLS) de los ControlNodes
    data_dir: Path
    cert: str
    key: str
    ca: str
    advertise_public: str
    advertise_internal: str
    zone: str = "default"
    host: str = "0.0.0.0"
    api_port: int = 7443
    internal_port: int = 7444
    capacity: int = 0  # bytes; 0 = espacio libre del disco
    fsync: bool = True
    heartbeat_interval: float = 2.0
    report_interval: float = 60.0
    scrub_interval: float = 3600.0
    scrub_pause: float = 0.005
    max_inflight: int = 128
    bandwidth_limit: int = 0  # bytes/s (0 = sin límite): emula el tope de red/disco de una VM real en benchmarks
    log_level: str = "info"
