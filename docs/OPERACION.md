# Operación y despliegue

Hay tres formas de ejecutar DFSha; las tres usan **exactamente el mismo código**:

| Modo | Para qué | Comando |
|---|---|---|
| **Nativo, cluster local** | desarrollo, demo, tests, benchmarks | `dfsha-cluster up` |
| **Docker Compose** | red aislada, “un solo comando”, entorno reproducible | `docker compose up -d --build` |
| **IaaS (AWS Academy)** | despliegue distribuido real en varias VM | `deploy/aws/` |

## 1. Requisitos

* Python ≥ 3.12 y [`uv`](https://docs.astral.sh/uv/) (`brew install uv`) — o Docker.
* Linux/macOS. El código no usa nada específico de plataforma.

```bash
git clone <repo> && cd SIST-DISTR
uv sync --extra dev --extra report      # crea .venv e instala todo
```

## 2. Modo nativo: cluster local multiproceso

```bash
uv run dfsha-cluster up --control 3 --data 5 --zones 2   # CA + certificados + 8 procesos
eval "$(uv run dfsha-cluster env)"                       # DFSHA_ENDPOINTS, DFSHA_CA, DFSHA_USER...
uv run dfsha login -u admin -p dfsha-admin-dev           # clave de desarrollo por defecto
uv run dfsha nodes                                       # ControlNodes (Raft) y DataNodes
uv run dfsha put ./archivo.bin /home/admin/archivo.bin
uv run dfsha blocks /home/admin/archivo.bin              # dónde está cada réplica
uv run dfsha get /home/admin/archivo.bin ./copia.bin
uv run dfsha shell                                       # shell interactivo
```

Caos y gestión del cluster local:

```bash
uv run dfsha-cluster status
uv run dfsha-cluster kill dn2        # SIGKILL a un DataNode
uv run dfsha-cluster kill leader     # SIGKILL al líder Raft
uv run dfsha-cluster start dn2       # lo vuelve a levantar
uv run dfsha-cluster down            # detiene (conserva datos)    |   destroy: borra todo
```

Cada nodo escribe su log en `.cluster/logs/<nodo>.log` y sus datos en `.cluster/data/<nodo>/`.

## 3. Docker Compose (3 ControlNodes + 5 DataNodes + cliente)

```bash
cp .env.example .env                  # y cambie ADMIN_PASSWORD (obligatoria; el valor del ejemplo es sólo un marcador)
docker compose build
docker compose up -d                  # pki-init genera CA/certificados/secreto; luego arrancan los nodos
docker compose ps                     # los 8 nodos deben quedar (healthy)
docker compose --profile client up -d client
docker compose exec client dfsha nodes
docker compose exec client dfsha put /work/archivo.bin /home/admin/archivo.bin
```

* Un contenedor por nodo (misma imagen `dfsha:latest`, distinto comando), red `dfsha-net`, usuario sin
  privilegios (uid 10001), `cap_drop: ALL`, `no-new-privileges`, puertos **internos (mTLS) sin publicar**.
* **Aislamiento de claves (mínimo privilegio).** El `docker-compose.yml` lo genera `docker/gen_compose.py`
  (no se edita a mano; `tests/test_compose.py` comprueba que está sincronizado). Cada nodo monta
  **sólo** su volumen `pki-<nodo>` (`ca.pem` + su propio `.crt/.key`); `cluster.key` y `admin.pass` están
  únicamente en `sec-<controlnode>` (los DataNodes jamás lo montan); `ca.key` vive sólo en el volumen
  `state`, privado del contenedor efímero `pki-init`. El cliente recibe un volumen `pki-client` con la CA
  pública. Comprobación: `docker exec dfsha-dn1 ls /pki /secrets` → `ca.pem dn1.crt dn1.key` y `/secrets` vacío.
* `state` conserva la CA y `cluster.key`: **no lo borre** si quiere conservar los datos almacenados
  (`docker compose down` lo respeta; `down -v` lo destruye todo, datos incluidos).
* Los nodos se anuncian por su nombre de servicio (`https://dn1:7441`): el contenedor **cliente** está
  en esa red y es la vía recomendada. Para usar el CLI desde el anfitrión haga falta que esos nombres
  resuelvan a `127.0.0.1` (línea `127.0.0.1 c1 c2 c3 dn1 dn2 dn3 dn4 dn5` en `/etc/hosts`; los puertos 8441-8443 y
  7441-7445 están publicados) y copie la CA del volumen de cliente:
  `docker run --rm -v dfsha_pki-client:/pki alpine cat /pki/ca.pem > ca.pem`; luego
  `DFSHA_ENDPOINTS=https://c1:8441,https://c2:8442,https://c3:8443 DFSHA_CA=./ca.pem dfsha login`.
* Caos: `docker compose kill dn2`, `docker compose stop c1`, `docker compose start c1`.
* **Estado de verificación.** Docker no estaba instalado en la máquina de desarrollo, por lo que la imagen y los contenedores
  **no se construyeron ni ejecutaron**. La configuración se validó por otras dos vías:
  1. `tests/test_compose.py` comprueba la estructura del compose y el reparto de claves (cada nodo monta sólo las suyas).
  2. `scripts/compose_dryrun.py` **lee el propio `docker-compose.yml`**, genera la PKI con `docker/init-pki.sh` hacia
     directorios por servicio (mismo reparto de volúmenes), arranca los 8 nodos usando únicamente las variables de
     entorno, puertos y URLs del compose (con los nombres de servicio resolviendo a 127.0.0.1), comprueba que ningún
     servicio ve claves ajenas, verifica `put`/`get` y la caída del líder.

  Quedan sin comprobar el `Dockerfile`, los permisos reales de los volúmenes y la red entre contenedores.

## 4. AWS Academy (IaaS)

Los scripts están en `deploy/aws/` del repositorio. Resumen: 3 EC2 ControlNode + N EC2 DataNode en
distintas zonas de disponibilidad (más una VM cliente dentro de la VPC para medir sin la latencia de su casa),
un *security group* que expone sólo las APIs de cliente (desde `ALLOW_CIDR`) y deja los puertos internos mTLS
únicamente entre miembros del grupo; certificados con SAN de IP privada y DNS público; contenedores con
`--restart unless-stopped`.

| Script (`deploy/aws/`) | Función |
|---|---|
| `preflight.sh` | comprueba CLI, credenciales del Learner Lab, clave PEM, región y cuotas **antes** de gastar nada |
| `launch.sh` / `refresh-inventory.sh` | crea SG + instancias y escribe `inventory.txt` (se refresca si las IP cambian al reiniciar el lab) |
| `gen-bundles.sh` | PKI y paquetes por nodo en `state/` (la CA y `cluster.key` **no** se regeneran: sin ellas los datos serían ilegibles) |
| `deploy.sh` | copia y arranca cada nodo; espera a que se forme el cluster |
| `status.sh` / `node.sh` | estado y operaciones por nodo (`kill`, `stop`, `start`, `logs`, `blk`, `leader`…) |
| `validate.sh` | validación de 10 pasos con caos real (`dfsha-cloud validate`), evidencia en `deploy/aws/evidence/<fecha>/` |
| `bench-remote.sh` | benchmarks desde la VM cliente (`dfsha-cloud bench`), resultados en `bench/results_cloud/` |
| `teardown.sh` | elimina instancias y SG (hágalo al terminar para no consumir créditos) |

## 5. Referencia de configuración

### Archivos de ejemplo (`*.env.example`)

Copie el que necesite sin la palabra `example` y edítelo; las copias reales (`.env`, `.env.client`, `deploy/aws/.env`) **no se versionan** (están en `.gitignore`).

| Archivo | Para qué | Cómo se usa |
|---|---|---|
| `.env.example` | Docker Compose (`ADMIN_PASSWORD`, replicación, tamaño de bloque, capacidad) | `cp .env.example .env` y `docker compose up -d` |
| `.env.client.example` | cliente (`dfsha`/SDK): endpoints, CA, usuario | `set -a; source .env.client; set +a` (PowerShell: ver el archivo) |
| `deploy/aws/.env.example` | scripts de AWS: región, clave SSH, IP permitida, tamaño del despliegue | `cp deploy/aws/.env.example deploy/aws/.env`; los scripts lo leen solos (lo exportado en el terminal tiene prioridad) |
| `docker/control.env.example` | todas las variables de un ControlNode | para arrancar `dfsha-control` a mano |
| `docker/data.env.example` | todas las variables de un DataNode | para arrancar `dfsha-data` a mano |


### ControlNode (`dfsha-control`, variables `DFSHA_*` o flags)
| Opción | Defecto | Descripción |
|---|---|---|
| `--id`, `--peers` | – | id propio y lista `id=url_publica,url_interna;…` de **todos** los ControlNodes |
| `--cert/--key/--ca` | – | certificado del nodo (rol `controlnode`) y CA |
| `--cluster-secret` | – | archivo con el secreto maestro (idéntico en los 3 ControlNodes) |
| `--admin-password[-file]` | – | clave del usuario `admin` en el primer arranque |
| `--repl` | 3 | factor de replicación por defecto |
| `--block-size` | 4MiB | tamaño de bloque por defecto |
| `--write-quorum` | majority | `majority` \| `all` \| `one` |
| `--election-timeout min max` / `--heartbeat` | 1.0 2.0 / 0.15 | temporización de Raft (s) |
| `--snapshot-threshold` | 2000 | entradas del log antes de compactar |
| `--dn-dead-after` | 10 | s sin heartbeat para declarar muerto un DataNode |
| `--repl-grace` | 15 | s de espera antes de re-replicar tras una caída |
| `--gc-grace` | 120 | s de gracia antes de borrar físicamente bloques huérfanos |
| `--balance-interval` / `--balance-tolerance` | 30 / 0.2 | balanceador (0 = off) |
| `--no-fsync` | – | **sólo pruebas**: desactiva fsync del log Raft |

### DataNode (`dfsha-data`)
| Opción | Defecto | Descripción |
|---|---|---|
| `--id`, `--controls` | – | id y URLs **internas** de los ControlNodes |
| `--zone` | default | zona/rack (para colocación de réplicas) |
| `--advertise-public`, `--advertise-internal` | – | URLs con que clientes / nodos lo alcanzan |
| `--capacity` | 0 | tope de bytes (0 = espacio libre del disco) |
| `--heartbeat` / `--report-interval` / `--scrub-interval` | 2 / 60 / 3600 | s |

## 6. Procedimientos (runbooks)

| Tarea | Procedimiento |
|---|---|
| **Añadir capacidad** | Emitir certificado (`dfsha-pki issue --role datanode`), arrancar el DataNode con `--controls …`. Se registra solo; el balanceador migra datos hacia él |
| **Retirar un DataNode** | `dfsha dn-state dnX decommissioning` → esperar a que `dfsha fsck` quede sano → `dfsha dn-state dnX removed` |
| **Reemplazar un ControlNode caído** | Volver a levantarlo con su directorio de datos: se pone al día por log o `InstallSnapshot`. (La membresía Raft es estática: para cambiar la lista de nodos reinicie los 3 con la nueva lista) |
| **Comprobar salud** | `dfsha nodes`, `dfsha fsck`, `GET /v1/cluster`, `GET /metrics` (mTLS) |
| **Verificar integridad en reposo de un DataNode** | `dfsha scrub dnX` (los blobs corruptos pasan a cuarentena y se re-replican) |
| **Backup de metadatos** | Copiar `data/<cN>/raft/` de un seguidor (snapshot + log) con el nodo detenido |
| **Rotar el certificado de un nodo** | `dfsha-pki issue` con el mismo `--name`; reiniciar el nodo |
| **Rotar `cluster.key`** | No automatizado: requiere re-envolver las DEK de todos los archivos (limitación documentada) |
| **Recuperación ante desastre total** | Restaurar `raft/` y los datos de los DataNodes; el informe de bloques reconcilia |

## 7. Observabilidad

* Logs por nodo (`INFO` por defecto); `audit.log` en cada ControlNode.
* `GET /metrics` (puerto interno, mTLS) — Prometheus: término Raft, líder, índice de commit, bloques, DataNodes vivos,
  bloques escritos/leídos/replicados por DataNode, bytes entrantes/salientes.
* `dfsha nodes` resume en una pantalla el estado de ambos planos.
