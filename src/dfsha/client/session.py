"""Sesión persistente del CLI (``~/.dfsha/session.json``, permisos 0600) y construcción del cliente."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..common.errors import Unauthenticated
from ..common.util import atomic_write
from .sdk import Client


def home() -> Path:
    return Path(os.environ.get("DFSHA_HOME") or Path.home() / ".dfsha")


@dataclass
class Session:
    endpoints: list[str] = field(default_factory=list)
    ca: str = ""
    token: str = ""
    expires_at: float = 0.0
    user: str = ""
    cwd: str = "/"

    @classmethod
    def load(cls) -> Session:
        p = home() / "session.json"
        if p.exists():
            try:
                return cls(**json.loads(p.read_text()))
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        atomic_write(home() / "session.json", json.dumps(asdict(self), indent=1).encode(), mode=0o600, fsync=False)

    def valid(self) -> bool:
        return bool(self.token) and self.expires_at > time.time() + 5


def make_client(
    session: Session,
    *,
    endpoints: str | None = None,
    ca: str | None = None,
    workers: int | None = None,
    zone: str | None = None,
    need_auth: bool = True,
) -> Client:
    eps = [e for e in (endpoints or os.environ.get("DFSHA_ENDPOINTS", "")).split(",") if e] or session.endpoints
    ca_path = ca or os.environ.get("DFSHA_CA") or session.ca or str(home() / "ca.pem")
    if not eps:
        raise Unauthenticated("no hay endpoints; use 'dfsha login --endpoints https://host:8443,...' o DFSHA_ENDPOINTS")
    if not Path(ca_path).exists():
        raise Unauthenticated(f"no encuentro la CA ({ca_path}); use --ca o DFSHA_CA")
    api_key = os.environ.get("DFSHA_API_KEY")
    user, pw = os.environ.get("DFSHA_USER"), os.environ.get("DFSHA_PASSWORD")
    c = Client(
        eps, ca_path, api_key=api_key, zone=zone or os.environ.get("DFSHA_ZONE"),
        workers=workers or int(os.environ.get("DFSHA_WORKERS", "8")), cwd=session.cwd or "/",
        credentials=(user, pw) if user and pw else None,
    )  # fmt: skip
    if session.valid():
        c.token, c.user = session.token, session.user
    if need_auth and not api_key and not c.token:
        if user and pw:
            c.login(user, pw, os.environ.get("DFSHA_OTP"))
        else:
            raise Unauthenticated("no hay sesión; ejecute 'dfsha login'")
    return c
