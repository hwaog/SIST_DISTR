"""SDK de DFSha: cliente síncrono con transparencia de ubicación, replicación y fallos.

Transparencia que ofrece al programa de aplicación
--------------------------------------------------
* **Acceso**: ``open()`` devuelve objetos tipo archivo (``read/seek/write``);
  ``put/get`` y ``send/receive`` cubren la transferencia de archivos completos.
* **Ubicación**: el cliente jamás conoce de antemano dónde están los datos; los
  descubre dinámicamente con el ControlNode (nunca de forma estática).
* **Replicación y fallos**: lecturas con *failover* entre réplicas y verificación
  SHA-256 extremo a extremo; escrituras con reubicación automática si un
  DataNode cae a mitad de la tubería; reintento transparente ante cambio de
  líder Raft (con ``X-Request-Id`` para que el reintento sea idempotente).
* **Concurrencia**: transferencias paralelas por bloque (``workers``).
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

from ..common import errors as E
from ..common.net import user_client
from ..common.util import new_id, normpath

Progress = Callable[[int, int], None]
_TRANSPORT_ERRORS = (httpx.TransportError,)


class Client:
    def __init__(
        self,
        endpoints: list[str],
        ca: str,
        *,
        token: str | None = None,
        api_key: str | None = None,
        credentials: tuple[str, str] | None = None,
        zone: str | None = None,
        workers: int = 8,
        timeout: float = 30.0,
        retry_window: float = 30.0,
        cwd: str = "/",
    ) -> None:
        if not endpoints:
            raise E.InvalidArgument("se requiere al menos un endpoint de ControlNode")
        self.endpoints = [e.rstrip("/") for e in endpoints]
        self.ca = ca
        self.token = token
        self.api_key = api_key
        self.credentials = credentials
        self.zone = zone
        self.workers = workers
        self.retry_window = retry_window
        self.cwd = cwd
        self.http = user_client(ca, timeout=timeout, pool=max(32, workers * 4))
        self._leader: str | None = None
        self._rr = random.randrange(len(self.endpoints))
        self._login_lock = threading.Lock()
        self.user: str | None = None
        self.stats = {"retries": 0, "failovers": 0, "replica_fallbacks": 0}

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ rutas
    def abspath(self, path: str) -> str:
        return normpath(path, self.cwd)

    # --------------------------------------------------------- transporte
    def _headers(self, rid: str | None) -> dict[str, str]:
        h: dict[str, str] = {}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        elif self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if rid:
            h["X-Request-Id"] = rid
        return h

    def _pick(self, stale: bool) -> str:
        if stale:
            return random.choice(self.endpoints)
        return self._leader or self.endpoints[self._rr % len(self.endpoints)]

    def _call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        mutating: bool = False,
        stale: bool = False,
        auth: bool = True,
    ) -> Any:
        """Llamada al plano de control con reintentos y seguimiento del líder."""
        rid = new_id() if mutating else None
        if stale:
            params = {**(params or {}), "stale": "1"}
        deadline = time.monotonic() + self.retry_window
        attempt, last = 0, None
        relogged = False
        while True:
            base = self._pick(stale)
            try:
                resp = self.http.request(method, base + path, json=json, params=params, headers=self._headers(rid) if auth else {})
            except _TRANSPORT_ERRORS as e:
                last = e
                self._leader = None
                self._rr += 1
                self.stats["failovers"] += 1
            else:
                if resp.status_code < 400:
                    if base != self._leader and not stale:
                        self._leader = base
                    return resp.json()
                try:
                    err = E.from_dict(resp.json()["error"])
                except Exception:
                    raise E.Unavailable(f"HTTP {resp.status_code}: {resp.text[:160]}") from None
                if isinstance(err, E.NotLeader):
                    self._leader = err.leader
                    if not err.leader:
                        self._rr += 1
                    last = err
                elif isinstance(err, E.Unavailable):
                    last = err
                elif stale:
                    # Una lectura relajada pudo ver estado atrasado: se reconfirma con el líder.
                    return self._call(method, path, json=json, params={k: v for k, v in (params or {}).items() if k != "stale"}, mutating=mutating, stale=False, auth=auth)
                elif isinstance(err, E.Unauthenticated) and auth and self.credentials and not relogged and not self.api_key:
                    relogged = True
                    self.login(*self.credentials)  # sesión expirada: reautenticar una vez
                    continue
                else:
                    raise err
            if time.monotonic() > deadline:
                raise E.Unavailable(f"el cluster no respondió dentro de {self.retry_window:.0f}s: {last}")
            attempt += 1
            self.stats["retries"] += 1
            time.sleep(min(0.05 * 2**attempt, 0.8))

    # ------------------------------------------------------- autenticación
    def login(self, username: str, password: str, otp: str | None = None) -> dict:
        with self._login_lock:
            res = self._call("POST", "/v1/auth/login", json={"username": username, "password": password, "otp": otp}, auth=False)
            self.token, self.user = res["token"], res["user"]
            return res

    def whoami(self) -> dict:
        return self._call("GET", "/v1/whoami")

    def change_password(self, old: str, new: str) -> dict:
        return self._call("POST", "/v1/auth/password", json={"old": old, "new": new}, mutating=True)

    def enable_2fa(self, password: str) -> dict:
        return self._call("POST", "/v1/auth/totp/enable", json={"password": password})

    def disable_2fa(self, password: str, otp: str) -> dict:
        return self._call("POST", "/v1/auth/totp/disable", json={"password": password, "otp": otp})

    def create_api_key(self, label: str = "") -> dict:
        return self._call("POST", "/v1/auth/apikeys", json={"label": label})

    def list_api_keys(self) -> list[dict]:
        return self._call("GET", "/v1/auth/apikeys")

    def revoke_api_key(self, key_id: str) -> dict:
        return self._call("DELETE", f"/v1/auth/apikeys/{key_id}")

    # --------------------------------------------- RF1: gestión del sistema de archivos
    def stat(self, path: str, *, stale: bool = False) -> dict:
        return self._call("GET", "/v1/fs/stat", params={"path": self.abspath(path)}, stale=stale)

    def ls(self, path: str = ".", *, stale: bool = False) -> list[dict]:
        return self._call("GET", "/v1/fs/ls", params={"path": self.abspath(path)}, stale=stale)

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except E.NotFound:
            return False

    def isdir(self, path: str) -> bool:
        try:
            return self.stat(path)["type"] == "dir"
        except E.NotFound:
            return False

    def mkdir(self, path: str, *, parents: bool = False, mode: int = 0o755) -> dict:
        return self._call("POST", "/v1/fs/mkdir", json={"path": self.abspath(path), "parents": parents, "mode": mode}, mutating=True)

    def rmdir(self, path: str) -> dict:
        return self._call("POST", "/v1/fs/rmdir", json={"path": self.abspath(path)}, mutating=True)

    def rm(self, path: str, *, recursive: bool = False, lock_id: str | None = None) -> dict:
        return self._call("POST", "/v1/fs/rm", json={"path": self.abspath(path), "recursive": recursive, "lock_id": lock_id}, mutating=True)

    def mv(self, src: str, dst: str) -> dict:
        return self._call("POST", "/v1/fs/mv", json={"src": self.abspath(src), "dst": self.abspath(dst)}, mutating=True)

    def chmod(self, path: str, mode: int) -> dict:
        return self._call("POST", "/v1/fs/chmod", json={"path": self.abspath(path), "mode": mode}, mutating=True)

    def chown(self, path: str, owner: str | None = None, group: str | None = None) -> dict:
        return self._call("POST", "/v1/fs/chown", json={"path": self.abspath(path), "owner": owner, "group": group}, mutating=True)

    def setacl(self, path: str, entries: list[dict]) -> dict:
        return self._call("POST", "/v1/fs/setacl", json={"path": self.abspath(path), "entries": entries}, mutating=True)

    def getacl(self, path: str) -> list[dict]:
        return self.stat(path)["acl"]

    def setrepl(self, path: str, repl: int) -> dict:
        return self._call("POST", "/v1/fs/setrepl", json={"path": self.abspath(path), "repl": repl}, mutating=True)

    def df(self) -> dict:
        return self._call("GET", "/v1/fs/df")

    def walk(self, path: str = "/"):
        """Recorrido estilo ``os.walk``: (dir, [subdirs], [archivos])."""
        entries = self.ls(path)
        base = self.abspath(path)
        dirs = [e["name"] for e in entries if e["type"] == "dir"]
        files = [e["name"] for e in entries if e["type"] == "file"]
        yield base, dirs, files
        for d in dirs:
            yield from self.walk(posixpath.join(base, d))

    # ------------------------------------------------------ cluster/administración
    def _leader_or_local(self, path: str):
        """Consulta al líder; si no hay líder (elección/caída) cae a una lectura relajada para poder diagnosticar."""
        saved, self.retry_window = self.retry_window, min(self.retry_window, 4.0)
        try:
            return self._call("GET", path)
        except E.Unavailable:
            return self._call("GET", path, stale=True)
        finally:
            self.retry_window = saved

    def cluster(self) -> dict:
        return self._leader_or_local("/v1/cluster")

    def datanodes(self) -> list[dict]:
        return self._leader_or_local("/v1/admin/datanodes")

    def fsck(self) -> dict:
        return self._call("GET", "/v1/admin/fsck")

    def scrub(self, dn: str) -> dict:
        """Verificación inmediata de integridad de los bloques de un DataNode (admin)."""
        return self._call("POST", f"/v1/admin/datanodes/{dn}/scrub")

    def users(self) -> list[dict]:
        return self._call("GET", "/v1/admin/users")

    def useradd(self, name: str, password: str, *, groups: list[str] | None = None, admin: bool = False) -> dict:
        return self._call("POST", "/v1/admin/users", json={"name": name, "password": password, "groups": groups or [], "admin": admin}, mutating=True)

    def usermod(self, name: str, *, password: str | None = None, groups: list[str] | None = None, enabled: bool | None = None) -> dict:
        body = {k: v for k, v in {"password": password, "groups": groups, "enabled": enabled}.items() if v is not None}
        return self._call("PATCH", f"/v1/admin/users/{name}", json=body, mutating=True)

    def userdel(self, name: str) -> dict:
        return self._call("DELETE", f"/v1/admin/users/{name}", mutating=True)

    def set_datanode_state(self, dn: str, state: str) -> dict:
        return self._call("POST", f"/v1/admin/datanodes/{dn}/state", json={"state": state}, mutating=True)

    # ------------------------------------------------------------------ locks
    def lock(self, path: str, mode: str = "exclusive", ttl: float = 30.0, *, wait: float = 0.0) -> Lock:
        """Adquiere un lock con lease (renovación automática).  ``wait`` > 0 reintenta si está ocupado."""
        deadline = time.monotonic() + wait
        lock_id = new_id()
        while True:
            try:
                r = self._call("POST", "/v1/locks/acquire", json={"path": self.abspath(path), "mode": mode, "ttl": ttl, "lock_id": lock_id})
                return Lock(self, lock_id, r["fence"], ttl, self.abspath(path), mode)
            except E.Locked:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    # ---------------------------------------------------- RF2: transferencia de archivos
    def put(
        self,
        local: str,
        remote: str,
        *,
        repl: int | None = None,
        block_size: int | None = None,
        overwrite: bool = True,
        if_version: int | None = None,
        lock_id: str | None = None,
        progress: Progress | None = None,
        mode_bits: int = 0o644,
    ) -> dict:
        """Sube un archivo local particionándolo en bloques que viajan en paralelo."""
        size = os.stat(local).st_size
        fd = os.open(local, os.O_RDONLY)
        try:
            return self._upload(
                lambda off, n: os.pread(fd, n, off), size, self.abspath(remote), repl=repl, block_size=block_size,
                overwrite=overwrite, if_version=if_version, lock_id=lock_id, progress=progress, mode_bits=mode_bits,
            )  # fmt: skip
        finally:
            os.close(fd)

    def send(self, data: bytes, remote: str, **kw) -> dict:
        """Sube un ``bytes`` en memoria."""
        mv = memoryview(data)
        return self._upload(lambda off, n: bytes(mv[off : off + n]), len(data), self.abspath(remote), **kw)

    def get(self, remote: str, local: str, *, progress: Progress | None = None) -> dict:
        """Descarga en paralelo, verificando el SHA-256 de cada bloque."""
        remote = self.abspath(remote)
        meta = self._locations_all(remote)
        size = meta["size"]
        tmp = f"{local}.dfsha-part"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.ftruncate(fd, size)
            done = [0]
            lock = threading.Lock()

            def one(i: int, off: int) -> None:
                data = self._fetch_block(remote, meta["blocks"][i], i)
                os.pwrite(fd, data, off)
                if progress:
                    with lock:
                        done[0] += len(data)
                        progress(done[0], size)

            offs, o = [], 0
            for b in meta["blocks"]:
                offs.append(o)
                o += b["len"]
            with ThreadPoolExecutor(self.workers) as ex:
                for f in as_completed([ex.submit(one, i, off) for i, off in enumerate(offs)]):
                    f.result()
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp, local)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        return {"path": remote, "size": size, "version": meta["version"], "blocks": len(meta["blocks"])}

    def receive(self, remote: str) -> bytes:
        """Descarga un archivo completo a memoria."""
        remote = self.abspath(remote)
        meta = self._locations_all(remote)
        out = bytearray(meta["size"])
        offs, o = [], 0
        for b in meta["blocks"]:
            offs.append(o)
            o += b["len"]

        def one(i: int) -> None:
            data = self._fetch_block(remote, meta["blocks"][i], i)
            out[offs[i] : offs[i] + len(data)] = data

        with ThreadPoolExecutor(self.workers) as ex:
            for f in as_completed([ex.submit(one, i) for i in range(len(offs))]):
                f.result()
        return bytes(out)

    # ----------------------------------------------------------- lectura de bloques
    def _locations_all(self, remote: str) -> dict:
        for _ in range(3):
            first = self._call("GET", "/v1/files/locations", params={"path": remote, "start": 0, "limit": 256, "zone": self.zone})
            blocks = list(first["blocks"])
            while len(blocks) < first["nblocks"]:
                page = self._call("GET", "/v1/files/locations", params={"path": remote, "start": len(blocks), "limit": 256, "zone": self.zone})
                if page["version"] != first["version"]:
                    break  # el archivo cambió a mitad de la paginación: reiniciar
                blocks.extend(page["blocks"])
            else:
                first["blocks"] = blocks
                return first
        raise E.StaleHandle(f"{remote} cambió repetidamente durante la lectura")

    def _refresh_block(self, remote: str, idx: int) -> dict:
        return self._call("GET", "/v1/files/locations", params={"path": remote, "start": idx, "limit": 1, "zone": self.zone})["blocks"][0]

    def _fetch_block(self, remote: str, blk: dict, idx: int, *, offset: int = 0, length: int | None = None) -> bytes:
        """Obtiene un bloque probando réplicas; refresca ubicaciones/tokens si hace falta."""
        last: Exception | None = None
        for round_ in range(3):
            for loc in blk["locs"]:
                try:
                    params = {"offset": offset, "length": length} if (offset or length is not None) else None
                    r = self.http.get(f"{loc['url']}/v1/blocks/{blk['block_id']}", params=params, headers={"Authorization": f"Bearer {blk['token']}"})
                except _TRANSPORT_ERRORS as e:
                    last = e
                    self.stats["replica_fallbacks"] += 1
                    continue
                if r.status_code == 200:
                    data = r.content
                    if offset or length is not None:
                        return data
                    if len(data) == blk["len"] and hashlib.sha256(data).hexdigest() == blk["sha"]:
                        return data
                    last = E.CorruptBlock(f"SHA-256 de {blk['block_id'][:8]} no coincide en {loc['id']}")
                    self.stats["replica_fallbacks"] += 1
                    continue
                try:
                    last = E.from_dict(r.json()["error"])
                except Exception:
                    last = E.Unavailable(f"HTTP {r.status_code}")
                self.stats["replica_fallbacks"] += 1
                if isinstance(last, E.Unauthenticated):
                    break  # token vencido: refrescar
            if round_ < 2:
                time.sleep(0.2 * (round_ + 1))
                blk = self._refresh_block(remote, idx)
        raise E.Unavailable(f"bloque {blk['block_id'][:8]} ({remote}#{idx}) inaccesible en todas sus réplicas: {last}")

    # --------------------------------------------------------- escritura de bloques
    def _begin(self, remote: str, mode: str, **kw) -> dict:
        body = {"path": remote, "mode": mode, "overwrite": kw.get("overwrite", True), "repl": kw.get("repl"),
                "block_size": kw.get("block_size"), "lock_id": kw.get("lock_id"), "mode_bits": kw.get("mode_bits", 0o644)}  # fmt: skip
        return self._call("POST", "/v1/files/begin", json=body, mutating=True)

    def _alloc(self, sid: str, count: int) -> list[dict]:
        out: list[dict] = []
        while len(out) < count:
            n = min(256, count - len(out))
            out.extend(self._call("POST", f"/v1/files/{sid}/blocks", json={"count": n, "zone": self.zone, "exclude": []})["blocks"])
        return out

    def _upload_block(self, sid: str, alloc: dict, data: bytes, min_repl: int) -> dict:
        bid, pipeline, token = alloc["block_id"], alloc["pipeline"], alloc["token"]
        sha = hashlib.sha256(data).hexdigest()
        failed: list[str] = []
        last: Exception | None = None
        for _attempt in range(5):
            acks: list[str] | None = None
            for pos, loc in enumerate(pipeline):
                try:
                    r = self.http.put(
                        f"{loc['url']}/v1/blocks/{bid}", content=data,
                        headers={"Authorization": f"Bearer {token}", "X-Block-Sha256": sha, "X-Pipe-Pos": str(pos)},
                    )  # fmt: skip
                except _TRANSPORT_ERRORS as e:
                    last = e
                    failed.append(loc["id"])
                    self.stats["replica_fallbacks"] += 1
                    continue
                if r.status_code == 200:
                    acks = r.json()["acks"]
                    break
                try:
                    last = E.from_dict(r.json()["error"])
                except Exception:
                    last = E.Unavailable(f"HTTP {r.status_code}")
                failed.append(loc["id"])
            if acks is not None and len(acks) >= min_repl:
                return {"id": bid, "len": len(data), "sha": sha, "reps": acks}
            if acks:
                failed += [lc["id"] for lc in pipeline if lc["id"] not in acks]
            self.stats["retries"] += 1
            new = self._call(
                "POST", f"/v1/files/{sid}/realloc",
                json={"block_id": bid, "zone": self.zone, "exclude": sorted(set(failed))},
            )  # fmt: skip
            pipeline, token = new["pipeline"], new["token"]
        raise E.Unavailable(f"no se pudo escribir el bloque {bid[:8]} con quórum: {last}")

    def _keepalive(self, sid: str, ttl: float) -> Callable[[], None]:
        stop = threading.Event()

        def run() -> None:
            while not stop.wait(max(5.0, ttl / 3)):
                try:
                    self._call("POST", f"/v1/files/{sid}/renew")
                except E.DfshaError:
                    return

        t = threading.Thread(target=run, daemon=True, name="dfsha-keepalive")
        t.start()
        return stop.set

    def _upload(
        self,
        read_at: Callable[[int, int], bytes],
        size: int,
        remote: str,
        *,
        repl: int | None = None,
        block_size: int | None = None,
        overwrite: bool = True,
        if_version: int | None = None,
        lock_id: str | None = None,
        progress: Progress | None = None,
        mode_bits: int = 0o644,
    ) -> dict:
        sess = self._begin(remote, "replace", repl=repl, block_size=block_size, overwrite=overwrite, lock_id=lock_id, mode_bits=mode_bits)
        sid, bs, min_repl = sess["sid"], sess["block_size"], sess["min_repl"]
        stop_keepalive = self._keepalive(sid, sess.get("session_ttl", 900))
        try:
            nblocks = (size + bs - 1) // bs
            allocs = self._alloc(sid, nblocks) if nblocks else []
            entries: list[dict | None] = [None] * nblocks
            done, lock = [0], threading.Lock()

            def one(i: int) -> None:
                chunk = read_at(i * bs, min(bs, size - i * bs))
                entries[i] = self._upload_block(sid, allocs[i], chunk, min_repl)
                if progress:
                    with lock:
                        done[0] += len(chunk)
                        progress(done[0], size)

            with ThreadPoolExecutor(self.workers) as ex:
                for f in as_completed([ex.submit(one, i) for i in range(nblocks)]):
                    f.result()
            res = self._call(
                "POST", f"/v1/files/{sid}/commit",
                json={"layout": entries, "size": size, "if_version": if_version, "lock_id": lock_id}, mutating=True,
            )  # fmt: skip
            return {**res, "blocks": nblocks}
        except BaseException:
            try:
                self._call("POST", f"/v1/files/{sid}/abort")
            except Exception:
                pass
            raise
        finally:
            stop_keepalive()

    # ---------------------------------------------------------------- RF3: acceso
    def open(self, path: str, mode: str = "rb", *, lock: str | None = None, **kw):
        """Abre un archivo remoto con semántica de archivo local.

        ``rb`` lectura aleatoria · ``wb`` escritura secuencial (reemplazo atómico) ·
        ``ab`` anexar · ``r+b`` lectura/escritura aleatoria (copy-on-write por bloque).
        Con ``lock='exclusive'|'shared'`` el archivo se bloquea mientras esté abierto.
        Modo texto con ``r``, ``w``, ``a``.  Cierra con ``close()``/``with``.
        """
        import io

        from .fileobj import DfshaAppender, DfshaReader, DfshaUpdater, DfshaWriter

        text = "b" not in mode
        m = mode.replace("b", "").replace("t", "")
        full = self.abspath(path)
        lk = self.lock(full, lock) if lock else None
        try:
            if m == "r":
                raw = DfshaReader(self, full, **kw)
                f = io.BufferedReader(raw, buffer_size=1 << 20)
            elif m == "w":
                raw = DfshaWriter(self, full, lock=lk, **kw)
                f = _SafeBufferedWriter(raw, buffer_size=1 << 20)
            elif m == "a":
                raw = DfshaAppender(self, full, lock=lk, **kw)
                f = _SafeBufferedWriter(raw, buffer_size=1 << 20)
            elif m == "r+":
                raw = DfshaUpdater(self, full, lock=lk, **kw)
                f = raw
            else:
                raise E.InvalidArgument(f"modo no soportado: {mode!r}")
        except BaseException:
            if lk:
                lk.release()
            raise
        if lk and m == "r":
            raw._lock = lk  # se libera al cerrar el lector
        return _SafeText(f, encoding="utf-8") if text else f


def _raw_of(f):
    b = f.buffer if hasattr(f, "buffer") else f
    return b.raw if hasattr(b, "raw") else b


class _SafeBufferedWriter(__import__("io").BufferedWriter):
    """Si el bloque ``with`` termina con excepción, la escritura se ABORTA (no se confirma nada)."""

    def __exit__(self, t, e, tb):
        if t is not None:
            self.raw.abort()
        return super().__exit__(t, e, tb)


class _SafeText(__import__("io").TextIOWrapper):
    def __exit__(self, t, e, tb):
        if t is not None and hasattr(_raw_of(self), "abort"):
            _raw_of(self).abort()
        return super().__exit__(t, e, tb)


class Lock:
    """Lock con lease renovado automáticamente; usable como context manager."""

    def __init__(self, client: Client, lock_id: str, fence: int, ttl: float, path: str, mode: str) -> None:
        self.client, self.lock_id, self.fence, self.ttl, self.path, self.mode = client, lock_id, fence, ttl, path, mode
        self._stop = threading.Event()
        self.lost = False
        self._t = threading.Thread(target=self._renew, daemon=True, name="dfsha-lock-renew")
        self._t.start()

    def _renew(self) -> None:
        while not self._stop.wait(self.ttl / 3):
            try:
                self.client._call("POST", "/v1/locks/renew", json={"lock_id": self.lock_id, "ttl": self.ttl})
            except E.NotFound:
                self.lost = True  # el lease venció (p. ej. partición prolongada)
                return
            except E.DfshaError:
                continue

    def release(self) -> None:
        self._stop.set()
        try:
            self.client._call("POST", "/v1/locks/release", json={"lock_id": self.lock_id, "ttl": self.ttl})
        except E.DfshaError:
            pass

    def __enter__(self) -> Lock:
        return self

    def __exit__(self, *exc) -> None:
        self.release()
