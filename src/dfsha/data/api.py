"""API HTTP del DataNode: pública (clientes con token de bloque) e interna (mTLS)."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from ..common.errors import InvalidArgument, PermissionDenied
from ..common.net import install_error_handlers, peer_of, require_peer
from .node import DataNode


class ReplicateBody(BaseModel):
    block_id: str
    sources: list[str]


class DeleteBody(BaseModel):
    blocks: list[str]


def build_public_app(node: DataNode) -> FastAPI:
    app = FastAPI(title="DFSha — API del DataNode", docs_url=None, redoc_url=None, openapi_url=None)
    install_error_handlers(app)

    @app.get("/health")
    async def health():
        return {"ok": True, "id": node.cfg.node_id, "zone": node.cfg.zone, "registered": node.block_key is not None,
                "blocks": node.store.count, "used": node.store.used}  # fmt: skip

    @app.put("/v1/blocks/{block_id}")
    async def put_block(block_id: str, request: Request):
        cl = request.headers.get("content-length")
        if cl is None or not cl.isdigit():
            raise InvalidArgument("Content-Length requerido")
        pos = int(request.headers.get("x-pipe-pos", "0"))
        node.precheck_write(block_id, request.headers.get("authorization", ""), pos, int(cl))
        body = await request.body()
        sha = request.headers.get("x-block-sha256", "")
        res = await node.write_from_client(block_id, request.headers.get("authorization", ""), body, sha, pos)
        return res

    @app.get("/v1/blocks/{block_id}")
    async def get_block(block_id: str, request: Request, offset: int = 0, length: int | None = None):
        data, sha = await node.read_for_client(block_id, request.headers.get("authorization", ""), offset, length)
        return Response(content=data, media_type="application/octet-stream", headers={"X-Block-Sha256": sha})

    return app


def build_internal_app(node: DataNode) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_error_handlers(app)

    @app.put("/internal/blocks/{block_id}")
    async def put_internal(block_id: str, request: Request):
        require_peer(request, "datanode")
        body = await request.body()
        return await node.write_from_peer(
            block_id,
            request.headers.get("x-block-token", ""),
            body,
            request.headers.get("x-blob-sha256", ""),
            int(request.headers.get("x-pipe-pos", "0")),
        )

    @app.get("/internal/blocks/{block_id}/raw")
    async def get_raw(block_id: str, request: Request):
        require_peer(request, "datanode")
        from .store import check_id

        check_id(block_id)
        blob = await node.raw_blob(block_id)
        node.metrics["blocks_served_raw"] += 1
        return Response(content=blob, media_type="application/octet-stream")

    @app.post("/internal/replicate")
    async def replicate(b: ReplicateBody, request: Request):
        require_peer(request, "controlnode")
        return await node.replicate_from(b.block_id, b.sources)

    @app.post("/internal/delete")
    async def delete(b: DeleteBody, request: Request):
        require_peer(request, "controlnode")
        return await asyncio.get_running_loop().run_in_executor(None, node.delete_blocks, b.blocks)

    @app.post("/internal/scrub")
    async def scrub(request: Request):
        require_peer(request, "controlnode", "admin")
        return {"corrupt": await node.scrub_once()}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(request: Request):
        if peer_of(request) is None:
            raise PermissionDenied("mTLS requerido")
        return node.metrics_text()

    return app
