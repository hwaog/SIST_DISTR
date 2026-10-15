"""Genera las figuras del informe (PNG, 200 dpi) con matplotlib.

    uv run python report/figs.py            # diagramas + gráficas de bench/results/*.json
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).parent / "figs"
OUT.mkdir(exist_ok=True)
ROOT_DIR = Path(__file__).parent.parent
RES = ROOT_DIR / "bench" / "results"

C = {
    "client": "#dbeafe", "client_e": "#1d4ed8",
    "ctl": "#fde68a", "ctl_e": "#b45309", "leader": "#fbbf24",
    "dn_a": "#bbf7d0", "dn_b": "#fecaca", "dn_e": "#166534",
    "tls": "#2563eb", "mtls": "#16a34a", "raft": "#b45309", "warn": "#dc2626",
    "bg": "#f8fafc", "ink": "#0f172a", "grey": "#64748b",
}
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.titleweight": "bold"})


def box(ax, x, y, w, h, text, fc, ec, *, fs=9, bold=False, r=0.012, lw=1.4, ls="-", color=None, va="center"):
    p = FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0.0,rounding_size={r}", fc=fc, ec=ec, lw=lw, ls=ls, zorder=2)
    ax.add_patch(p)
    if text:
        ax.text(x + w / 2, y + h / 2, text, ha="center", va=va, fontsize=fs, fontweight="bold" if bold else "normal", color=color or C["ink"], zorder=3, linespacing=1.25)
    return (x, y, w, h)


def arrow(ax, p1, p2, color, *, text="", ls="-", lw=1.6, rad=0.0, style="-|>", tx=None, ty=None, fs=7.5, both=False, tcolor=None):
    a = FancyArrowPatch(p1, p2, arrowstyle="<|-|>" if both else style, mutation_scale=11, color=color, lw=lw, ls=ls, connectionstyle=f"arc3,rad={rad}", zorder=6)
    ax.add_patch(a)
    if text:
        mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
        ax.text(tx if tx is not None else mx, ty if ty is not None else my, text, ha="center", va="center", fontsize=fs, color=tcolor or color, zorder=7,
                bbox={"fc": "white", "ec": "none", "pad": 1.2, "alpha": 0.9})


def canvas(w=10, h=6, title=None):
    fig, ax = plt.subplots(figsize=(w, h))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    fig.patch.set_facecolor("white")
    if title:
        ax.text(0.5, 1.012, title, ha="center", va="bottom", fontsize=12, fontweight="bold", color=C["ink"])
    return fig, ax


def save(fig, name):
    fig.savefig(OUT / f"{name}.png", dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)


# --------------------------------------------------------------------------- diagramas
def fig_architecture():
    fig, ax = canvas(11, 7.2, "Arquitectura de DFSha: plano de control (Raft) y plano de datos")
    Z = 8  # zorder de flechas (por encima de las cajas)

    def arr(p1, p2, color, **kw):
        arrow(ax, p1, p2, color, **kw)
        for patch in ax.patches[-1:]:
            patch.set_zorder(Z)

    # ---- cliente
    box(ax, 0.01, 0.22, 0.17, 0.60, "", C["client"], C["client_e"])
    ax.text(0.095, 0.795, "CLIENTE", ha="center", fontsize=10, fontweight="bold", color=C["client_e"])
    for y, h, t in ((0.665, 0.085, "CLI  dfsha"), (0.545, 0.085, "Shell interactivo"), (0.35, 0.155, "SDK Python\nopen · read · write\nlock · put · get")):
        box(ax, 0.025, y, 0.14, h, t, "white", C["client_e"], fs=8)

    # ---- plano de control
    box(ax, 0.30, 0.575, 0.68, 0.355, "", "#fffbeb", C["ctl_e"], ls="--")
    ax.text(0.64, 0.905, "PLANO DE CONTROL  ·  metadatos replicados con Raft (red privada)", ha="center", fontsize=9.5, fontweight="bold", color=C["ctl_e"])
    cn = {}
    for x, n, fc in ((0.33, "ControlNode c1\nLÍDER", C["leader"]), (0.55, "ControlNode c2\nseguidor", C["ctl"]), (0.77, "ControlNode c3\nseguidor", C["ctl"])):
        box(ax, x, 0.69, 0.18, 0.15, n, fc, C["ctl_e"], fs=9, bold=True)
        cn[n[-8:]] = x
    arr((0.51, 0.765), (0.55, 0.765), C["raft"], both=True, lw=2)
    arr((0.73, 0.765), (0.77, 0.765), C["raft"], both=True, lw=2)
    arr((0.42, 0.69), (0.86, 0.69), C["raft"], both=True, lw=1.6, rad=0.12)
    ax.text(0.64, 0.612, "Raft: AppendEntries · votos · snapshots (mTLS)", ha="center", fontsize=8, color=C["raft"], fontweight="bold", bbox={"fc": "#fffbeb", "ec": "none", "pad": 1})
    ax.text(0.64, 0.585, "máquina de estados: namespace · ACL · bloques→réplicas · sesiones · locks · usuarios", ha="center", fontsize=7.3, color=C["grey"])

    # ---- plano de datos
    box(ax, 0.30, 0.05, 0.68, 0.40, "", "#f0fdf4", C["dn_e"], ls="--")
    ax.text(0.64, 0.425, "PLANO DE DATOS  ·  bloques cifrados (AES-256-GCM), réplicas en ≥ 2 zonas", ha="center", fontsize=9.5, fontweight="bold", color=C["dn_e"])
    box(ax, 0.315, 0.075, 0.305, 0.31, "", "#ecfdf5", "#86efac", lw=1)
    box(ax, 0.66, 0.075, 0.305, 0.31, "", "#fef2f2", "#fca5a5", lw=1)
    ax.text(0.325, 0.355, "Zona A", ha="left", fontsize=9, color=C["dn_e"], fontweight="bold")
    ax.text(0.675, 0.355, "Zona B", ha="left", fontsize=9, color=C["warn"], fontweight="bold")
    pos = {"dn1": (0.325, "a"), "dn3": (0.47, "a"), "dn2": (0.67, "b"), "dn4": (0.815, "b")}
    for n, (x, z) in pos.items():
        box(ax, x, 0.13, 0.14, 0.17, f"DataNode {n}\nbloques .blk\ncifrados", C["dn_a"] if z == "a" else C["dn_b"], C["dn_e"], fs=8)
    # tubería de replicación dn1 -> dn2 -> dn3 (ejemplo)
    arr((0.395, 0.30), (0.74, 0.30), C["mtls"], lw=1.6, rad=-0.16)
    arr((0.74, 0.13), (0.54, 0.13), C["mtls"], lw=1.6, rad=-0.16)
    ax.text(0.64, 0.088, "tubería de replicación dn1 → dn2 → dn3 (blob ya cifrado, mTLS)", ha="center", fontsize=7.5, color=C["mtls"], fontweight="bold", bbox={"fc": "#f0fdf4", "ec": "none", "pad": 1})

    # ---- flechas cliente
    arr((0.18, 0.74), (0.33, 0.77), C["tls"], lw=2)
    ax.text(0.245, 0.835, "HTTPS + JWT / API key", ha="center", fontsize=7.8, color=C["tls"], fontweight="bold")
    ax.text(0.245, 0.805, "metadatos y tokens de bloque", ha="center", fontsize=7.3, color=C["tls"])
    arr((0.18, 0.40), (0.30, 0.26), C["tls"], lw=2.4)
    ax.text(0.145, 0.14, "HTTPS + token de bloque", ha="center", fontsize=7.8, color=C["tls"], fontweight="bold")
    ax.text(0.145, 0.11, "bloques EN PARALELO a\ntodos los DataNodes", ha="center", fontsize=7.3, color=C["tls"], va="top")
    # control <-> datos
    arr((0.64, 0.575), (0.64, 0.45), C["mtls"], lw=2, both=True)
    ax.text(0.655, 0.512, "heartbeat · informe de bloques ↑   órdenes de replicar / borrar ↓   (mTLS)", ha="left", va="center", fontsize=7.5, color=C["mtls"], fontweight="bold")

    # ---- leyenda
    for x, col, lab in ((0.01, C["tls"], "TLS (usuarios)"), (0.20, C["mtls"], "mTLS con roles (nodos)"), (0.44, C["raft"], "consenso Raft")):
        ax.plot([x, x + 0.035], [0.015, 0.015], color=col, lw=2.5)
        ax.text(x + 0.045, 0.015, lab, va="center", fontsize=8)
    save(fig, "arquitectura")


def seq(ax, parts, steps, y0=0.90, dy=0.052, x0=0.075, x1=0.915):
    xs = [x0 + (x1 - x0) * i / (len(parts) - 1) for i in range(len(parts))]
    for x, p in zip(xs, parts, strict=True):
        box(ax, x - 0.058, 0.925, 0.116, 0.055, p, "#e2e8f0", "#475569", fs=7.3, bold=True)
        ax.plot([x, x], [0.925, 0.02], color="#94a3b8", lw=0.8, ls=":", zorder=0)
    y = y0
    for s in steps:
        kind = s[0]
        if kind == "msg":
            _, a, b, text, col = s
            arrow(ax, (xs[a], y), (xs[b], y), col, text=text, fs=6.8, ty=y + 0.016, tx=(xs[a] + xs[b]) / 2)
        elif kind == "self":
            _, a, text, col = s
            ax.text(xs[a] + 0.012, y, text, fontsize=6.6, color=col, va="center", style="italic")
        elif kind == "band":
            _, text = s
            ax.add_patch(plt.Rectangle((x0 - 0.07, y - dy * 0.45), x1 - x0 + 0.14, dy * 0.9, fc="#f1f5f9", ec="#cbd5e1", lw=0.6, zorder=0))
            ax.text(x0 - 0.065, y, text, fontsize=6.6, color=C["grey"], va="center", fontweight="bold")
        y -= dy
    return xs


def fig_write_sequence():
    fig, ax = canvas(10, 7.6, "Escritura de un archivo (put / open('wb')): bloques en paralelo + commit atómico")
    parts = ["Cliente", "ControlNode\n(líder)", "Raft\n(seguidores)", "DataNode A", "DataNode B", "DataNode C"]
    t, m, r = C["tls"], C["mtls"], C["raft"]
    steps = [
        ("msg", 0, 1, "1. begin(path, repl, block_size)", t),
        ("msg", 1, 2, "begin_write (sesión invisible)", r),
        ("msg", 2, 1, "commit del log", r),
        ("msg", 1, 0, "sid, block_size", t),
        ("msg", 0, 1, "2. alloc(N bloques)", t),
        ("self", 1, "placement: R nodos/bloque, ≥2 zonas", C["grey"]),
        ("msg", 1, 2, "alloc_blocks (1 entrada para N)", r),
        ("msg", 1, 0, "N × {block_id, pipeline, token sellado}", t),
        ("band", "3. por cada bloque, EN PARALELO (8 hilos)"),
        ("msg", 0, 3, "PUT /blocks/id · token · SHA-256", t),
        ("self", 3, "verifica hash · cifra AES-GCM · fsync", C["grey"]),
        ("msg", 3, 4, "blob cifrado (mTLS)", m),
        ("msg", 4, 5, "blob cifrado (mTLS)", m),
        ("msg", 5, 4, "acks", m),
        ("msg", 4, 3, "acks", m),
        ("msg", 3, 0, "acks [A, B, C]  (≥ W = mayoría)", t),
        ("band", "4. confirmación atómica"),
        ("msg", 0, 1, "commit(layout, sha, acks, if_version?)", t),
        ("msg", 1, 2, "commit_write (todo-o-nada)", r),
        ("msg", 2, 1, "mayoría + fsync", r),
        ("msg", 1, 0, "versión N+1 — el archivo ya es visible", t),
    ]
    seq(ax, parts, steps, dy=0.0445)
    save(fig, "secuencia_escritura")


def fig_layout():
    fig, ax = plt.subplots(figsize=(8.4, 3.6))
    dns = ["dn1\nzona A", "dn2\nzona B", "dn3\nzona A", "dn4\nzona B", "dn5\nzona A"]
    zone = ["a", "b", "a", "b", "a"]
    placement = [(0, 1, 4), (1, 2, 3), (0, 3, 2), (4, 1, 3), (2, 3, 0), (1, 4, 3), (0, 1, 2), (3, 4, 1)]
    for r, reps in enumerate(placement):
        for c in range(5):
            ax.add_patch(plt.Rectangle((c, 7 - r), 1, 1, fc="#f8fafc", ec="#cbd5e1", lw=0.7))
            if c in reps:
                ax.add_patch(plt.Rectangle((c + 0.12, 7 - r + 0.12), 0.76, 0.76, fc=C["dn_a"] if zone[c] == "a" else C["dn_b"], ec=C["dn_e"], lw=1))
                ax.text(c + 0.5, 7 - r + 0.5, "R" + str(reps.index(c) + 1), ha="center", va="center", fontsize=8, fontweight="bold")
        ax.text(-0.15, 7 - r + 0.5, f"bloque {r}", ha="right", va="center", fontsize=8)
    for c, d in enumerate(dns):
        ax.text(c + 0.5, 8.25, d, ha="center", va="bottom", fontsize=8, fontweight="bold")
    ax.set_xlim(-1.5, 5.2)
    ax.set_ylim(-0.1, 9.3)
    ax.axis("off")
    ax.set_title("Un archivo de 8 bloques: cada bloque en 3 DataNodes distintos y en ambas zonas", fontsize=10, fontweight="bold")
    save(fig, "mapa_replicas")


def fig_security():
    fig, ax = canvas(10, 6.3, "Seguridad por capas")
    box(ax, 0.01, 0.58, 0.20, 0.34, "", C["client"], C["client_e"])
    ax.text(0.11, 0.90, "CLIENTE / INTERNET", ha="center", fontsize=8.5, fontweight="bold", color=C["client_e"])
    ax.text(0.11, 0.74, "usuario + clave\n(+ 2FA TOTP)\n\nAPI key\n\nverifica al servidor\ncon la CA del cluster", ha="center", va="center", fontsize=7.8)
    box(ax, 0.26, 0.05, 0.72, 0.88, "", "#f8fafc", "#475569", ls="--")
    ax.text(0.62, 0.91, "RED PRIVADA DEL SERVICIO (security group: puertos internos sólo entre miembros)", ha="center", fontsize=8.5, fontweight="bold", color="#334155")
    box(ax, 0.28, 0.58, 0.30, 0.28, "", "#fffbeb", C["ctl_e"])
    ax.text(0.43, 0.84, "ControlNode", ha="center", fontsize=8.5, fontweight="bold", color=C["ctl_e"])
    ax.text(0.43, 0.70, "· JWT firmado + versión de token\n· scrypt · throttling · auditoría\n· ACL/POSIX en la FSM (Raft)\n· KEK: envuelve DEK de archivos\n· emite tokens de bloque sellados", ha="center", va="center", fontsize=7.3)
    box(ax, 0.64, 0.58, 0.32, 0.28, "", "#f0fdf4", C["dn_e"])
    ax.text(0.80, 0.84, "DataNode", ha="center", fontsize=8.5, fontweight="bold", color=C["dn_e"])
    ax.text(0.80, 0.70, "· valida token sellado (AES-GCM)\n· descifra con DEK sólo en RAM\n· NO guarda claves\n· AES-256-GCM por bloque (AAD=id)\n· hash de cola → scrubber", ha="center", va="center", fontsize=7.3)
    box(ax, 0.64, 0.10, 0.32, 0.30, "", "#fef9c3", "#a16207")
    ax.text(0.80, 0.37, "DISCO del DataNode", ha="center", fontsize=8.5, fontweight="bold", color="#a16207")
    ax.text(0.80, 0.22, "blobs cifrados: MAGIC · nonce ·\nciphertext+tag · SHA-256\nrobar el disco = ni texto\nclaro ni clave", ha="center", va="center", fontsize=7.3)
    box(ax, 0.28, 0.10, 0.30, 0.30, "", "#ede9fe", "#6d28d9")
    ax.text(0.43, 0.37, "PKI propia", ha="center", fontsize=8.5, fontweight="bold", color="#6d28d9")
    ax.text(0.43, 0.22, "CA raíz · cert ECDSA por nodo\nOU = rol (controlnode, datanode)\nCN = id (anti-suplantación)\nmTLS CERT_REQUIRED", ha="center", va="center", fontsize=7.3)
    arrow(ax, (0.21, 0.76), (0.28, 0.76), C["tls"], text="TLS 1.2+\nJWT", fs=7, both=True)
    arrow(ax, (0.11, 0.58), (0.72, 0.58), C["tls"], text="TLS + token de bloque sellado\n(bloques, sin pasar por el ControlNode)", rad=0.16, tx=0.43, ty=0.455, fs=7)
    arrow(ax, (0.58, 0.72), (0.64, 0.72), C["mtls"], text="mTLS", fs=7, both=True)
    arrow(ax, (0.80, 0.58), (0.80, 0.40), C["warn"], text="AES-256-GCM", tx=0.875, fs=7)
    save(fig, "seguridad")


def fig_aws():
    fig, ax = canvas(10, 5.6, "Despliegue de referencia en AWS Academy (7 instancias EC2)")
    box(ax, 0.01, 0.38, 0.13, 0.22, "Su portátil\nCLI dfsha\n(HTTPS)", C["client"], C["client_e"], fs=8, bold=True)
    box(ax, 0.20, 0.04, 0.78, 0.84, "", "#f8fafc", "#475569", ls="--")
    ax.text(0.59, 0.845, "VPC por defecto  ·  security group dfsha-sg  ·  API pública 8441-8443 / 7441-7449\ninterno (mTLS) sólo entre miembros del propio SG", ha="center", va="center", fontsize=7.6, fontweight="bold", color="#334155")
    cols = [("us-east-1a", 0.22), ("us-east-1b", 0.47), ("us-east-1c", 0.72)]
    for az, x in cols:
        box(ax, x, 0.08, 0.24, 0.70, "", "white", "#cbd5e1", lw=1)
        ax.text(x + 0.12, 0.75, f"AZ {az}", ha="center", fontsize=8, fontweight="bold", color="#475569")
    ctl = ["c1", "c2", "c3"]
    for (_az, x), n in zip(cols, ctl, strict=True):
        box(ax, x + 0.02, 0.52, 0.20, 0.20, f"EC2 {n}\nControlNode (Docker)\nRaft · t3.small · EBS 10 GB", C["ctl"], C["ctl_e"], fs=7.3)
    dns = {0: ["dn1", "dn4"], 1: ["dn2"], 2: ["dn3"]}
    for i, (az, x) in enumerate(cols):
        for k, n in enumerate(dns[i]):
            box(ax, x + 0.02, 0.36 - k * 0.15, 0.20, 0.12, f"EC2 {n}  ·  DataNode\nEBS gp3 30 GB · zona {az[-2:]}", C["dn_a"], C["dn_e"], fs=7.2)
    arrow(ax, (0.14, 0.52), (0.22, 0.62), C["tls"], text="HTTPS", fs=7)
    arrow(ax, (0.14, 0.44), (0.22, 0.30), C["tls"], text="HTTPS", fs=7)
    arrow(ax, (0.46, 0.62), (0.47, 0.62), C["raft"], both=True)
    arrow(ax, (0.71, 0.62), (0.72, 0.62), C["raft"], both=True)
    ax.text(0.59, 0.095, "Réplicas de cada bloque repartidas entre ≥2 AZ: la pérdida de una zona completa no pierde datos", ha="center", fontsize=7.8, color=C["dn_e"], fontweight="bold")
    save(fig, "aws")


def fig_repair():
    fig, ax = canvas(10, 3.6, "Ciclo de autorreparación tras la caída de un DataNode")
    steps = [
        ("t = 0", "SIGKILL\ndn1", "#fecaca"), ("t ≈ 5-6 s", "sin heartbeat:\nmuerto\n(dn_dead_after)", "#fed7aa"),
        ("+ 3 s", "periodo de gracia\n(repl_grace)\nfallos breves no\nmueven datos", "#fef08a"), ("+ ciclo 2 s", "ControlNode elige\ndestinos y ordena\ncopia DN→DN", "#d9f99d"),
        ("≈ 4 s / 256 MiB", "copia verificada\n(hash) + réplica\nregistrada por Raft", "#bbf7d0"), ("OK  (t ≈ 10 s)", "R réplicas vivas\nde nuevo\n(fsck sano)", "#86efac"),
    ]
    w = 0.145
    for i, (t, txt, col) in enumerate(steps):
        x = 0.02 + i * 0.163
        box(ax, x, 0.30, w, 0.40, txt, col, "#334155", fs=7.5)
        ax.text(x + w / 2, 0.78, t, ha="center", fontsize=8, fontweight="bold", color="#334155")
        if i < len(steps) - 1:
            arrow(ax, (x + w, 0.50), (x + 0.163, 0.50), "#334155")
    ax.text(0.5, 0.12, "Mientras tanto las lecturas conmutan a otra réplica y las escrituras saltan el nodo caído: el servicio no se interrumpe.", ha="center", fontsize=8, style="italic", color=C["grey"])
    save(fig, "reparacion")


# ---------------------------------------------------------------------------- gráficas
def _load(name):
    p = RES / f"{name}.json"
    return json.loads(p.read_text())["result"] if p.exists() else None


def _bars(ax, labels, vals, sds=None, color="#2563eb", w=0.38, off=0.0, label=None):
    xs = range(len(labels))
    ax.bar([x + off for x in xs], vals, width=w, yerr=sds, color=color, capsize=3, label=label, ec="white")


def fig_scaling():
    r = _load("scaling")
    if not r:
        return
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.5), sharey=True)
    for ax, key, title in zip(axes, ("repl1", "repl3"), ("Replicación ×1 (aísla el reparto de bloques)", "Replicación ×3 (producción)"), strict=True):
        d = r[key]
        ns = sorted(d, key=int)
        up = [d[n]["upload_MiBps"]["mean"] for n in ns]
        dn = [d[n]["download_MiBps"]["mean"] for n in ns]
        us = [d[n]["upload_MiBps"]["sd"] for n in ns]
        ds = [d[n]["download_MiBps"]["sd"] for n in ns]
        _bars(ax, ns, up, us, "#2563eb", off=-0.2, label="subida")
        _bars(ax, ns, dn, ds, "#16a34a", off=0.2, label="bajada")
        ax.set_xticks(range(len(ns)), ns)
        ax.set_xlabel("DataNodes")
        ax.set_title(title, fontsize=9)
        ax.grid(axis="y", alpha=0.3)
    axes[0].set_ylabel("MiB/s (archivo de 256 MiB)")
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    save(fig, "bench_escalado")


def fig_blocksize():
    r = _load("blocksize")
    if not r:
        return
    fig, ax = plt.subplots(figsize=(6.2, 3.3))
    ks = list(r)
    _bars(ax, ks, [r[k]["upload_MiBps"]["mean"] for k in ks], [r[k]["upload_MiBps"]["sd"] for k in ks], "#2563eb", off=-0.2, label="subida")
    _bars(ax, ks, [r[k]["download_MiBps"]["mean"] for k in ks], [r[k]["download_MiBps"]["sd"] for k in ks], "#16a34a", off=0.2, label="bajada")
    ax.set_xticks(range(len(ks)), ks)
    ax.set_xlabel("tamaño de bloque")
    ax.set_ylabel("MiB/s")
    ax.set_title("Efecto del tamaño de bloque (256 MiB, 4 DataNodes, ×3)", fontsize=9)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    save(fig, "bench_bloque")


def fig_workers():
    r = _load("workers")
    if not r:
        return
    fig, ax = plt.subplots(figsize=(6.2, 3.3))
    ks = sorted(r, key=int)
    ax.errorbar(ks, [r[k]["upload_MiBps"]["mean"] for k in ks], [r[k]["upload_MiBps"]["sd"] for k in ks], marker="o", color="#2563eb", label="subida", capsize=3)
    ax.errorbar(ks, [r[k]["download_MiBps"]["mean"] for k in ks], [r[k]["download_MiBps"]["sd"] for k in ks], marker="s", color="#16a34a", label="bajada", capsize=3)
    ax.set_xlabel("transferencias paralelas de bloques (workers)")
    ax.set_ylabel("MiB/s")
    ax.set_title("Efecto del paralelismo por bloque (256 MiB, 4 DataNodes, ×3)", fontsize=9)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    save(fig, "bench_workers")


def fig_clients():
    r = _load("clients")
    if not r:
        return
    fig, ax = plt.subplots(figsize=(6.2, 3.3))
    ks = sorted(r, key=int)
    ax.plot(ks, [r[k]["agg_MiBps"] for k in ks], marker="o", color="#7c3aed", label="agregado")
    ax.plot(ks, [r[k]["per_client_MiBps"] for k in ks], marker="s", color="#f59e0b", label="por cliente")
    ax.set_xlabel("clientes concurrentes (64 MiB subida+bajada c/u)")
    ax.set_ylabel("MiB/s")
    ax.set_title("Escalabilidad en número de clientes (6 DataNodes)", fontsize=9)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    save(fig, "bench_clientes")


def fig_metadata():
    r = _load("metadata")
    if not r:
        return
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.4))
    for ax, fs in zip(axes, ("off", "on"), strict=True):
        keys = [k for k in r if k.startswith(f"fsync={fs}")]
        keys.sort(key=lambda k: int(k.split("=")[-1]))
        th = [int(k.split("=")[-1]) for k in keys]
        for op, col, lab in (("stat_linearizable", "#2563eb", "stat linealizable (líder, lease)"), ("stat_stale_followers", "#16a34a", "stat relajado (cualquier réplica)"), ("mkdir_raft_write", "#dc2626", "mkdir (escritura Raft)")):
            ax.plot(th, [r[k][op]["ops_per_s"] for k in keys], marker="o", color=col, label=lab)
        ax.set_xscale("log", base=2)
        ax.set_xticks(th, th)
        ax.set_yscale("log")
        ax.set_xlabel("clientes concurrentes")
        ax.set_ylabel("operaciones/s")
        ax.set_title(f"Metadatos · fsync {'activado' if fs == 'on' else 'desactivado'}", fontsize=9)
        ax.grid(alpha=0.3, which="both")
    axes[0].legend(fontsize=7)
    fig.tight_layout()
    save(fig, "bench_metadatos")


def fig_scaling_bw():
    r = _load("scaling_bw")
    if not r:
        return
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.6))
    for ax, key, title, ideal in (
        (axes[0], "repl1", "Replicación ×1", lambda n: 40 * n),
        (axes[1], "repl3", "Replicación ×3", lambda n: 40 * n / 3),
    ):
        d = r[key]
        ns = sorted(d, key=int)
        xs = [int(n) for n in ns]
        ax.errorbar(xs, [d[n]["upload_MiBps"]["mean"] for n in ns], [d[n]["upload_MiBps"]["sd"] for n in ns], marker="o", color="#2563eb", label="subida medida", capsize=3)
        ax.errorbar(xs, [d[n]["download_MiBps"]["mean"] for n in ns], [d[n]["download_MiBps"]["sd"] for n in ns], marker="s", color="#16a34a", label="bajada medida", capsize=3)
        ax.plot(xs, [ideal(n) for n in xs], ls="--", color="#2563eb", alpha=0.5, label=f"techo teórico subida ({'40·N' if key == 'repl1' else '40·N/3'})")
        if key == "repl1":
            ax.plot(xs, [40 * n for n in xs], ls=":", color="#16a34a", alpha=0.5, label="techo teórico bajada (40·N)")
        ax.set_xlabel("DataNodes")
        ax.set_xticks(xs)
        ax.set_ylabel("MiB/s")
        ax.set_title(f"{title} · tope de 40 MiB/s por DataNode", fontsize=9)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=7)
    fig.tight_layout()
    save(fig, "bench_escalado_tope")


def fig_cloud_scaling(cloud: Path | None = None, local: Path | None = None, out: Path | None = None) -> bool:
    """Escalado medido en VMs separadas (si existe bench/results_cloud/scaling.json) frente a una sola máquina."""
    cloud = cloud or ROOT_DIR / "bench" / "results_cloud" / "scaling.json"
    local = local or RES / "scaling.json"
    if not cloud.exists():
        return False
    c = json.loads(cloud.read_text())["result"]
    loc = json.loads(local.read_text())["result"] if local.exists() else {}
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.6))
    for ax, key, title in ((axes[0], "repl1", "Replicación ×1"), (axes[1], "repl3", "Replicación ×3")):
        d = c.get(key, {})
        ks = sorted(d, key=int)
        xs = [int(k) for k in ks]
        if not xs:
            continue
        ax.errorbar(xs, [d[k]["upload_MiBps"]["mean"] for k in ks], [d[k]["upload_MiBps"]["sd"] for k in ks], marker="o", color="#2563eb", label="nube: subida", capsize=3)
        ax.errorbar(xs, [d[k]["download_MiBps"]["mean"] for k in ks], [d[k]["download_MiBps"]["sd"] for k in ks], marker="s", color="#16a34a", label="nube: bajada", capsize=3)
        ld = loc.get(key, {})
        lk = [k for k in sorted(ld, key=int) if int(k) in xs]
        if lk:
            ax.plot([int(k) for k in lk], [ld[k]["upload_MiBps"]["mean"] for k in lk], ls="--", color="#2563eb", alpha=0.45, label="una sola máquina: subida")
            ax.plot([int(k) for k in lk], [ld[k]["download_MiBps"]["mean"] for k in lk], ls="--", color="#16a34a", alpha=0.45, label="una sola máquina: bajada")
        ax.set_xlabel("DataNodes activos")
        ax.set_xticks(xs)
        ax.set_ylabel("MiB/s")
        ax.set_title(title, fontsize=9)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=7)
    fig.tight_layout()
    if out is None:
        save(fig, "cloud_escalado")
    else:
        fig.savefig(out, dpi=200, bbox_inches="tight", facecolor="white")
        plt.close(fig)
    return True


def fig_availability():
    r = _load("availability")
    if not r:
        return
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(9.4, 4.6), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    sm = r.get("samples", [])
    for kind, col, lab in (("read", "#2563eb", "lectura de 2 MiB"), ("write", "#dc2626", "escritura de 2 MiB")):
        pts = [(t, v) for t, k, v in sm if k == kind]
        ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=3, color=col, alpha=0.45, label=lab)
    ax.set_yscale("log")
    ax.set_ylabel("latencia (ms, log)")
    ax.grid(alpha=0.3, which="both")
    ax.legend(fontsize=8, markerscale=4)
    ax.set_title(f"Carga continua (6 clientes) durante fallos · éxito: lecturas {r['availability_pct']['read']:.2f} % · escrituras {r['availability_pct']['write']:.2f} %", fontsize=9)
    ax2.set_ylim(0, 1)
    ax2.set_yticks([])
    for i, (t, what) in enumerate(r["events"]):
        col = "#16a34a" if what.startswith("start") else "#dc2626"
        ax.axvline(t, color=col, ls="--", lw=1)
        ax2.axvline(t, color=col, ls="--", lw=1)
        ax2.text(t + 0.4, 0.85 - 0.28 * (i % 3), what, fontsize=7, color=col, va="top")
    ax2.set_xlabel("segundos")
    fig.tight_layout()
    save(fig, "bench_disponibilidad")


def main() -> None:
    for fn in (fig_architecture, fig_write_sequence, fig_layout, fig_security, fig_aws, fig_repair, fig_scaling, fig_blocksize, fig_workers, fig_clients, fig_metadata, fig_scaling_bw, fig_cloud_scaling, fig_availability):
        r = fn()
        print("omitida (sin datos)" if r is False else "ok", fn.__name__)


if __name__ == "__main__":
    main()
