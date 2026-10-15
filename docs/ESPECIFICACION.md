# Definición del servicio DFSha

## 1. Problema y objetivo

Almacenar y recuperar **archivos grandes** de forma distribuida en múltiples nodos, con **alta
disponibilidad, rendimiento, transparencia y seguridad**. Tanto la escritura como la lectura deben
repartirse entre varios nodos, y cada archivo debe estar a su vez particionado en varios nodos.

DFSha es un sistema de archivos distribuido **basado en bloques** (familia GFS/HDFS) con una
arquitectura **Cliente/Servidor (Opción 1)** en la que el servidor *S* no es un único proceso, sino un
servicio **compuesto y distribuido** (S2S) de dos clases de nodos autónomos que corren dentro de una
red privada y exponen APIs abiertas (REST/HTTPS):

| Rol | Cantidad típica | Responsabilidad |
|---|---|---|
| **Cliente DFSha** (CLI, shell, SDK Python) | n | Abstracción de sistema de archivos jerárquico tipo Linux; no forma parte del servicio |
| **ControlNode** | 3 (o 5) | Metadatos, autenticación/autorización, ubicación de bloques, coordinación. Replicados con **Raft** |
| **DataNode** | N (≥3) | Almacenan bloques **cifrados**; replican en tubería; verifican integridad |

## 2. Alcance de la Opción 1 (C/S con S compuesto y distribuido)

* **Autonomía S-S**: todos los nodos pertenecen a una sola organización y se autentican entre sí con
  **mTLS** (CA propia); el servicio corre en una red privada (real o virtual) y solo expone al exterior
  las APIs de cliente.
* **Mecanismos abiertos C↔S**: HTTPS/JSON (REST) para metadatos y HTTPS binario para bloques; todo nodo
  expone una API documentada (OpenAPI autogenerada en `/docs` del ControlNode).
* **Composición**: el cliente compone una operación de archivo orquestando ControlNode (metadatos) y
  DataNodes (datos); los DataNodes componen la tubería de replicación entre sí; los ControlNodes
  componen el consenso entre sí.

## 3. Requisitos funcionales

| ID | Requisito | Cómo se cubre |
|---|---|---|
| **RF1** | Gestión del sistema de archivos: `ls, cd, mkdir, rmdir, rm, mv, stat, chmod, chown, ACL…` | `dfsha ls/cd/pwd/mkdir/rmdir/rm/mv/cp/cat/tree/chmod/chown/setfacl/getfacl` + `dfsha shell` con directorio de trabajo; SDK `Client.ls/mkdir/…` |
| **RF2** | Transferencia de archivos: `send()` / `receive()` | `Client.send/receive` (memoria) y `Client.put/get` (archivos locales); `dfsha put/get`, transferencias **paralelas por bloque**, con progreso y verificación SHA-256 |
| **RF3** | Acceso a archivos: `open(), close(), read(), write(), lock()…` | `Client.open(path, 'rb'/'wb'/'ab'/'r+b'/texto)` devuelve objetos tipo archivo con `read/seek/write/truncate`; `Client.lock()` (compartido/exclusivo, con *lease* y *fencing token*) |
| **RF4** | Autenticación y control de acceso por usuario o grupo | usuario/clave, **2FA TOTP**, API keys, permisos POSIX (rwx dueño/grupo/otros) + **ACL** por usuario/grupo |
| **RF5** | Operación administrativa | alta/baja de usuarios y grupos, estado de DataNodes (`active/decommissioning/removed`), `fsck`, `nodes`, métricas Prometheus |

## 4. Requisitos no funcionales (mapeados a la rúbrica)

| ID | Requisito | Mecanismo clave | Verificación |
|---|---|---|---|
| **RNF1 Escalabilidad** | usuarios, archivos, tamaño de archivo | Archivos en bloques inmutables repartidos por política de colocación; plano de datos escala añadiendo DataNodes; metadatos en memoria con lecturas servibles desde seguidores (`--stale`); bloques de tamaño configurable (4 MiB por defecto); asignación de bloques por lotes (1 consenso por ≤512 bloques) | `bench/` (escalado en DataNodes, clientes, tamaño de bloque, paralelismo) |
| **RNF2 Alta disponibilidad** | sin caídas del servicio ni pérdida de datos | Raft con 3 réplicas de metadatos (tolera 1 caída; 5 → 2); replicación ×3 de datos; re-replicación automática; *failover* de líder en ≈1,5 s (mediana medida; ≤4 s); el cliente reintenta de forma transparente | pruebas de caos: matar DataNodes/líder a mitad de subida/bajada, caída total del plano de control |
| **RNF3 Consistencia** | resolver el reto de consistencia de datos | Metadatos **linealizables** (Raft, lease + check-quorum); bloques **inmutables** y verificados por hash; *commit* atómico de archivos; semántica *close-to-open*; control optimista de versión; locks con fencing | pruebas de Raft (caos aleatorio con invariantes), de la máquina de estados (determinismo) e integración |
| **RNF4 Particionamiento** | cada archivo repartido entre nodos | Bloques de tamaño fijo; cada bloque en R nodos de ≥2 zonas; balance por bandas de utilización | `dfsha blocks <archivo>` muestra el mapa de réplicas; test de distribución |
| **RNF5 Rendimiento / concurrencia** | accesos concurrentes a archivos y a datos de un archivo | E/S paralela por bloque; tubería de replicación; *read-ahead*; asignación por lotes; *group commit* en el log Raft; lecturas de metadatos con *lease* | benchmarks de throughput y latencia de metadatos |
| **RNF6 Seguridad** | cifrado en comunicaciones y en almacenamiento; control de acceso; seguridad entre nodos | TLS 1.2+ en todo; **mTLS** con roles entre nodos; AES-256-GCM por bloque con **cifrado de sobre** (las DataNodes no almacenan las claves); tokens de capacidad sellados; scrypt; 2FA; throttling; auditoría | `tests/test_integration.py` (token binding, mTLS, throttling, 2FA, ACL…) |
| **RNF7 Transparencia** | acceso y localización, nunca estática | API tipo archivo; ubicación resuelta dinámicamente con el ControlNode en cada operación; *failover* entre réplicas y líderes invisible; reubicación automática de bloques en escritura | tests de caos |
| **RNF8 Portabilidad / despliegue** | nativo o Docker; IaaS (AWS Academy) | Una imagen Docker para todos los roles; `docker-compose.yml` de referencia; guía y scripts AWS | `docs/OPERACION.md`, `deploy/aws/` |

## 5. Decisiones de diseño (resumen)

1. **Basado en bloques con metadatos centralizados-replicados** (estilo HDFS NameNode + DataNodes),
   pero con el *NameNode* **alta-disponible por consenso** (Raft) en lugar de un respaldo manual.
2. **Bloques inmutables + commit atómico**: simplifica la consistencia (no hay réplicas divergentes
   de un bloque) y permite reemplazos y *copy-on-write* sin ventanas de lectura inconsistente.
3. **Plano de control y de datos separados**: el consenso solo transporta metadatos; los datos fluyen
   directamente cliente↔DataNode (el ControlNode nunca es cuello de botella de ancho de banda).
4. **Seguridad por capas**: cada salto de red cifrado y autenticado; cifrado en reposo con claves que
   no residen en el nodo de almacenamiento.
5. **Reparación continua**: vivacidad por *heartbeat*, informes de bloques, *scrubber* de integridad y
   re-replicación con periodo de gracia para no mover datos por fallos transitorios.

## 6. Límites explícitos (fuera de alcance)

* Membresía **estática** de ControlNodes (no hay cambio dinámico de configuración Raft).
* No hay montaje a nivel de SO (FUSE/NFS): la transparencia de acceso se logra mediante SDK/CLI/shell
  (el enunciado lo admite para sistemas de objetos/bloques con SDK propio).
* El cifrado en reposo protege contra robo de discos de DataNodes; **no** protege contra un
  compromiso del ControlNode (que custodia las claves envueltas). Ver `SEGURIDAD.md`.
* Un solo dominio administrativo (no federado): la Opción 2 (P2P / SuperPeers) no se implementa.
