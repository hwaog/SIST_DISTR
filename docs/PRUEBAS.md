# Estrategia de pruebas y resultados

## 1. Estrategia

Pirámide de pruebas con énfasis en la corrección bajo fallos, porque ése es el riesgo principal de un
sistema distribuido:

| Nivel | Archivos | Qué prueba | Cómo |
|---|---|---|---|
| Unitarias de seguridad | `tests/test_security.py` | AES-GCM por bloque, AAD, bit-rot sin clave, envoltura de claves, tokens sellados (expiración, manipulación), scrypt, API keys, **TOTP contra los vectores del RFC 6238**, JWT | llamadas directas |
| Consenso (Raft) | `tests/test_raft.py` | elección única, replicación y orden total, caída del líder, partición minoritaria, Pre-Vote, lease de lectura, snapshots/`InstallSnapshot`, reinicio desde disco, 5 nodos tolerando 2 fallos, **caos aleatorio con invariantes** | red en memoria con particiones, caídas y reinicios controlados |
| Máquina de estados | `tests/test_fsm.py` | permisos POSIX/ACL, recorrido de rutas, escrituras atómicas, modos replace/append/update, CAS, sesiones y expiración, GC, locks y fencing, idempotencia, **determinismo (fuzz de 400 comandos sobre dos réplicas)** | se aplica el mismo log a réplicas independientes |
| Colocación | `tests/test_placement.py` | nodos distintos, diversidad de zonas, **ausencia de sesgo**, evitar nodos llenos | estadísticas sobre miles de decisiones |
| CLI | `tests/test_cli.py` | login, `cd` persistente, put/get/cat/cp/mv/rm, `put -r`, `blocks`, `verify`, chmod/ACL, locks, `nodes`, errores y códigos de salida | `CliRunner` contra el cluster real |
| Integración | `tests/test_integration.py` | cluster **real** (3 ControlNodes + 4 DataNodes, procesos separados, TLS/mTLS): idas y vueltas de 0 B a 80 MiB, distribución y zonas, **cifrado en disco**, API tipo archivo, FS, permisos y ACL, tokens, mTLS, 2FA, API keys, throttling, concurrencia, locks | `LocalCluster` |
| Caos / HA | `tests/test_chaos.py` | SIGKILL de DataNodes y líder **durante** subidas y bajadas, pérdida de quórum, caída total del plano de control (con fsync real), corrupción de bloques, scrubber, disco borrado, decommission, GC | un cluster nuevo por prueba |
| Escala | `tests/test_scale.py` | añadir un DataNode en caliente y observar el balanceo; `InstallSnapshot` real sobre HTTPS; límite de cuerpo | cluster real |
| Despliegue (Docker) | `tests/test_compose.py`, `tests/test_docker_verify.py` | el `docker-compose.yml` coincide con su generador; **cada nodo monta sólo sus claves** (`cluster.key` sólo en ControlNodes, `ca.key` sólo en `pki-init`); endurecimiento y puertos; `init-pki.sh` (diseño, idempotencia, cadena de certificados); la lógica del verificador de Docker | análisis del compose y un `FakeDocker` |
| Despliegue (AWS) | `tests/test_aws_scripts.py`, `tests/test_cloud.py`, `tests/test_demo_cloud.py` | los scripts `deploy/aws/*` con `aws`/`ssh`/`rsync` falsos (órdenes y argumentos exactos); el **validador de 10 pasos** y los benchmarks de nube ejecutados contra un cluster local que hace de «remoto»; la demo en la nube | ejecutables falsos en el `PATH` + `LocalCluster` |

Principios: (1) **sin simulaciones donde se pueda usar lo real** (procesos, sockets, TLS, SIGKILL);
(2) cada defecto encontrado deja una **prueba de regresión**; (3) las pruebas de caos verifican
**invariantes**, no sólo "no hubo excepción": nada confirmado se pierde, ninguna entrada se aplica dos
veces, todas las réplicas convergen al mismo estado, el contenido leído coincide byte a byte (SHA-256).

## 2. Cómo ejecutarlas

```bash
uv run pytest -m "not integration"        # unitarias: segundos
uv run pytest tests/test_integration.py   # ≈ 1,5 min
uv run pytest tests/test_chaos.py         # ≈ 2 min
uv run pytest tests/test_scale.py         # ≈ 1 min
uv run pytest                             # todo (≈ 4 min)
```

### Verificaciones adicionales (no son parte de `pytest`)

| Verificación | Cómo | Resultado |
|---|---|---|
| Configuración del `docker-compose.yml` | `scripts/compose_dryrun.py` (ver `OPERACION.md`) | **OK**: cluster formado sólo con las variables del compose, **aislamiento de claves por servicio**, put/get, caída del líder, healthcheck |
| Docker real (`docker compose up`) | — | **No ejecutado**: Docker no estaba disponible en el entorno de desarrollo |
| Despliegue en AWS Academy | `dfsha-cloud validate` (10 pasos con fallos reales) y benchmarks desde una VM del mismo VPC | **Ejecutado**: 10/10 pasos superados (ver capítulo «Despliegue en la nube») |
| Análisis estático | `ruff check` | sin avisos |

## 3. Defectos hallados por las pruebas (y corregidos)

| # | Defecto | Cómo se detectó | Corrección | Regresión |
|---|---|---|---|---|
| 1 | La colocación («el menos lleno de dos») **nunca** elegía al nodo marginalmente más lleno: un DataNode quedaba sin datos | `test_blocks_are_partitioned…` | bandas de utilización del 5 % con desempate aleatorio | `test_placement.py` |
| 2 | `with open(p,'wb')` **confirmaba** el archivo parcial si el bloque `with` terminaba en excepción (`BufferedWriter.__exit__` no llegaba al `abort`) | `test_failed_writer_aborts…` | `_SafeBufferedWriter/_SafeText` abortan ante excepción; el escritor descarta datos tras `abort` | mismo test |
| 3 | Un seguidor con estado atrasado unos ms **rechazaba una API key recién creada** (falso `EAUTH`) | `test_api_keys` (intermitente) | los accesos no relajados se redirigen al líder **antes** de autenticar; las lecturas `stale` que fallan se reconfirman con el líder | mismo test |
| 4 | El balanceo/recorte de sobre-replicación podía **romper la diversidad de zonas** | análisis tras el test de balanceo (`zonas=2` con una zona de un solo nodo) | recorte y balanceo consultan el nº de zonas resultante | `test_scale.py` |
| 5 | Un cliente podía **declarar réplicas** en DataNodes que no se le asignaron | revisión de seguridad | `commit_write` exige `reps ⊆ asignados` | `test_fsm.py` |
| 6 | Las ubicaciones y tokens de bloque podían servirse desde estado atrasado (revocaciones tardías) | revisión de seguridad | `locations` siempre linealizable | — |
| 7 | El DataNode leía el cuerpo completo antes de comprobar el tamaño máximo del token | revisión de seguridad | `precheck_write` valida token y `Content-Length` antes de leer | `test_datanode_rejects_oversized_put…` |
| 8 | En macOS `os.fsync` **no vacía la caché del disco**: la durabilidad declarada no era real | medición (0,05 ms vs 4,3 ms) | `fsync_fd` usa `F_FULLFSYNC` en Darwin | micro-medición |
| 9 | El apagado de los nodos tardaba hasta 10 s por conexiones *keep-alive* | observando los benchmarks | `timeout_graceful_shutdown=3` y apagado paralelo | — |
| 10 | El desempate de colocación por **carga instantánea** era estricto: un DataNode con una transferencia más en el último *heartbeat* quedaba excluido de todo un lote (fallo intermitente en ≈1 de 3 ejecuciones) | repitiendo 8 veces las pruebas de integración | la carga se discretiza en escalones de 8 transferencias (empate ⇒ azar) | `test_small_load_differences_do_not_starve_a_node` |
| 11 | La re-replicación tras la caída de un DataNode **se saltaba la gracia** `repl_grace` hasta ≈1 s: `dead_since()` leía la marca que anota el bucle de vivacidad (que corre cada 1 s) y, mientras no estaba anotada, caía al instante de arranque del ControlNode: la espera se daba por cumplida y se re-replicaba de inmediato por un fallo que podía ser transitorio | el validador de nube falló de forma intermitente en el paso 8 (`repaired_s` demasiado corto); se aisló con trazas de la reparación | `dead_since = last_seen + dn_dead_after` (derivado, sin depender del bucle) y registros del monitor (reparación, espera de gracia, sin destinos) | `test_repair_waits_for_the_grace_period_after_a_datanode_is_declared_dead` (falla 2 de 6 veces con el código antiguo) |
| 12 | `docker-compose.yml` montaba **un único volumen** con `ca.key`, `cluster.key` y las claves privadas de todos los nodos en cada contenedor (un DataNode comprometido lo veía todo) | revisión de seguridad del propio compose | volúmenes por nodo generados por `docker/gen_compose.py`; `ca.key` sólo en `pki-init` | `tests/test_compose.py` |
| 13 | `test_adding_datanode_triggers_rebalancing` era intermitente: comprobaba el reparto antes de que el GC físico (`gc_grace` 120 s) borrara las réplicas sobrantes | ejecución repetida | el test usa `--gc-grace 1` y espera la convergencia con `eventually` | mismo test (3/3) |
| 14 | Al leer un archivo **sin permiso** (o inexistente) el cliente mostraba además «Exception ignored … no attribute '_ex'»: el lector de archivos fallaba en el constructor y luego no podía cerrarse | prueba de permisos en un segundo computador (Windows) | los atributos del lector se inicializan antes de consultar al ControlNode y `close()` tolera su ausencia | `test_reader_that_failed_to_open_can_still_be_closed` |

## 4. Resultados

Los resultados de la última ejecución completa se registran en la sección siguiente («Benchmarks») y en
la salida de `pytest`. Resumen:

| Conjunto | Pruebas | Resultado |
|---|---|---|
| Seguridad (`test_security.py`) | 13 | todas pasan |
| Raft (`test_raft.py`) | 12 | todas pasan (el caos aleatorio se repitió 5 veces en ejecuciones independientes sin fallos) |
| Máquina de estados (`test_fsm.py`) | 25 | todas pasan |
| Colocación (`test_placement.py`) | 5 | todas pasan |
| Integración (`test_integration.py`) | 39 | todas pasan |
| CLI (`test_cli.py`) | 7 | todas pasan |
| Caos / HA (`test_chaos.py`) | 16 | todas pasan |
| Escala (`test_scale.py`) | 3 | todas pasan |
| Docker: compose y verificador (`test_compose.py`, `test_docker_verify.py`) | 17 | todas pasan |
| AWS: scripts, validador y demo en la nube (`test_aws_scripts.py`, `test_cloud.py`, `test_demo_cloud.py`) | 26 | todas pasan |
| Utilidades del repositorio (`test_report_cloud.py`, `test_check_entrega.py`, `test_env_examples.py`) | 13 | todas pasan |
| **Total** | **175** | **175 pasan en ≈6 min** (macOS arm64, Python 3.12, 10 CPU) |

Cada prueba de integración y de caos levanta procesos reales (hasta 3 ControlNodes + 4-5 DataNodes) con TLS y
mTLS, sin simulaciones. El análisis estático (`ruff`) no reporta avisos.
