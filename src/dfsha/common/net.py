"""Infraestructura de red compartida por ControlNode y DataNode.

* ``MTLSProtocol``: protocolo HTTP/1.1 de uvicorn que registra la identidad
  (CN, OU) del certificado de cliente verificado, para autorizar por **rol**
  en los listeners internos.
* ``serve``: ejecuta varios listeners (público + interno) en un único proceso
  con apagado ordenado.
* Fábricas de clientes httpx con mTLS.
* Manejadores de excepciones que mapean ``DfshaError`` → JSON + status HTTP.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import ssl
from collections.abc import Callable, Iterable

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from uvicorn.protocols.http.h11_impl import H11Protocol

from ..pki import client_context, peer_identity, server_context
from .errors import DfshaError, PermissionDenied, from_dict

log = logging.getLogger("dfsha.net")

# (ip, puerto del par) -> (CN, OU) de su certificado verificado
PEERS: dict[tuple[str, int], tuple[str, str]] = {}


class MTLSProtocol(H11Protocol):
    def connection_made(self, transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        so = transport.get_extra_info("ssl_object")
        if so is not None and self.client:
            ident = peer_identity(so.getpeercert())
            if ident:
                PEERS[tuple(self.client)] = ident

    def connection_lost(self, exc) -> None:  # type: ignore[override]
        if self.client:
            PEERS.pop(tuple(self.client), None)
        super().connection_lost(exc)


def peer_of(request: Request) -> tuple[str, str] | None:
    """(CN, OU) del certificado del par, o ``None`` si no hay mTLS."""
    c = request.scope.get("client")
    return PEERS.get((c[0], c[1])) if c else None


def require_peer(request: Request, *roles: str) -> str:
    """Autoriza por rol del certificado; devuelve el CN (id del nodo)."""
    ident = peer_of(request)
    if ident is None or ident[1] not in roles:
        who = f"{ident[0]}/{ident[1]}" if ident else "sin certificado"
        raise PermissionDenied(f"par no autorizado para {request.url.path}: {who}")
    return ident[0]


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):  # las señales las gestiona ``serve`` para todos los listeners
        yield


def make_server(
    app: FastAPI,
    *,
    host: str,
    port: int,
    cert: str,
    key: str,
    ca: str,
    mtls: bool,
    log_level: str = "warning",
) -> uvicorn.Server:
    ctx = server_context(cert, key, ca, require_client_cert=mtls)
    cfg = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level,
        access_log=False,
        ssl_context_factory=lambda _c, _d: ctx,
        http=MTLSProtocol if mtls else "auto",
        timeout_keep_alive=30,
        timeout_graceful_shutdown=3,  # no esperar indefinidamente a conexiones keep-alive al apagar
        lifespan="off",
        server_header=False,
        h11_max_incomplete_event_size=64 * 1024,
    )
    return _Server(cfg)


async def serve(servers: Iterable[uvicorn.Server], on_stop: Callable[[], asyncio.Future] | None = None) -> None:
    servers = list(servers)
    loop = asyncio.get_running_loop()

    def _stop() -> None:
        for s in servers:
            s.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, _stop)
    try:
        await asyncio.gather(*(s.serve() for s in servers))
    finally:
        if on_stop:
            await on_stop()


# ------------------------------------------------------------------ clientes


def internal_client(ca: str, cert: str, key: str, *, timeout: float = 10.0, max_conn: int = 200) -> httpx.AsyncClient:
    """Cliente mTLS para tráfico nodo↔nodo (async)."""
    return httpx.AsyncClient(
        verify=client_context(ca, cert, key),
        timeout=httpx.Timeout(timeout, connect=3.0),
        limits=httpx.Limits(max_connections=max_conn, max_keepalive_connections=max_conn),
    )


def user_client(ca: str, *, timeout: float = 30.0, pool: int = 64) -> httpx.Client:
    """Cliente síncrono para usuarios: verifica al servidor contra la CA."""
    return httpx.Client(
        verify=client_context(ca),
        timeout=httpx.Timeout(timeout, connect=5.0),
        limits=httpx.Limits(max_connections=pool, max_keepalive_connections=pool),
    )


def raise_for_dfsha(resp: httpx.Response) -> None:
    """Traduce una respuesta de error del servidor a la excepción de dominio."""
    if resp.status_code < 400:
        return
    try:
        err = resp.json()["error"]
    except Exception:
        raise DfshaError(f"HTTP {resp.status_code}: {resp.text[:200]}") from None
    raise from_dict(err)


# ------------------------------------------------------------- manejo de errores


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DfshaError)
    async def _dfsha(_req: Request, exc: DfshaError):
        return JSONResponse({"error": exc.to_dict()}, status_code=exc.http_status)

    @app.exception_handler(Exception)
    async def _unhandled(_req: Request, exc: Exception):  # pragma: no cover
        log.exception("error no controlado")
        return JSONResponse({"error": {"code": "EIO", "message": "error interno"}}, status_code=500)


def tls_files_ok(*paths: str) -> None:
    import os

    for p in paths:
        if not os.path.exists(p):
            raise FileNotFoundError(f"falta el archivo TLS {p}; ejecute 'dfsha-pki'")


__all__ = [
    "PEERS", "MTLSProtocol", "peer_of", "require_peer", "make_server", "serve",
    "internal_client", "user_client", "raise_for_dfsha", "install_error_handlers", "ssl",
]  # fmt: skip
