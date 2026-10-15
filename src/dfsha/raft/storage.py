"""Persistencia durable de Raft (término, voto, log, snapshot).

Archivos en ``<dir>``:

* ``meta.json``      — ``{"term": int, "voted_for": str|null}``  (escritura atómica)
* ``log.bin``        — registros ``len(u32) | crc32(u32) | json`` anexados (append-only)
* ``snapshot.bin``   — estado serializado de la máquina de estados
* ``snapshot.json``  — ``{"index", "term"}`` del último entry incluido

El log se reescribe (compactación) sólo al instalar/tomar un snapshot.  Un
registro truncado o con CRC inválido al final del archivo (caída durante la
escritura) se descarta al recuperar: Raft nunca confirmó esa entrada.
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.util import atomic_write, fsync_fd

_REC = struct.Struct(">II")


@dataclass
class LogEntry:
    term: int
    index: int
    command: dict | None  # None => no-op del líder

    def to_obj(self) -> dict[str, Any]:
        return {"t": self.term, "i": self.index, "c": self.command}

    @classmethod
    def from_obj(cls, o: dict[str, Any]) -> LogEntry:
        return cls(o["t"], o["i"], o["c"])


class RaftStorage:
    def __init__(self, directory: str | Path, *, fsync: bool = True) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fsync = fsync
        self._log_f = None  # type: ignore[var-annotated]

    # ----------------------------------------------------------------- meta
    def load_meta(self) -> tuple[int, str | None]:
        p = self.dir / "meta.json"
        if not p.exists():
            return 0, None
        o = json.loads(p.read_text())
        return int(o["term"]), o.get("voted_for")

    def save_meta(self, term: int, voted_for: str | None) -> None:
        atomic_write(
            self.dir / "meta.json",
            json.dumps({"term": term, "voted_for": voted_for}).encode(),
            fsync=self.fsync,
        )

    # ------------------------------------------------------------------ log
    def load_log(self) -> list[LogEntry]:
        p = self.dir / "log.bin"
        entries: list[LogEntry] = []
        if not p.exists():
            return entries
        data = p.read_bytes()
        pos, good = 0, 0
        while pos + _REC.size <= len(data):
            ln, crc = _REC.unpack_from(data, pos)
            body = data[pos + _REC.size : pos + _REC.size + ln]
            if len(body) < ln or zlib.crc32(body) != crc:
                break
            entries.append(LogEntry.from_obj(json.loads(body)))
            pos += _REC.size + ln
            good = pos
        if good != len(data):  # cola corrupta/truncada: la descartamos
            with open(p, "r+b") as f:
                f.truncate(good)
        return entries

    def _open_log(self):
        if self._log_f is None:
            self._log_f = open(self.dir / "log.bin", "ab")  # noqa: SIM115
        return self._log_f

    def append(self, entries: list[LogEntry]) -> None:
        """Anexa y hace fsync UNA vez (group commit)."""
        if not entries:
            return
        f = self._open_log()
        buf = bytearray()
        for e in entries:
            body = json.dumps(e.to_obj(), separators=(",", ":")).encode()
            buf += _REC.pack(len(body), zlib.crc32(body)) + body
        f.write(buf)
        f.flush()
        if self.fsync:
            fsync_fd(f.fileno())

    def rewrite_log(self, entries: list[LogEntry]) -> None:
        """Reemplaza el log completo (truncado por conflicto o compactación)."""
        if self._log_f is not None:
            self._log_f.close()
            self._log_f = None
        buf = bytearray()
        for e in entries:
            body = json.dumps(e.to_obj(), separators=(",", ":")).encode()
            buf += _REC.pack(len(body), zlib.crc32(body)) + body
        atomic_write(self.dir / "log.bin", bytes(buf), fsync=self.fsync)

    # ------------------------------------------------------------- snapshot
    def load_snapshot(self) -> tuple[int, int, bytes] | None:
        meta, data = self.dir / "snapshot.json", self.dir / "snapshot.bin"
        if not meta.exists() or not data.exists():
            return None
        o = json.loads(meta.read_text())
        return int(o["index"]), int(o["term"]), data.read_bytes()

    def save_snapshot(self, index: int, term: int, data: bytes) -> None:
        # Orden importante: primero los datos, luego el puntero.
        atomic_write(self.dir / "snapshot.bin", data, fsync=self.fsync)
        atomic_write(
            self.dir / "snapshot.json", json.dumps({"index": index, "term": term}).encode(), fsync=self.fsync
        )

    def close(self) -> None:
        if self._log_f is not None:
            self._log_f.close()
            self._log_f = None
