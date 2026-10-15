"""Almacén local de bloques (blobs cifrados autoverificables)."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from ..common.errors import BlockNotFound, InvalidArgument, NoSpace
from ..common.util import fsync_dir, fsync_fd
from ..security.crypto import verify_blob

BLOCK_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def check_id(block_id: str) -> str:
    """Valida el id: sin esto un id como '../../x' permitiría escapar del directorio."""
    if not BLOCK_ID_RE.match(block_id):
        raise InvalidArgument("block_id inválido")
    return block_id


class BlockStore:
    def __init__(self, root: str | Path, *, fsync: bool = True, capacity: int = 0) -> None:
        self.root = Path(root)
        self.blocks_dir = self.root / "blocks"
        self.tmp_dir = self.root / "tmp"
        self.corrupt_dir = self.root / "corrupt"
        for d in (self.blocks_dir, self.tmp_dir, self.corrupt_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.fsync = fsync
        self.capacity_cfg = capacity
        self._lock = threading.Lock()
        for f in self.tmp_dir.iterdir():  # restos de escrituras interrumpidas
            f.unlink(missing_ok=True)
        self.used = 0
        self.count = 0
        for _bid, size in self.list():
            self.used += size
            self.count += 1

    # ---------------------------------------------------------------- consultas
    def path(self, block_id: str) -> Path:
        check_id(block_id)
        return self.blocks_dir / block_id[:2] / f"{block_id}.blk"

    def exists(self, block_id: str) -> bool:
        return self.path(block_id).exists()

    def list(self) -> list[tuple[str, int]]:
        out = []
        for sub in self.blocks_dir.iterdir():
            if sub.is_dir():
                for f in sub.iterdir():
                    if f.suffix == ".blk":
                        out.append((f.stem, f.stat().st_size))
        return out

    def capacity(self) -> int:
        if self.capacity_cfg:
            return self.capacity_cfg
        import shutil

        return self.used + shutil.disk_usage(self.root).free

    # ----------------------------------------------------------------- mutación
    def put(self, block_id: str, blob: bytes) -> None:
        p = self.path(block_id)
        if self.capacity_cfg and self.used + len(blob) > self.capacity_cfg:
            raise NoSpace("DataNode sin capacidad")
        p.parent.mkdir(exist_ok=True)
        tmp = self.tmp_dir / f"{block_id}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
            f.flush()
            if self.fsync:
                fsync_fd(f.fileno())
        with self._lock:
            old = p.stat().st_size if p.exists() else None
            os.replace(tmp, p)
            if old is None:
                self.used += len(blob)
                self.count += 1
            else:
                self.used += len(blob) - old
        if self.fsync:
            fsync_dir(p.parent)

    def get(self, block_id: str) -> bytes:
        try:
            return self.path(block_id).read_bytes()
        except FileNotFoundError:
            raise BlockNotFound(block_id) from None

    def delete(self, block_id: str) -> bool:
        p = self.path(block_id)
        with self._lock:
            try:
                size = p.stat().st_size
                p.unlink()
            except FileNotFoundError:
                return False
            self.used -= size
            self.count -= 1
        return True

    def quarantine(self, block_id: str) -> None:
        p = self.path(block_id)
        with self._lock:
            try:
                size = p.stat().st_size
                os.replace(p, self.corrupt_dir / p.name)
            except FileNotFoundError:
                return
            self.used -= size
            self.count -= 1

    def verify(self, block_id: str) -> bool:
        try:
            return verify_blob(self.get(block_id))
        except BlockNotFound:
            return False
