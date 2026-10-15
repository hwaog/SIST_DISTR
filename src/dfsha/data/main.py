"""Punto de entrada del DataNode:  ``dfsha-data --id dn1 --controls https://c1:9443,...``."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from ..common.net import make_server, serve, tls_files_ok
from ..common.util import parse_size
from .api import build_internal_app, build_public_app
from .config import DataConfig
from .node import DataNode


@click.command(context_settings={"show_default": True})
@click.option("--id", "node_id", required=True, envvar="DFSHA_NODE_ID")
@click.option("--controls", required=True, envvar="DFSHA_CONTROLS", help="URLs internas (mTLS) de los ControlNodes, separadas por coma.")
@click.option("--data-dir", default="./data/data", envvar="DFSHA_DATA_DIR")
@click.option("--cert", required=True, envvar="DFSHA_CERT")
@click.option("--key", required=True, envvar="DFSHA_KEY")
@click.option("--ca", required=True, envvar="DFSHA_CA")
@click.option("--zone", default="default", envvar="DFSHA_ZONE", help="Rack / zona de disponibilidad (para colocación).")
@click.option("--host", default="0.0.0.0", envvar="DFSHA_HOST")
@click.option("--api-port", type=int, default=7443, envvar="DFSHA_API_PORT")
@click.option("--internal-port", type=int, default=7444, envvar="DFSHA_INTERNAL_PORT")
@click.option("--advertise-public", required=True, envvar="DFSHA_ADVERTISE_PUBLIC", help="URL pública con la que los clientes alcanzan este nodo.")
@click.option("--advertise-internal", required=True, envvar="DFSHA_ADVERTISE_INTERNAL", help="URL interna (mTLS) para tráfico entre nodos.")
@click.option("--capacity", default="0", envvar="DFSHA_CAPACITY", help="Capacidad máxima (p. ej. 50GiB). 0 = espacio libre del disco.")
@click.option("--heartbeat", type=float, default=2.0, envvar="DFSHA_HEARTBEAT")
@click.option("--report-interval", type=float, default=60.0)
@click.option("--scrub-interval", type=float, default=3600.0, help="Segundos entre verificaciones de integridad.")
@click.option("--bandwidth-limit", default="0", help="Sólo benchmarks: tope de ancho de banda por nodo (p. ej. 50MiB = 50 MiB/s). 0 = sin tope.")
@click.option("--no-fsync", is_flag=True, help="Sólo pruebas/benchmarks.")
@click.option("--log-level", default="info", envvar="DFSHA_LOG_LEVEL")
def main(node_id, controls, data_dir, cert, key, ca, zone, host, api_port, internal_port, advertise_public,
         advertise_internal, capacity, heartbeat, report_interval, scrub_interval, bandwidth_limit, no_fsync, log_level):  # fmt: skip
    """DataNode de DFSha: almacena bloques cifrados y los replica en tubería."""
    logging.basicConfig(level=log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    tls_files_ok(cert, key, ca)
    cfg = DataConfig(
        node_id=node_id, controls=[u.strip().rstrip("/") for u in controls.split(",") if u.strip()],
        data_dir=Path(data_dir), cert=cert, key=key, ca=ca, advertise_public=advertise_public.rstrip("/"),
        advertise_internal=advertise_internal.rstrip("/"), zone=zone, host=host, api_port=api_port,
        internal_port=internal_port, capacity=parse_size(capacity), fsync=not no_fsync,
        heartbeat_interval=heartbeat, report_interval=report_interval, scrub_interval=scrub_interval,
        bandwidth_limit=parse_size(bandwidth_limit), log_level=log_level,
    )  # fmt: skip
    asyncio.run(_run(cfg))


async def _run(cfg: DataConfig) -> None:
    node = DataNode(cfg)
    await node.start()
    pub = make_server(build_public_app(node), host=cfg.host, port=cfg.api_port, cert=cfg.cert, key=cfg.key, ca=cfg.ca, mtls=False)
    internal = make_server(build_internal_app(node), host=cfg.host, port=cfg.internal_port, cert=cfg.cert, key=cfg.key, ca=cfg.ca, mtls=True)
    logging.getLogger("dfsha.data").info(
        "DataNode %s: API :%d, interna (mTLS) :%d, %d bloques, %d B usados", cfg.node_id, cfg.api_port,
        cfg.internal_port, node.store.count, node.store.used,
    )
    await serve([pub, internal], on_stop=node.stop)


if __name__ == "__main__":  # pragma: no cover
    main()
