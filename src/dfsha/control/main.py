"""Punto de entrada del ControlNode:  ``dfsha-control --id c1 --peers ...``."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse

import click

from ..common.net import make_server, serve, tls_files_ok
from ..common.util import parse_size
from ..raft.core import RaftConfig
from .api import build_internal_app, build_public_app
from .config import ControlConfig, parse_peers
from .node import ControlNode


def _port(url: str) -> int:
    return urlparse(url).port or 443


@click.command(context_settings={"show_default": True, "auto_envvar_prefix": "DFSHA"})
@click.option("--id", "node_id", required=True, envvar="DFSHA_NODE_ID", help="Identificador de este ControlNode.")
@click.option("--peers", required=True, envvar="DFSHA_PEERS", help="'id=url_publica,url_interna;...' de TODOS los ControlNodes (incluido éste).")
@click.option("--data-dir", default="./data/control", envvar="DFSHA_DATA_DIR")
@click.option("--cert", required=True, envvar="DFSHA_CERT")
@click.option("--key", required=True, envvar="DFSHA_KEY")
@click.option("--ca", required=True, envvar="DFSHA_CA")
@click.option("--cluster-secret", required=True, envvar="DFSHA_CLUSTER_SECRET_FILE", help="Archivo con el secreto maestro.")
@click.option("--host", default="0.0.0.0", envvar="DFSHA_HOST")
@click.option("--api-port", type=int, default=None, help="Por defecto, el puerto de la URL pública propia.")
@click.option("--internal-port", type=int, default=None, help="Por defecto, el puerto de la URL interna propia.")
@click.option("--admin-password", envvar="DFSHA_ADMIN_PASSWORD", default=None, help="Clave del usuario 'admin' en el primer arranque.")
@click.option("--admin-password-file", envvar="DFSHA_ADMIN_PASSWORD_FILE", default=None, type=click.Path())
@click.option("--repl", "default_repl", type=int, default=3, envvar="DFSHA_REPL", help="Factor de replicación por defecto.")
@click.option("--block-size", default="4MiB", envvar="DFSHA_BLOCK_SIZE")
@click.option("--write-quorum", type=click.Choice(["majority", "all", "one"]), default="majority", envvar="DFSHA_WRITE_QUORUM")
@click.option("--election-timeout", nargs=2, type=float, default=(1.0, 2.0), help="min max, segundos.")
@click.option("--heartbeat", type=float, default=0.15, help="Intervalo de heartbeat Raft (s).")
@click.option("--snapshot-threshold", type=int, default=2000)
@click.option("--dn-dead-after", type=float, default=10.0, envvar="DFSHA_DN_DEAD_AFTER")
@click.option("--repl-grace", type=float, default=15.0, envvar="DFSHA_REPL_GRACE")
@click.option("--gc-grace", type=float, default=120.0, envvar="DFSHA_GC_GRACE")
@click.option("--balance-interval", type=float, default=30.0, envvar="DFSHA_BALANCE_INTERVAL", help="Segundos entre pasadas del balanceador (0 = desactivado).")
@click.option("--balance-tolerance", type=float, default=0.2, help="Desbalance de uso tolerado (fracción del uso medio).")
@click.option("--no-fsync", is_flag=True, help="Sólo para pruebas/benchmarks: desactiva fsync del log Raft.")
@click.option("--log-level", default="info", envvar="DFSHA_LOG_LEVEL")
def main(node_id, peers, data_dir, cert, key, ca, cluster_secret, host, api_port, internal_port,
         admin_password, admin_password_file, default_repl, block_size, write_quorum, election_timeout,
         heartbeat, snapshot_threshold, dn_dead_after, repl_grace, gc_grace, balance_interval, balance_tolerance, no_fsync, log_level):  # fmt: skip
    """ControlNode de DFSha: metadatos replicados con Raft + coordinación de DataNodes."""
    logging.basicConfig(level=log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    specs = parse_peers(peers)
    me = next((p for p in specs if p.id == node_id), None)
    if me is None:
        raise click.UsageError(f"--id {node_id} no figura en --peers")
    tls_files_ok(cert, key, ca, cluster_secret)
    if admin_password_file:
        admin_password = Path(admin_password_file).read_text().strip()
    cfg = ControlConfig(
        node_id=node_id, peers=specs, data_dir=Path(data_dir), cert=cert, key=key, ca=ca,
        cluster_secret=cluster_secret, host=host, api_port=api_port or _port(me.public_url),
        internal_port=internal_port or _port(me.internal_url), admin_password=admin_password,
        fsync=not no_fsync, log_level=log_level, default_repl=default_repl, block_size=parse_size(block_size),
        write_quorum=write_quorum, dn_dead_after=dn_dead_after, repl_grace=repl_grace, gc_grace=gc_grace,
        balance_interval=balance_interval, balance_tolerance=balance_tolerance,
        raft=RaftConfig(election_timeout=tuple(election_timeout), heartbeat_interval=heartbeat,
                        snapshot_threshold=snapshot_threshold),
    )  # fmt: skip
    asyncio.run(_run(cfg))


async def _run(cfg: ControlConfig) -> None:
    node = ControlNode(cfg)
    await node.start()
    pub = make_server(build_public_app(node), host=cfg.host, port=cfg.api_port, cert=cfg.cert, key=cfg.key, ca=cfg.ca, mtls=False, log_level="warning")
    internal = make_server(build_internal_app(node), host=cfg.host, port=cfg.internal_port, cert=cfg.cert, key=cfg.key, ca=cfg.ca, mtls=True, log_level="warning")
    logging.getLogger("dfsha.control").info(
        "ControlNode %s: API pública :%d, interna (mTLS) :%d, %d pares", cfg.node_id, cfg.api_port, cfg.internal_port, len(cfg.peers)
    )
    await serve([pub, internal], on_stop=node.stop)


if __name__ == "__main__":  # pragma: no cover
    main()
