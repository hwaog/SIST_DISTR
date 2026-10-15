"""Objetos tipo archivo sobre DFSha (RF3: open / read / write / close / lock).

Semántica de consistencia (documentada en ``docs/CONSISTENCY.md``):

* **close-to-open**: los cambios de un escritor se vuelven visibles, de forma
  atómica, al ``close()``/commit; un lector ve la versión vigente al ``open()``.
* Los bloques son **inmutables**; modificar = copy-on-write de los bloques
  afectados + commit de un nuevo layout con control optimista de versión.
* Un lector abierto sigue viendo su versión mientras los bloques viejos no
  hayan sido recolectados (periodo de gracia del GC).
"""

from __future__ import annotations

import io
import threading
from bisect import bisect_right
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor

from ..common import errors as E


class DfshaReader(io.RawIOBase):
    """Lectura con acceso aleatorio, caché LRU de bloques y *read-ahead* paralelo."""

    def __init__(self, client, path: str, *, cache_blocks: int = 8, readahead: int = 3) -> None:
        super().__init__()
        self._ex = None  # si falla la apertura (permiso denegado, no existe), close()/__del__ no deben fallar
        self._lock = None
        self.c, self.path = client, path
        meta = client._locations_all(path)
        self.size, self.version, self.blocks = meta["size"], meta["version"], meta["blocks"]
        self.offs: list[int] = []
        o = 0
        for b in self.blocks:
            self.offs.append(o)
            o += b["len"]
        self.pos = 0
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._pending: dict[int, Future] = {}
        self._cap, self._ra = cache_blocks, readahead
        self._lk = threading.Lock()
        self._ex = ThreadPoolExecutor(max(2, readahead + 1), thread_name_prefix="dfsha-ra")

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        if base + offset < 0:
            raise ValueError("posición negativa")
        self.pos = base + offset
        return self.pos

    def _fetch(self, i: int) -> bytes:
        return self.c._fetch_block(self.path, self.blocks[i], i)

    def _block(self, i: int) -> bytes:
        with self._lk:
            if i in self._cache:
                self._cache.move_to_end(i)
                data = self._cache[i]
                fut = None
            else:
                fut = self._pending.get(i)
                if fut is None:
                    fut = self._pending[i] = self._ex.submit(self._fetch, i)
                data = None
            for j in range(i + 1, min(i + 1 + self._ra, len(self.blocks))):  # read-ahead secuencial
                if j not in self._cache and j not in self._pending:
                    self._pending[j] = self._ex.submit(self._fetch, j)
        if data is None:
            data = fut.result()  # type: ignore[union-attr]
        with self._lk:
            self._pending.pop(i, None)
            self._cache[i] = data
            self._cache.move_to_end(i)
            while len(self._cache) > self._cap:
                self._cache.popitem(last=False)
            for j, f in list(self._pending.items()):  # recoger read-aheads terminados
                if f.done() and f.exception() is None:
                    self._cache[j] = f.result()
                    del self._pending[j]
            while len(self._cache) > self._cap:
                self._cache.popitem(last=False)
        return data

    def readinto(self, b) -> int:
        if self.pos >= self.size:
            return 0
        n = min(len(b), self.size - self.pos)
        done = 0
        while done < n:
            i = bisect_right(self.offs, self.pos) - 1
            data = self._block(i)
            start = self.pos - self.offs[i]
            take = min(len(data) - start, n - done)
            b[done : done + take] = data[start : start + take]
            done += take
            self.pos += take
        return done

    def pread(self, offset: int, n: int) -> bytes:
        """Lectura puntual sin mover el cursor; trozos pequeños usan lectura parcial en el DataNode."""
        if offset >= self.size or n <= 0:
            return b""
        n = min(n, self.size - offset)
        out = bytearray()
        while len(out) < n:
            i = bisect_right(self.offs, offset + len(out)) - 1
            start = offset + len(out) - self.offs[i]
            take = min(self.blocks[i]["len"] - start, n - len(out))
            with self._lk:
                cached = self._cache.get(i)
            if cached is not None:
                out += cached[start : start + take]
            elif take <= 64 * 1024:
                out += self.c._fetch_block(self.path, self.blocks[i], i, offset=start, length=take)
            else:
                out += self._block(i)[start : start + take]
        return bytes(out)

    def close(self) -> None:
        if not self.closed:
            if self._ex is not None:
                self._ex.shutdown(wait=False, cancel_futures=True)
            if self._lock:
                self._lock.release()
        super().close()


class _BaseWriter(io.RawIOBase):
    """Escritura secuencial por bloques, con subida paralela acotada en memoria."""

    _MODE = "replace"

    def __init__(self, client, path: str, *, lock=None, repl=None, block_size=None, overwrite=True,
                 if_version=None, mode_bits=0o644) -> None:  # fmt: skip
        super().__init__()
        self.c, self.path, self.lock = client, path, lock
        self.if_version = if_version
        self.sess = client._begin(path, self._MODE, repl=repl, block_size=block_size, overwrite=overwrite,
                                  lock_id=lock.lock_id if lock else None, mode_bits=mode_bits)  # fmt: skip
        self.sid, self.bs, self.min_repl = self.sess["sid"], self.sess["block_size"], self.sess["min_repl"]
        self.existing = [{"id": b["id"]} for b in self.sess.get("blocks", [])]
        self.existing_size = sum(b["len"] for b in self.sess.get("blocks", []))
        self.buf = bytearray()
        self.total = 0
        self.entries: dict[int, dict] = {}
        self._allocs: deque = deque()
        self._next = 0
        self._ex = ThreadPoolExecutor(client.workers, thread_name_prefix="dfsha-up")
        self._sem = threading.Semaphore(client.workers * 2)
        self._futs: list[Future] = []
        self._stop_keepalive = client._keepalive(self.sid, self.sess.get("session_ttl", 900))
        self._aborted = False
        self.committed: dict | None = None

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        if self.closed:
            raise ValueError("archivo cerrado")
        if self._aborted:
            return len(b)  # tras abort() el contenido pendiente se descarta
        n = len(b)
        self.buf += b
        self.total += n
        while len(self.buf) >= self.bs:
            self._submit(bytes(self.buf[: self.bs]))
            del self.buf[: self.bs]
        return n

    def _next_alloc(self) -> dict:
        if not self._allocs:
            self._allocs.extend(self.c._alloc(self.sid, 16))
        return self._allocs.popleft()

    def _submit(self, data: bytes) -> None:
        for f in self._futs:
            if f.done() and f.exception():
                raise f.exception()  # type: ignore[misc]
        idx, alloc = self._next, self._next_alloc()
        self._next += 1
        self._sem.acquire()

        def work() -> None:
            self.entries[idx] = self.c._upload_block(self.sid, alloc, data, self.min_repl)

        fut = self._ex.submit(work)
        fut.add_done_callback(lambda _f: self._sem.release())
        self._futs.append(fut)

    def abort(self) -> None:
        if self._aborted:
            return
        self._aborted = True
        try:
            self._ex.shutdown(wait=True, cancel_futures=True)
            self.c._call("POST", f"/v1/files/{self.sid}/abort")
        except Exception:
            pass
        finally:
            self._stop_keepalive()
            if self.lock:
                self.lock.release()

    def close(self) -> None:
        if self.closed:
            return
        try:
            if not self._aborted:
                if self.buf:
                    self._submit(bytes(self.buf))
                    self.buf.clear()
                for f in self._futs:
                    f.result()
                layout = [*self.existing, *(self.entries[i] for i in sorted(self.entries))]
                self.committed = self.c._call(
                    "POST", f"/v1/files/{self.sid}/commit",
                    json={"layout": layout, "size": self.existing_size + self.total, "if_version": self.if_version,
                          "lock_id": self.lock.lock_id if self.lock else None},
                    mutating=True,
                )  # fmt: skip
        except BaseException:
            self.abort()
            raise
        finally:
            if not self._aborted:
                self._stop_keepalive()
                self._ex.shutdown(wait=False)
                if self.lock:
                    self.lock.release()
            super().close()

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.abort()
        self.close()


class DfshaWriter(_BaseWriter):
    _MODE = "replace"


class DfshaAppender(_BaseWriter):
    _MODE = "append"


class DfshaUpdater(io.RawIOBase):
    """Lectura/escritura aleatoria (``r+b``) por *copy-on-write* de bloques.

    Los bloques modificados se mantienen en memoria hasta ``close()``
    (acotado por ``max_dirty_bytes``); al cerrar se suben como bloques nuevos y
    se confirma un layout nuevo con CAS sobre la versión base.
    """

    def __init__(self, client, path: str, *, lock=None, max_dirty_bytes: int = 512 << 20) -> None:
        super().__init__()
        self.c, self.path, self.lock = client, path, lock
        self.sess = client._begin(path, "update", lock_id=lock.lock_id if lock else None)
        self.sid, self.bs, self.min_repl = self.sess["sid"], self.sess["block_size"], self.sess["min_repl"]
        meta = client._locations_all(path)
        if meta["version"] != self.sess["version"]:
            raise E.Conflict("el archivo cambió mientras se abría")
        self.blocks = meta["blocks"]  # bloques con ubicaciones (para leer los originales)
        self.ids = [b["block_id"] for b in self.blocks]
        self.lens = [b["len"] for b in self.blocks]
        self.dirty: dict[int, bytearray] = {}
        self.max_dirty = max_dirty_bytes
        self.pos = 0
        self._aborted = False
        self._changed = False
        self._stop_keepalive = client._keepalive(self.sid, self.sess.get("session_ttl", 900))

    @property
    def size(self) -> int:
        return sum(self.lens)

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        if base + offset < 0:
            raise ValueError("posición negativa")
        self.pos = base + offset
        return self.pos

    def _locate(self, pos: int) -> tuple[int, int]:
        o = 0
        for i, n in enumerate(self.lens):
            if pos < o + n:
                return i, pos - o
            o += n
        return len(self.lens), 0

    def _buf(self, i: int) -> bytearray:
        if i not in self.dirty:
            if sum(len(v) for v in self.dirty.values()) + self.lens[i] > self.max_dirty:
                raise E.InvalidArgument("demasiados datos modificados en memoria; use 'wb' para reescrituras masivas")
            self.dirty[i] = bytearray(self.c._fetch_block(self.path, self.blocks[i], i))
        return self.dirty[i]

    def readinto(self, b) -> int:
        size = self.size
        if self.pos >= size:
            return 0
        n = min(len(b), size - self.pos)
        done = 0
        while done < n:
            i, off = self._locate(self.pos)
            data = self.dirty.get(i) or self.c._fetch_block(self.path, self.blocks[i], i)
            take = min(len(data) - off, n - done)
            b[done : done + take] = data[off : off + take]
            done += take
            self.pos += take
        return done

    def write(self, data) -> int:
        mv = memoryview(bytes(data))
        n = len(mv)
        if n == 0:
            return 0
        self._changed = True
        if self.pos > self.size:  # hueco más allá del EOF: se rellena con ceros
            target, self.pos = self.pos, self.size
            self.write(bytes(target - self.pos))  # deja el cursor en `target`
        done = 0
        while done < n:
            size = self.size
            if self.pos < size:
                i, off = self._locate(self.pos)
                buf = self._buf(i)
                take = min(len(buf) - off, n - done)
                buf[off : off + take] = mv[done : done + take]
            else:  # EOF: extender el último bloque o crear uno nuevo
                if self.lens and self.lens[-1] < self.bs:
                    i = len(self.lens) - 1
                    buf = self._buf(i)
                    take = min(self.bs - len(buf), n - done)
                    buf += mv[done : done + take]
                    self.lens[i] = len(buf)
                else:
                    take = min(self.bs, n - done)
                    i = len(self.lens)
                    self.dirty[i] = bytearray(mv[done : done + take])
                    self.lens.append(take)
                    self.ids.append("")  # se asigna al subir
                    self.blocks.append({})
            done += take
            self.pos += take
        return n

    def truncate(self, size: int | None = None) -> int:
        size = self.pos if size is None else size
        cur = self.size
        if size != cur:
            self._changed = True
        if size > cur:
            p = self.pos
            self.pos = cur
            self.write(bytes(size - cur))
            self.pos = p
        elif size < cur:
            i, off = self._locate(size) if size < cur else (len(self.lens), 0)
            if off == 0:
                keep = i
            else:
                buf = self._buf(i)
                del buf[off:]
                self.lens[i] = off
                keep = i + 1
            for j in range(keep, len(self.lens)):
                self.dirty.pop(j, None)
            del self.lens[keep:], self.ids[keep:], self.blocks[keep:]
        return size

    def abort(self) -> None:
        if self._aborted:
            return
        self._aborted = True
        try:
            self.c._call("POST", f"/v1/files/{self.sid}/abort")
        except Exception:
            pass
        finally:
            self._stop_keepalive()
            if self.lock:
                self.lock.release()

    def close(self) -> None:
        if self.closed:
            return
        try:
            if not self._aborted:
                if not self._changed:
                    self.abort()  # sin cambios: no se crea una versión nueva
                else:
                    self._commit()
        except BaseException:
            self.abort()
            raise
        finally:
            if not self._aborted:
                self._stop_keepalive()
                if self.lock:
                    self.lock.release()
            super().close()

    def _commit(self) -> None:
        idxs = sorted(self.dirty)
        allocs = self.c._alloc(self.sid, len(idxs)) if idxs else []
        new: dict[int, dict] = {}
        with ThreadPoolExecutor(self.c.workers) as ex:
            futs = {ex.submit(self.c._upload_block, self.sid, allocs[k], bytes(self.dirty[i]), self.min_repl): i for k, i in enumerate(idxs)}
            for f, i in futs.items():
                new[i] = f.result()
        layout = [new[i] if i in new else {"id": self.ids[i]} for i in range(len(self.lens))]
        self.c._call(
            "POST", f"/v1/files/{self.sid}/commit",
            json={"layout": layout, "size": sum(self.lens), "if_version": self.sess["version"],
                  "lock_id": self.lock.lock_id if self.lock else None},
            mutating=True,
        )  # fmt: skip

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.abort()
        self.close()
