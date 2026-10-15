#!/usr/bin/env python3
"""Genera docker-compose.yml (fuente única de verdad: edite AQUÍ y regenere).

    python docker/gen_compose.py            # escribe ./docker-compose.yml
    python docker/gen_compose.py --control 3 --data 5

Aislamiento de claves (mínimo privilegio): CADA nodo monta su PROPIO volumen con únicamente
``ca.pem`` + su certificado + su clave; el secreto maestro (``cluster.key``) sólo llega a los ControlNodes;
la clave de la CA (``ca.key``) vive en el volumen ``state`` que sólo monta el contenedor efímero ``pki-init``.
Así un DataNode comprometido no puede firmarse un certificado con rol ``controlnode``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build(n_control: int = 3, n_data: int = 5) -> str:
    controls = [(f"c{i}", 8440 + i, 9440 + i) for i in range(1, n_control + 1)]
    zones = ["zone-a", "zone-b"]
    datas = [(f"dn{j}", 7440 + j, 7540 + j, zones[(j - 1) % 2]) for j in range(1, n_data + 1)]
    peers = ";".join(f"{n}=https://{n}:{p},https://{n}:{i}" for n, p, i in controls)
    ctl_urls = ",".join(f"https://{n}:{i}" for n, _p, i in controls)
    endpoints = ",".join(f"https://{n}:{p}" for n, p, _i in controls)
    L: list[str] = []
    a = L.append

    a(f"# DFSha — cluster de referencia: {n_control} ControlNodes (Raft) + {n_data} DataNodes en 2 zonas + contenedor cliente.")
    a("# GENERADO por docker/gen_compose.py — no editar a mano (edite el generador y regenere).")
    a("#")
    a("# Uso:  cp .env.example .env   # y cambie ADMIN_PASSWORD")
    a("#       docker compose build && docker compose up -d")
    a("#       docker compose --profile client run --rm client dfsha login")
    a("#")
    a("# Aislamiento de claves: cada nodo monta SÓLO su volumen pki-<nodo> (ca.pem + su .crt + su .key);")
    a("# cluster.key sólo existe en sec-<controlnode>; ca.key sólo en el volumen 'state' de pki-init.")
    a("name: dfsha")
    a("")
    a("x-node: &node")
    a("  image: dfsha:latest")
    a("  restart: unless-stopped")
    a("  pull_policy: never")
    a("  networks: [dfsha-net]")
    a("  cap_drop: [ALL]")
    a("  security_opt: [\"no-new-privileges:true\"]")
    a("")
    a("services:")
    a("  pki-init:")
    a("    build: {context: ., dockerfile: docker/Dockerfile}")
    a("    image: dfsha:latest")
    a("    user: root")
    a("    entrypoint: [\"/bin/sh\", \"/app/init-pki.sh\"]")
    a("    environment:")
    a("      CONTROLS: \"" + " ".join(n for n, *_ in controls) + "\"")
    a("      DATAS: \"" + " ".join(d[0] for d in datas) + "\"")
    a("      ADMIN_PASSWORD: ${ADMIN_PASSWORD:?defina ADMIN_PASSWORD en .env}")
    a("    volumes:")
    a("      - state:/state                       # PRIVADO: ca.key, claves emitidas y cluster.key (nadie más lo monta)")
    for n, *_ in controls:
        a(f"      - pki-{n}:/out/pki/{n}")
    for d in datas:
        a(f"      - pki-{d[0]}:/out/pki/{d[0]}")
    a("      - pki-client:/out/pki/client          # sólo ca.pem")
    for n, *_ in controls:
        a(f"      - sec-{n}:/out/sec/{n}")
    a("      - ./docker/init-pki.sh:/app/init-pki.sh:ro")
    a("    networks: [dfsha-net]")
    a("    restart: \"no\"")
    a("")
    for n, p, i in controls:
        a(f"  {n}:")
        a("    <<: *node")
        a(f"    container_name: dfsha-{n}")
        a("    command: [\"dfsha-control\"]")
        a("    environment:")
        a(f"      DFSHA_NODE_ID: {n}")
        a(f"      DFSHA_PEERS: \"{peers}\"")
        a("      DFSHA_DATA_DIR: /data")
        a(f"      DFSHA_CERT: /pki/{n}.crt")
        a(f"      DFSHA_KEY: /pki/{n}.key")
        a("      DFSHA_CA: /pki/ca.pem")
        a("      DFSHA_CLUSTER_SECRET_FILE: /secrets/cluster.key")
        a("      DFSHA_ADMIN_PASSWORD_FILE: /secrets/admin.pass")
        a("      DFSHA_REPL: ${DFSHA_REPL:-3}")
        a("      DFSHA_BLOCK_SIZE: ${DFSHA_BLOCK_SIZE:-4MiB}")
        a(f"    ports: [\"{p}:{p}\"]   # API pública; el puerto interno ({i}, mTLS) NO se publica")
        a("    volumes:")
        a(f"      - pki-{n}:/pki:ro")
        a(f"      - sec-{n}:/secrets:ro")
        a(f"      - {n}-data:/data")
        a(f"    healthcheck: {{test: [\"CMD\", \"dfsha-healthcheck\", \"{p}\"], interval: 5s, timeout: 4s, retries: 12, start_period: 5s}}")
        a("    depends_on: {pki-init: {condition: service_completed_successfully}}")
        a("")
    for n, p, i, z in datas:
        a(f"  {n}:")
        a("    <<: *node")
        a(f"    container_name: dfsha-{n}")
        a("    command: [\"dfsha-data\"]")
        a("    environment:")
        a(f"      DFSHA_NODE_ID: {n}")
        a(f"      DFSHA_CONTROLS: \"{ctl_urls}\"")
        a("      DFSHA_DATA_DIR: /data")
        a(f"      DFSHA_CERT: /pki/{n}.crt")
        a(f"      DFSHA_KEY: /pki/{n}.key")
        a("      DFSHA_CA: /pki/ca.pem")
        a(f"      DFSHA_ZONE: {z}")
        a(f"      DFSHA_API_PORT: {p}")
        a(f"      DFSHA_INTERNAL_PORT: {i}")
        a(f"      DFSHA_ADVERTISE_PUBLIC: https://{n}:{p}")
        a(f"      DFSHA_ADVERTISE_INTERNAL: https://{n}:{i}")
        a("      DFSHA_CAPACITY: ${DFSHA_DN_CAPACITY:-20GiB}")
        a(f"    ports: [\"{p}:{p}\"]")
        a("    volumes:                             # un DataNode NO recibe cluster.key ni la clave de la CA")
        a(f"      - pki-{n}:/pki:ro")
        a(f"      - {n}-data:/data")
        a(f"    healthcheck: {{test: [\"CMD\", \"dfsha-healthcheck\", \"{p}\"], interval: 5s, timeout: 4s, retries: 12, start_period: 5s}}")
        a("    depends_on: {pki-init: {condition: service_completed_successfully}}")
        a("")
    a("  client:")
    a("    <<: *node")
    a("    container_name: dfsha-client")
    a("    profiles: [client]")
    a("    command: [\"sleep\", \"infinity\"]")
    a("    environment:")
    a(f"      DFSHA_ENDPOINTS: \"{endpoints}\"")
    a("      DFSHA_CA: /pki/ca.pem")
    a("      DFSHA_USER: admin")
    a("      DFSHA_PASSWORD: ${ADMIN_PASSWORD}")
    a("      DFSHA_HOME: /home/dfsha/.dfsha")
    a("    volumes:")
    a("      - pki-client:/pki:ro                 # sólo la CA pública")
    a("      - ./work:/work")
    a("    working_dir: /work")
    a("    depends_on: {pki-init: {condition: service_completed_successfully}}")
    a("")
    a("networks:")
    a("  dfsha-net: {}")
    a("")
    a("volumes:")
    a("  state: {}")
    for n, *_ in controls:
        a(f"  pki-{n}: {{}}")
        a(f"  sec-{n}: {{}}")
        a(f"  {n}-data: {{}}")
    for d in datas:
        a(f"  pki-{d[0]}: {{}}")
        a(f"  {d[0]}-data: {{}}")
    a("  pki-client: {}")
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--control", type=int, default=3)
    ap.add_argument("--data", type=int, default=5)
    ap.add_argument("-o", "--out", default=str(ROOT / "docker-compose.yml"))
    args = ap.parse_args()
    Path(args.out).write_text(build(args.control, args.data))
    print(f"escrito {args.out}")


if __name__ == "__main__":
    main()
