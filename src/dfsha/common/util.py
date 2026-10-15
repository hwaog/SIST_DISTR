"""Utilidades pequeñas y sin dependencias internas."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import posixpath
import secrets
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import InvalidArgument

MAX_PATH = 4096
MAX_NAME = 255


def fsync_fd(fd: int) -> None:
    """fsync DURABLE.  En macOS ``os.fsync`` no vacía la caché del disco: hace falta F_FULLFSYNC."""
    if sys.platform == "darwin":
        try:
            import fcntl

            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(fd)


def now() -> float:
    return time.time()


def new_id() -> str:
    """Identificador opaco de 128 bits (hex)."""
    return uuid.uuid4().hex


def new_secret(n: int = 32) -> bytes:
    return secrets.token_bytes(n)


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def normpath(path: str, cwd: str = "/") -> str:
    """Normaliza un path absoluto estilo POSIX y rechaza entradas peligrosas.

    ``..`` se resuelve lexicalmente *antes* de llegar al servidor (el cliente
    envía siempre paths normalizados) y el servidor lo vuelve a validar: jamás
    se confía en el cliente para evitar path traversal.
    """
    if not isinstance(path, str) or not path:
        raise InvalidArgument("path vacío")
    if "\x00" in path or len(path) > MAX_PATH:
        raise InvalidArgument("path inválido")
    if not path.startswith("/"):
        path = posixpath.join(cwd, path)
    norm = posixpath.normpath(path)
    # posixpath.normpath conserva '//' inicial; lo colapsamos.
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    return norm


def split_path(norm: str) -> list[str]:
    """Componentes de un path ya normalizado."""
    parts = [p for p in norm.split("/") if p]
    for p in parts:
        if len(p) > MAX_NAME or p in (".", ".."):
            raise InvalidArgument(f"nombre inválido: {p!r}")
    return parts


def atomic_write(path: str | Path, data: bytes, *, mode: int = 0o600, fsync: bool = True) -> None:
    """Escritura atómica: tmp + fsync + rename + fsync del directorio."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if fsync:
                fsync_fd(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        if fsync:
            fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def fsync_dir(path: str | Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        fsync_fd(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def jdumps(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def parse_size(s: str | int) -> int:
    """'4MiB', '512k', '1g', 1024 -> bytes."""
    if isinstance(s, int):
        return s
    s = s.strip().lower()
    mult = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}
    for suf in ("ib", "b"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(s)
