# Replicación y consistencia de datos

DFSha separa dos problemas con garantías distintas: la consistencia de los **metadatos**
(qué archivos existen, qué bloques los componen, dónde están) y la de los **datos** (el contenido
de los bloques).

## 1. Metadatos: consenso con Raft (CP)

Los N ControlNodes (3 por defecto) mantienen una **máquina de estados replicada** (`control/fsm.py`)
mediante **Raft** (`raft/core.py`, implementación propia verificada con pruebas de caos).

| Propiedad | Garantía |
|---|---|
| Escrituras de metadatos | **Linealizables**: pasan por el líder, se replican a una mayoría y se persisten (fsync) antes de confirmarse |
| Lecturas de metadatos (por defecto) | **Linealizables**: sólo las sirve el líder, con *lease* de líder basado en acks de mayoría (válido porque los seguidores rechazan votos mientras oyen al líder) y *fallback* a ronda de heartbeats |
| Lecturas relajadas (`--stale`) | Cualquier ControlNode; pueden ir atrasadas unos ms; ante cualquier error se reconfirma con el líder |
| Tolerancia a fallos | 3 nodos tolera 1 caída; 5 nodos tolera 2. Sin mayoría el servicio **rechaza** (Unavailable) en vez de arriesgar inconsistencia: es un sistema **CP** en terminología CAP |
| Reinicios | Término, voto y log se persisten antes de responder; el estado se reconstruye de snapshot + log |
| Particiones | Una minoría aislada no puede comprometer ni servir lecturas (check-quorum: el líder renuncia) y un nodo aislado no desestabiliza al líder al volver (Pre-Vote + *leader stickiness*) |

### Determinismo e idempotencia

* `apply(comando)` es **determinista**: el líder inyecta todo lo no determinista (hora, identificadores,
  claves, ubicaciones elegidas). Una prueba de *fuzzing* aplica 400 comandos aleatorios a dos réplicas
  independientes y exige estados idénticos byte a byte.
* Cada comando lleva un `req_id` (cabecera `X-Request-Id`). Si el líder cae tras comprometer pero antes
  de responder, el cliente reintenta con el mismo id y la máquina de estados devuelve **el mismo
  resultado sin volver a ejecutar** (tabla acotada incluida en los snapshots).
* El tiempo de la máquina de estados es el máximo de las marcas de tiempo vistas (monótono entre
  líderes); *leases* de locks y sesiones se evalúan con ese reloj, de forma determinista.

## 2. Datos: bloques inmutables

Cada archivo es una lista ordenada de **bloques inmutables** identificados por un UUID. Un bloque
nunca se modifica: modificar un archivo crea bloques nuevos. Consecuencias:

* **No hay réplicas divergentes** de un mismo bloque (no existe el problema de reconciliar escrituras
  concurrentes sobre el mismo bloque).
* La corrección de una réplica se decide solo por **su hash**: el cliente calcula SHA-256 del texto claro
  de cada bloque, el ControlNode lo guarda en el metadato y el cliente lo verifica al leer
  (**extremo a extremo**, independiente de qué réplica atendió).
* Reparar = copiar un blob íntegro desde otra réplica.

### Escritura: tubería con quórum de escritura

1. `begin` crea una **sesión de escritura** (invisible: el archivo aún no existe/no cambió).
2. `alloc` reserva bloques por lotes: el líder elige R DataNodes por bloque y replica la reserva.
3. El cliente envía cada bloque al primer nodo de la tubería; éste lo cifra, lo persiste y lo reenvía
   (ya cifrado) al siguiente, y así sucesivamente. La respuesta lista los nodos que **confirmaron**.
4. El bloque cuenta como escrito si ≥ **W** réplicas confirmaron (W = mayoría de R; configurable
   `all/majority/one`). Si no, el cliente pide **reubicación** excluyendo los nodos fallidos.
5. `commit` instala **atómicamente** el nuevo layout del archivo (todos los bloques o ninguno) y sube
   su `version`. Los bloques viejos pasan a recolección de basura con periodo de gracia.
6. Las réplicas que falten hasta R las completa el ControlNode en segundo plano.

Si el cliente muere a mitad de camino, la sesión expira (*lease*) y sus bloques se recolectan: nunca
queda un archivo a medias visible.

### Semántica visible para las aplicaciones

| Operación | Semántica |
|---|---|
| `open('wb')` + `close()` | Reemplazo **atómico** (S3-like). Último escritor gana, salvo `if_version` (CAS) |
| `open('ab')` / `open('r+b')` | Control **optimista**: el commit falla con `ECONFLICT` si otro confirmó antes; `r+b` hace *copy-on-write* de los bloques afectados |
| Lectura tras escritura | **Close-to-open**: un lector ve la última versión confirmada al abrir; no ve escrituras parciales |
| Lector abierto durante una reescritura | Sigue leyendo **su** versión (bloques inmutables) hasta el fin del periodo de gracia del GC (`ESTALE` después) |
| Locks | Compartido/exclusivo con *lease*, renovación automática y **fencing token** monótono. El servidor **hace cumplir** el lock a las escrituras (un escritor sin el lock recibe `ELOCKED`) |

## 3. Reparación y convergencia

| Mecanismo | Disparador | Acción |
|---|---|---|
| Vivacidad | sin *heartbeat* > `dn_dead_after` | el nodo se considera muerto; sus réplicas dejan de contar |
| Re-replicación | bloque con réplicas vivas < R, tras `repl_grace` | el líder ordena la copia DN→DN (verificada) y registra la nueva réplica por Raft |
| Sobre-replicación | réplicas vivas > R (p. ej. un nodo muerto que regresa) | se recortan donde hay menos espacio |
| Informe de bloques | arranque del DN y periódico | reconcilia: huérfanos se borran; réplicas perdidas (dos informes consecutivos) se descartan y se re-replican |
| *Scrubber* | periódico | verifica el hash de cola de cada blob **sin necesitar la clave**; corruptos van a cuarentena y se reportan |
| Lectura corrupta | fallo de autenticación AES-GCM o hash | el DN pone el blob en cuarentena y avisa; el cliente cambia de réplica |
| GC | borrado/reemplazo/sesión expirada | cola `pending_deletes` con periodo de gracia; el DN confirma y Raft la vacía |

## 4. Tabla de fallos y comportamiento

| Fallo | Efecto | Recuperación |
|---|---|---|
| Cae un DataNode | lecturas conmutan a otra réplica; escrituras saltan el nodo o se reubican | tras `repl_grace` se restablece R |
| Cae el líder Raft | pausa de metadatos de ≈1,5 s (mediana medida con temporizadores de 0,5–1 s; máx. 3,9 s; con los valores por defecto de 1–2 s cabe esperar el doble); el cliente reintenta | nuevo líder; sin pérdida de operaciones confirmadas |
| Caen 2 de 3 ControlNodes | no hay quórum: se rechazan metadatos (CP). Los **bloques ya ubicados siguen siendo legibles** si el cliente tiene tokens vigentes | al volver un nodo se reanuda |
| Caída total del plano de control | indisponible | al reiniciar, estado íntegro desde disco |
| Partición | la minoría no sirve | al sanar, el log de la minoría se descarta |
| Bit rot en un DataNode | detectado por GCM/hash; réplica descartada | re-replicación desde réplica sana |
| Disco de DN borrado | informe de bloques detecta la ausencia | re-replicación |
| Cliente muere a mitad de subida | sesión huérfana | expira y se recolecta; el archivo nunca fue visible |
