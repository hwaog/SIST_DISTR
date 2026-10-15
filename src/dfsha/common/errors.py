"""Errores de dominio de DFSha.

Cada error lleva un ``code`` estable (estilo errno) que viaja por la red en el
cuerpo JSON ``{"error": {"code": ..., "message": ...}}`` y se mapea a un status
HTTP.  El cliente reconstruye la misma excepción, de modo que la capa de
transporte es transparente para el código de aplicación.
"""

from __future__ import annotations

from typing import Any


class DfshaError(Exception):
    """Error base.  ``code`` es estable y forma parte del protocolo."""

    code = "EIO"
    http_status = 500
    label = ""  # prefijo legible: NotFound("/x") -> "no existe: /x"

    def __init__(self, message: str = "", **extra: Any) -> None:
        msg = f"{self.label}: {message}" if self.label and message else (message or self.label or self.code)
        super().__init__(msg)
        self.message = msg
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.extra:
            d["extra"] = self.extra
        return d


def _mk(name: str, code: str, status: int, doc: str, label: str = "") -> type[DfshaError]:
    return type(name, (DfshaError,), {"code": code, "http_status": status, "__doc__": doc, "label": label})


NotFound = _mk("NotFound", "ENOENT", 404, "El archivo o directorio no existe.", "no existe")
Exists = _mk("Exists", "EEXIST", 409, "Ya existe.", "ya existe")
NotADirectory = _mk("NotADirectory", "ENOTDIR", 400, "Un componente del path no es directorio.", "no es un directorio")
IsADirectory = _mk("IsADirectory", "EISDIR", 400, "La operación no aplica a directorios.", "es un directorio")
NotEmpty = _mk("NotEmpty", "ENOTEMPTY", 409, "El directorio no está vacío.", "directorio no vacío")
PermissionDenied = _mk("PermissionDenied", "EACCES", 403, "Permiso denegado.")
InvalidArgument = _mk("InvalidArgument", "EINVAL", 400, "Argumento inválido.")
Locked = _mk("Locked", "ELOCKED", 423, "El recurso está bloqueado por otro cliente.")
Conflict = _mk("Conflict", "ECONFLICT", 412, "La versión del archivo cambió (concurrencia optimista).")
Unauthenticated = _mk("Unauthenticated", "EAUTH", 401, "Credenciales ausentes o inválidas.")
TooManyAttempts = _mk("TooManyAttempts", "ETHROTTLED", 429, "Demasiados intentos; espere.")
NoSpace = _mk("NoSpace", "ENOSPC", 507, "No hay DataNodes suficientes/espacio.")
Unavailable = _mk("Unavailable", "EUNAVAIL", 503, "Servicio temporalmente no disponible.")
CorruptBlock = _mk("CorruptBlock", "ECORRUPT", 422, "El bloque falló la verificación de integridad.")
BlockNotFound = _mk("BlockNotFound", "EBLKNOENT", 404, "El DataNode no tiene ese bloque.")
StaleHandle = _mk("StaleHandle", "ESTALE", 410, "El archivo cambió o fue borrado mientras estaba abierto.")


class NotLeader(DfshaError):
    """Esta réplica no es líder Raft; ``leader`` indica a quién dirigirse."""

    code = "ENOTLEADER"
    http_status = 503

    def __init__(self, leader: str | None = None, message: str = "", leader_internal: str | None = None) -> None:
        super().__init__(message or "este ControlNode no es el líder", leader=leader, leader_internal=leader_internal)
        self.leader = leader
        self.leader_internal = leader_internal


_REGISTRY: dict[str, type[DfshaError]] = {
    cls.code: cls
    for cls in list(globals().values())
    if isinstance(cls, type) and issubclass(cls, DfshaError)
}


def from_dict(d: dict[str, Any]) -> DfshaError:
    """Reconstruye una excepción a partir de su forma de red."""
    cls = _REGISTRY.get(d.get("code", ""), DfshaError)
    extra = d.get("extra") or {}
    if cls is NotLeader:
        return NotLeader(extra.get("leader"), d.get("message", ""), extra.get("leader_internal"))
    e = cls("", **extra)
    e.message = d.get("message", "") or e.message  # el mensaje ya viene con su prefijo
    e.args = (e.message,)
    return e
