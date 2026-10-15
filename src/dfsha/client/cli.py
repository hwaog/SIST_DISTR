"""CLI ``dfsha``: abstracción de sistema de archivos jerárquico tipo Linux sobre DFSha."""

from __future__ import annotations

import getpass
import hashlib
import os
import posixpath
import stat as pystat
import sys
import time
from dataclasses import dataclass, field

import click
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from ..common import errors as E
from ..common.util import human, parse_size
from ..pki import client_context
from .sdk import Client
from .session import Session, home, make_client

out = Console()
err = Console(stderr=True)


@dataclass
class App:
    endpoints: str | None = None
    ca: str | None = None
    workers: int | None = None
    zone: str | None = None
    session: Session = field(default_factory=Session.load)
    _client: Client | None = None

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = make_client(self.session, endpoints=self.endpoints, ca=self.ca, workers=self.workers, zone=self.zone)
        return self._client

    def anon_client(self) -> Client:
        return make_client(self.session, endpoints=self.endpoints, ca=self.ca, workers=self.workers, need_auth=False)

    def persist(self) -> None:
        c = self._client
        if c is not None:
            if c.token:
                self.session.token = c.token
                self.session.user = c.user or self.session.user
            self.session.endpoints = c.endpoints
            self.session.ca = c.ca
            self.session.cwd = c.cwd
        self.session.save()


class Group(click.Group):
    """Traduce errores de dominio a mensajes legibles + código de salida."""

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except E.DfshaError as e:
            err.print(f"[red]dfsha:[/red] {e.message} [dim]({e.code})[/dim]")
            ctx.exit(1)


def mode_str(typ: str, mode: int, acl: bool = False) -> str:
    s = "d" if typ == "dir" else "-"
    for shift in (6, 3, 0):
        b = (mode >> shift) & 7
        s += ("r" if b & 4 else "-") + ("w" if b & 2 else "-") + ("x" if b & 1 else "-")
    return s + ("+" if acl else "")


def fmt_time(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "-"


def parse_mode(s: str) -> int:
    try:
        return int(s, 8)
    except ValueError:
        raise click.BadParameter("use modo octal, p. ej. 750") from None


def progress_bar(label: str):
    p = Progress(TextColumn("[bold]{task.description}"), BarColumn(), DownloadColumn(), TransferSpeedColumn(), TimeRemainingColumn(), console=out, transient=True)
    return p


@click.group(cls=Group, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--endpoints", envvar="DFSHA_ENDPOINTS", help="URLs públicas de ControlNodes (separadas por coma).")
@click.option("--ca", envvar="DFSHA_CA", help="Certificado de la CA del cluster.")
@click.option("--workers", type=int, help="Transferencias paralelas de bloques (por defecto 8).")
@click.option("--zone", help="Zona del cliente (localidad de lectura/escritura).")
@click.pass_context
def cli(ctx: click.Context, endpoints, ca, workers, zone) -> None:
    """DFSha — sistema de archivos distribuido: alta disponibilidad, cifrado y replicación por bloques."""
    ctx.obj = ctx.obj or App(endpoints=endpoints, ca=ca, workers=workers, zone=zone)
    ctx.call_on_close(ctx.obj.persist)


# ------------------------------------------------------------------ sesión
@cli.command()
@click.option("--user", "-u")
@click.option("--password", "-p", help="(evite pasarla por argumento; se pedirá de forma interactiva)")
@click.option("--otp", help="Código 2FA (TOTP)")
@click.pass_obj
def login(app: App, user, password, otp):
    """Inicia sesión (usuario/clave [+ 2FA])."""
    c = app.anon_client()
    user = user or click.prompt("Usuario")
    password = password or getpass.getpass("Clave: ")
    try:
        res = c.login(user, password, otp)
    except E.Unauthenticated as e:
        if e.extra.get("otp_required") and not otp:
            res = c.login(user, password, click.prompt("Código 2FA"))
        else:
            raise
    app._client = c
    app.session.expires_at = res["expires_at"]
    app.session.cwd = f"/home/{user}"
    c.cwd = app.session.cwd
    out.print(f"[green]Sesión iniciada[/green] como [bold]{user}[/bold] (grupos: {', '.join(res['groups'])}); expira {fmt_time(res['expires_at'])}")


@cli.command()
@click.pass_obj
def logout(app: App):
    """Cierra la sesión local."""
    app.session.token, app.session.expires_at = "", 0.0
    app._client = None
    out.print("Sesión cerrada.")


@cli.command()
@click.pass_obj
def whoami(app: App):
    """Usuario autenticado."""
    w = app.client.whoami()
    out.print(f"{w['name']}  grupos={','.join(w['groups'])}  admin={w['admin']}  2fa={w['totp']}")


# --------------------------------------------------------------- RF1: FS
@cli.command()
@click.pass_obj
def pwd(app: App):
    """Directorio de trabajo remoto."""
    out.print(app.client.cwd)


@cli.command()
@click.argument("path", default="~")
@click.pass_obj
def cd(app: App, path):
    """Cambia el directorio de trabajo remoto."""
    c = app.client
    if path == "~":
        path = f"/home/{c.user or app.session.user}"
    target = c.abspath(path)
    if not c.isdir(target):
        raise E.NotADirectory(target)
    c.cwd = target


@cli.command(name="ls")
@click.argument("paths", nargs=-1)
@click.option("-l", "long", is_flag=True, help="Formato largo.")
@click.option("--stale", is_flag=True, help="Lectura relajada (cualquier réplica de metadatos, menor latencia).")
@click.pass_obj
def ls_cmd(app: App, paths, long, stale):
    """Lista directorios."""
    c = app.client
    for p in paths or (".",):
        entries = c.ls(p, stale=stale)
        if len(paths) > 1:
            out.print(f"[bold]{p}:[/bold]")
        if long:
            t = Table(show_header=False, box=None, pad_edge=False)
            for e in entries:
                name = f"[bold blue]{e['name']}/[/bold blue]" if e["type"] == "dir" else e["name"]
                t.add_row(mode_str(e["type"], e["mode"], bool(e["acl"])), e["owner"], e["group"], human(e["size"]) if e["type"] == "file" else "-", fmt_time(e["mtime"]), name, f"v{e['version']} r={e['repl']}" if e["type"] == "file" else "")
            out.print(t)
        else:
            out.print("  ".join(f"[bold blue]{e['name']}/[/bold blue]" if e["type"] == "dir" else e["name"] for e in entries))


@cli.command()
@click.argument("path")
@click.pass_obj
def stat(app: App, path):
    """Metadatos de un archivo o directorio."""
    s = app.client.stat(path)
    out.print_json(data=s)


@cli.command()
@click.argument("path", default=".")
@click.option("-L", "depth", type=int, default=3)
@click.pass_obj
def tree(app: App, path, depth):
    """Árbol de directorios."""
    c = app.client

    def rec(p: str, prefix: str, d: int) -> None:
        entries = c.ls(p)
        for i, e in enumerate(entries):
            last = i == len(entries) - 1
            out.print(f"{prefix}{'└── ' if last else '├── '}{e['name']}{'/' if e['type'] == 'dir' else ''}" + (f"  [dim]{human(e['size'])}[/dim]" if e["type"] == "file" else ""))
            if e["type"] == "dir" and d > 1:
                rec(posixpath.join(c.abspath(p), e["name"]), prefix + ("    " if last else "│   "), d - 1)

    out.print(c.abspath(path))
    rec(path, "", depth)


@cli.command()
@click.argument("paths", nargs=-1, required=True)
@click.option("-p", "parents", is_flag=True)
@click.pass_obj
def mkdir(app: App, paths, parents):
    """Crea directorios."""
    for p in paths:
        app.client.mkdir(p, parents=parents)


@cli.command()
@click.argument("paths", nargs=-1, required=True)
@click.pass_obj
def rmdir(app: App, paths):
    """Borra directorios vacíos."""
    for p in paths:
        app.client.rmdir(p)


@cli.command()
@click.argument("paths", nargs=-1, required=True)
@click.option("-r", "recursive", is_flag=True)
@click.option("-f", "force", is_flag=True)
@click.pass_obj
def rm(app: App, paths, recursive, force):
    """Borra archivos (y directorios con -r)."""
    for p in paths:
        try:
            app.client.rm(p, recursive=recursive)
        except E.NotFound:
            if not force:
                raise


@cli.command()
@click.argument("src")
@click.argument("dst")
@click.pass_obj
def mv(app: App, src, dst):
    """Mueve o renombra (atómico)."""
    app.client.mv(src, dst)


@cli.command()
@click.argument("src")
@click.argument("dst")
@click.pass_obj
def cp(app: App, src, dst):
    """Copia un archivo remoto a otro remoto (streaming a través del cliente)."""
    c = app.client
    if c.isdir(dst):
        dst = posixpath.join(c.abspath(dst), posixpath.basename(c.abspath(src)))
    with c.open(src, "rb") as r, c.open(dst, "wb") as w:
        while chunk := r.read(4 << 20):
            w.write(chunk)


@cli.command()
@click.argument("path")
@click.option("-n", "--bytes", "nbytes", type=int, default=None, help="Sólo los primeros N bytes.")
@click.pass_obj
def cat(app: App, path, nbytes):
    """Escribe el contenido en stdout."""
    with app.client.open(path, "rb") as f:
        left = nbytes
        while True:
            chunk = f.read(min(1 << 20, left) if left is not None else 1 << 20)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            if left is not None:
                left -= len(chunk)
                if left <= 0:
                    break
    sys.stdout.buffer.flush()


@cli.command()
@click.argument("mode")
@click.argument("path")
@click.pass_obj
def chmod(app: App, mode, path):
    """chmod MODE PATH  (modo octal)."""
    app.client.chmod(path, parse_mode(mode))


@cli.command()
@click.argument("spec")
@click.argument("path")
@click.pass_obj
def chown(app: App, spec, path):
    """chown [OWNER][:GROUP] PATH."""
    owner, _, group = spec.partition(":")
    app.client.chown(path, owner or None, group or None)


@cli.command()
@click.argument("path")
@click.option("-m", "modify", multiple=True, help="Entrada ACL: u:usuario:rwx | g:grupo:r-x (repetible).")
@click.option("-b", "clear", is_flag=True, help="Elimina todas las entradas ACL.")
@click.pass_obj
def setfacl(app: App, path, modify, clear):
    """Establece la ACL de un archivo/directorio (reemplaza la existente)."""
    entries = []
    for m in modify:
        t, n, p = m.split(":")
        perm = (4 if "r" in p else 0) | (2 if "w" in p else 0) | (1 if "x" in p else 0)
        entries.append({"t": {"u": "user", "g": "group"}[t], "n": n, "p": perm})
    app.client.setacl(path, [] if clear else entries)


@cli.command()
@click.argument("path")
@click.pass_obj
def getfacl(app: App, path):
    """Muestra permisos y ACL."""
    s = app.client.stat(path)
    out.print(f"# file: {app.client.abspath(path)}\n# owner: {s['owner']}\n# group: {s['group']}")
    m = s["mode"]
    out.print(f"user::{'r' if m & 0o400 else '-'}{'w' if m & 0o200 else '-'}{'x' if m & 0o100 else '-'}")
    for a in s["acl"]:
        p = a["p"]
        out.print(f"{a['t']}:{a['n']}:{'r' if p & 4 else '-'}{'w' if p & 2 else '-'}{'x' if p & 1 else '-'}")
    out.print(f"group::{'r' if m & 0o040 else '-'}{'w' if m & 0o020 else '-'}{'x' if m & 0o010 else '-'}")
    out.print(f"other::{'r' if m & 0o004 else '-'}{'w' if m & 0o002 else '-'}{'x' if m & 0o001 else '-'}")


@cli.command()
@click.argument("path")
@click.argument("n", type=int)
@click.pass_obj
def setrepl(app: App, path, n):
    """Cambia el factor de replicación de un archivo."""
    app.client.setrepl(path, n)


# --------------------------------------------------------- RF2: transferencia
@cli.command()
@click.argument("local", type=click.Path(exists=True, dir_okay=True))
@click.argument("remote", required=False)
@click.option("--repl", type=int, help="Factor de replicación.")
@click.option("--block-size", help="Tamaño de bloque (p. ej. 8MiB).")
@click.option("--no-overwrite", is_flag=True)
@click.option("-r", "recursive", is_flag=True, help="Sube un directorio completo.")
@click.pass_obj
def put(app: App, local, remote, repl, block_size, no_overwrite, recursive):
    """Sube LOCAL a REMOTE (los bloques viajan en paralelo y se replican)."""
    c = app.client
    bs = parse_size(block_size) if block_size else None
    base = os.path.basename(os.path.normpath(local))
    dest = remote or base
    if c.isdir(dest):
        dest = posixpath.join(c.abspath(dest), base)
    if os.path.isdir(local):
        if not recursive:
            raise click.UsageError("es un directorio; use -r")
        for root, _dirs, files in os.walk(local):
            rel = os.path.relpath(root, local)
            rdir = dest if rel == "." else posixpath.join(dest, rel.replace(os.sep, "/"))
            c.mkdir(rdir, parents=True)
            for f in files:
                _put_one(c, os.path.join(root, f), posixpath.join(rdir, f), repl, bs, no_overwrite)
        return
    _put_one(c, local, dest, repl, bs, no_overwrite)


def _put_one(c: Client, local: str, remote: str, repl, bs, no_overwrite) -> None:
    size = os.path.getsize(local)
    t0 = time.perf_counter()
    with progress_bar("put") as p:
        task = p.add_task(f"↑ {os.path.basename(local)}", total=size or 1)
        r = c.put(local, remote, repl=repl, block_size=bs, overwrite=not no_overwrite, progress=lambda d, t: p.update(task, completed=d))
    dt = time.perf_counter() - t0
    out.print(f"[green]↑[/green] {local} → {r['path']}  {human(size)} en {r['blocks']} bloques, v{r['version']}  ({human(size / dt)}/s)")


@cli.command()
@click.argument("remote")
@click.argument("local", required=False)
@click.pass_obj
def get(app: App, remote, local):
    """Descarga REMOTE a LOCAL (bloques en paralelo, verificados con SHA-256)."""
    c = app.client
    local = local or posixpath.basename(c.abspath(remote))
    if os.path.isdir(local):
        local = os.path.join(local, posixpath.basename(c.abspath(remote)))
    t0 = time.perf_counter()
    with progress_bar("get") as p:
        task = p.add_task(f"↓ {posixpath.basename(remote)}", total=c.stat(remote)["size"] or 1)
        r = c.get(remote, local, progress=lambda d, t: p.update(task, completed=d))
    dt = time.perf_counter() - t0
    out.print(f"[green]↓[/green] {r['path']} → {local}  {human(r['size'])} de {r['blocks']} bloques ({human(r['size'] / dt)}/s)")


@cli.command()
@click.argument("path")
@click.pass_obj
def verify(app: App, path):
    """Relee TODOS los bloques de un archivo desde las réplicas y verifica SHA-256 extremo a extremo."""
    c = app.client
    p = c.abspath(path)
    meta = c._locations_all(p)
    h = hashlib.sha256()
    for i, b in enumerate(meta["blocks"]):
        h.update(c._fetch_block(p, b, i))
    out.print(f"[green]OK[/green] {p}: {len(meta['blocks'])} bloques íntegros, sha256={h.hexdigest()}")


@cli.command()
@click.argument("path")
@click.pass_obj
def blocks(app: App, path):
    """Mapa de bloques: tamaño, SHA-256 y en qué DataNodes (y zonas) está cada réplica."""
    c = app.client
    p = c.abspath(path)
    meta = c._locations_all(p)
    zones = {d["id"]: d["zone"] for d in c.datanodes()}
    t = Table(title=f"{p}  {human(meta['size'])}  v{meta['version']}  replicación={meta['repl']}  bloque={human(meta['block_size'])}")
    for col in ("#", "bloque", "bytes", "sha256", "réplicas (DataNode@zona)"):
        t.add_column(col)
    for i, b in enumerate(meta["blocks"]):
        t.add_row(str(i), b["block_id"][:10], str(b["len"]), b["sha"][:12], "  ".join(f"{loc['id']}@{zones.get(loc['id'], '?')}" for loc in b["locs"]))
    out.print(t)


# ------------------------------------------------------------- RF3: locks
@cli.command()
@click.argument("path")
@click.option("--shared", is_flag=True)
@click.option("--ttl", type=float, default=30.0)
@click.option("--hold", type=float, default=0.0, help="Mantener el lock N segundos (demostración).")
@click.pass_obj
def lock(app: App, path, shared, ttl, hold):
    """Adquiere un lock (con lease y fencing token); lo mantiene --hold segundos."""
    lk = app.client.lock(path, "shared" if shared else "exclusive", ttl)
    out.print(f"lock {lk.mode} sobre {lk.path}: id={lk.lock_id[:8]} fencing token={lk.fence}")
    try:
        if hold:
            time.sleep(hold)
    finally:
        lk.release()
        out.print("lock liberado")


# ---------------------------------------------------------- cluster / admin
@cli.command()
@click.pass_obj
def df(app: App):
    """Uso del sistema de archivos."""
    d = app.client.df()
    out.print(f"archivos={d['files']} directorios={d['dirs']} bytes={human(d['bytes'])} bloques={d['blocks']} usuarios={d['users']} locks={d['locks']}")


@cli.command()
@click.pass_obj
def fsck(app: App):
    """Salud de los bloques (sub/sobre-replicados, perdidos)."""
    r = app.client.fsck()
    ok = r["under_replicated"] == r["lost"] == 0
    out.print(f"[{'green' if ok else 'yellow'}]bloques={r['blocks']} sanos={r['healthy']} sub-replicados={r['under_replicated']} sobre-replicados={r['over_replicated']} perdidos={r['lost']}[/]")


@cli.command()
@click.pass_obj
def nodes(app: App):
    """Estado del cluster: ControlNodes (Raft) y DataNodes."""
    st = app.client.cluster()
    import httpx

    t = Table(title="ControlNodes (Raft)")
    for col in ("id", "rol", "término", "commit", "líder"):
        t.add_column(col)
    for cn in st["controlnodes"]:
        try:
            info = httpx.get(f"{cn['url']}/v1/node", verify=client_context(app.client.ca), timeout=2).json()
            t.add_row(info["id"], f"[green]{info['role']}[/green]" if info["role"] == "leader" else info["role"], str(info["term"]), str(info["commit"]), str(info["leader"]))
        except Exception:
            t.add_row(cn["id"], "[red]caído[/red]", "-", "-", "-")
    out.print(t)
    t = Table(title="DataNodes")
    for col in ("id", "zona", "estado", "vivo", "bloques", "usado", "capacidad", "carga"):
        t.add_column(col)
    for d in st["datanodes"]:
        t.add_row(d["id"], d["zone"], d["state"], "[green]sí[/green]" if d["alive"] else "[red]NO[/red]", str(d["blocks"]), human(d["used"]), human(d["capacity"]), f"{d['load']:.0f}")
    out.print(t)
    f = st["fsck"]
    out.print(f"bloques={f['blocks']} sanos={f['healthy']} sub-replicados={f['under_replicated']} perdidos={f['lost']}  | archivos={st['df']['files']}  | mantenimiento={st.get('maintenance', {})}")


@cli.command()
@click.pass_obj
def users(app: App):
    """Lista de usuarios (admin)."""
    t = Table()
    for col in ("usuario", "grupos", "admin", "habilitado", "2FA"):
        t.add_column(col)
    for u in app.client.users():
        t.add_row(u["name"], ",".join(u["groups"]), str(u["admin"]), str(u["enabled"]), str(u["totp"]))
    out.print(t)


@cli.command()
@click.argument("name")
@click.option("--admin", "is_admin", is_flag=True)
@click.option("--group", "-g", multiple=True)
@click.option("--password", "-p")
@click.pass_obj
def useradd(app: App, name, is_admin, group, password):
    """Crea un usuario (admin)."""
    pw = password or getpass.getpass(f"Clave para {name}: ")
    app.client.useradd(name, pw, groups=list(group), admin=is_admin)
    out.print(f"usuario {name} creado (home: /home/{name})")


@cli.command()
@click.argument("name")
@click.pass_obj
def userdel(app: App, name):
    """Elimina un usuario (admin)."""
    app.client.userdel(name)


@cli.command()
@click.argument("name")
@click.option("--group", "-g", multiple=True, help="Reemplaza los grupos.")
@click.option("--disable", is_flag=True)
@click.option("--enable", is_flag=True)
@click.pass_obj
def usermod(app: App, name, group, disable, enable):
    """Modifica grupos / habilita / deshabilita (admin)."""
    app.client.usermod(name, groups=list(group) if group else None, enabled=False if disable else True if enable else None)


@cli.command()
@click.pass_obj
def passwd(app: App):
    """Cambia su propia clave."""
    old = getpass.getpass("Clave actual: ")
    new = getpass.getpass("Clave nueva: ")
    if new != getpass.getpass("Repita la clave nueva: "):
        raise click.UsageError("las claves no coinciden")
    app.client.change_password(old, new)
    app.session.token = ""
    out.print("Clave cambiada. Inicie sesión de nuevo.")


@cli.command(name="2fa-enable")
@click.pass_obj
def twofa_enable(app: App):
    """Activa 2FA (TOTP) para su cuenta."""
    r = app.client.enable_2fa(getpass.getpass("Clave: "))
    out.print(f"Secreto TOTP (guárdelo en su app autenticadora): [bold]{r['secret']}[/bold]\n{r['uri']}\n{r['message']}")
    app.session.token = ""


@cli.command(name="2fa-disable")
@click.option("--otp", prompt="Código 2FA")
@click.pass_obj
def twofa_disable(app: App, otp):
    """Desactiva 2FA."""
    app.client.disable_2fa(getpass.getpass("Clave: "), otp)
    app.session.token = ""


@cli.command(name="apikey-create")
@click.option("--label", default="")
@click.pass_obj
def apikey_create(app: App, label):
    """Crea una API key para automatización (variable DFSHA_API_KEY)."""
    r = app.client.create_api_key(label)
    out.print(f"id={r['id']}\nkey=[bold]{r['key']}[/bold]\n{r['message']}")


@cli.command(name="apikey-list")
@click.pass_obj
def apikey_list(app: App):
    for k in app.client.list_api_keys():
        out.print(f"{k['id']}  {k['label']}  {fmt_time(k['created'])}")


@cli.command(name="apikey-revoke")
@click.argument("key_id")
@click.pass_obj
def apikey_revoke(app: App, key_id):
    app.client.revoke_api_key(key_id)


@cli.command(name="dn-state")
@click.argument("dn")
@click.argument("state", type=click.Choice(["active", "decommissioning", "removed"]))
@click.pass_obj
def dn_state(app: App, dn, state):
    """Cambia el estado de un DataNode (admin): decommissioning migra sus bloques."""
    app.client.set_datanode_state(dn, state)


@cli.command()
@click.argument("dn")
@click.pass_obj
def scrub(app: App, dn):
    """Verifica la integridad en reposo de TODOS los bloques de un DataNode (admin)."""
    bad = app.client.scrub(dn)["corrupt"]
    out.print(f"[{'red' if bad else 'green'}]{dn}: {len(bad)} bloques corruptos puestos en cuarentena (se re-replican desde réplicas sanas)[/]")


@cli.command()
@click.pass_obj
def shell(app: App):
    """Shell interactivo (cd, ls, put, get, ... con directorio de trabajo)."""
    import readline  # noqa: F401
    import shlex

    c = app.client
    out.print(f"[bold]DFSha shell[/bold] — {c.user}@{','.join(c.endpoints)}  (help, exit)")
    while True:
        try:
            line = input(f"\x1b[1;32m{c.user or '?'}@dfsha\x1b[0m:\x1b[1;34m{c.cwd}\x1b[0m$ ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        line = line.strip()
        if not line:
            continue
        if line in ("exit", "quit", "logout"):
            break
        try:
            argv = shlex.split(line)
        except ValueError as e:
            err.print(f"[red]{e}[/red]")
            continue
        if argv[0] == "help":
            out.print("comandos: " + ", ".join(sorted(k for k in cli.commands if k != "shell")))
            continue
        if argv[0] == "shell":
            continue
        try:
            cli.main(args=argv, prog_name="dfsha", standalone_mode=False, obj=app)
        except E.DfshaError as e:
            err.print(f"[red]{argv[0]}:[/red] {e.message} [dim]({e.code})[/dim]")
        except click.ClickException as e:
            err.print(f"[red]{e.format_message()}[/red]")
        except click.exceptions.Exit:
            pass
        except click.Abort:
            pass
        except SystemExit:
            pass


def main() -> None:  # pragma: no cover
    home().mkdir(parents=True, exist_ok=True)
    cli(prog_name="dfsha")


if __name__ == "__main__":  # pragma: no cover
    main()

__all__ = ["main", "cli", "pystat"]
