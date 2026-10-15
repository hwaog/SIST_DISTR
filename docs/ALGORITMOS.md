# Algoritmos de particionamiento y distribución

## 1. Particionamiento de archivos en bloques

Un archivo de `size` bytes con tamaño de bloque `bs` se parte en `n = ⌈size / bs⌉` bloques consecutivos;
el bloque `i` cubre los bytes `[i·bs, min((i+1)·bs, size))`. Cada bloque tiene un identificador aleatorio
de 128 bit, independiente del contenido y del offset.

```
archivo (22 MiB, bs = 4 MiB)
 ├─ bloque 0 [0, 4 MiB)       id=5f2c…  sha256=…   réplicas {dn4, dn5, dn2}
 ├─ bloque 1 [4, 8 MiB)       id=0e19…  sha256=…   réplicas {dn2, dn5, dn3}
 ├─ …
 └─ bloque 5 [20, 22 MiB)     id=b496…  (2 MiB)    réplicas {dn2, dn1, dn4}
```

* El **tamaño de bloque** es una propiedad del archivo (64 KiB – 256 MiB; 4 MiB por defecto). Bloques
  grandes reducen metadatos y viajes al ControlNode; bloques pequeños mejoran el paralelismo en
  archivos pequeños (ver benchmark de tamaño de bloque).
* El **layout** del archivo es la lista ordenada de ids de bloque. Cambiarlo es una operación atómica
  (commit). Los bloques nunca cambian.
* Como los ids son aleatorios, **no hay hot spots por contenido**: dos archivos populares no comparten
  nodo salvo por azar, y un archivo grande se reparte entre todos los nodos.

## 2. Colocación de réplicas

Para cada bloque se elige un conjunto de `R` DataNodes **vivos, activos y con espacio**:

```
choose(candidatos, R, excluidos, zona_cliente):
    pool ← candidatos \ excluidos (con espacio libre ≥ bs)
    elegidos ← []
    mientras |elegidos| < R y pool ≠ ∅:
        sub ← pool
        si elegidos = ∅ y zona_cliente existe en pool:   sub ← nodos de zona_cliente       # localidad
        sino si |zonas(elegidos)| < 2 y hay ≥2 zonas:    sub ← nodos de zonas aún no usadas  # diversidad
        a, b ← dos nodos al azar de sub
        elegido ← (a si banda(a) < banda(b)) ; (b si banda(b) < banda(a)) ; (al azar si empatan)
        elegidos.añadir(elegido) ; pool.quitar(elegido)
    devolver elegidos
banda(n) = (⌊ utilización(n) / 5 % ⌋ , ⌊ transferencias_en_curso(n) / 8 ⌋)   # ambas discretizadas: «casi igual» empata
```

Propiedades:

1. **Tolerancia a la pérdida de una zona**: con ≥2 zonas, cada bloque tiene réplicas en ≥2.
2. **Balance sin sesgo**: la *potencia de dos opciones* (Mitzenmacher) reduce el desbalance máximo de
   O(log n / log log n) a O(log log n) con una sola comparación; las **bandas** del 5 % hacen que nodos
   casi igual de llenos *empaten* y reciban datos por igual (sin ellas, una diferencia del 0,01 % condenaba
   al nodo marginalmente más lleno a no recibir nada: defecto hallado y corregido con una prueba de
   integración).
3. **Costo** O(R) por bloque tras filtrar candidatos O(N); la asignación se hace por lotes de hasta 512
   bloques por consenso.
4. Un contador local de bytes asignados evita que una ráfaga apile todos los bloques en el mismo nodo.

## 3. Escritura: tubería de replicación y quórum

El cliente envía **una** copia a la cabeza de la tubería `[A, B, C]`; cada nodo cifra (sólo el primero),
persiste y reenvía el blob al siguiente:

* Ancho de banda de subida del cliente: **U** (con abanico directo sería U/R). Cada DataNode reenvía a lo
  sumo una copia, de modo que el tráfico se reparte en lugar de concentrarse en el cliente.
* **Quórum de escritura** `W = ⌊R/2⌋ + 1` por defecto: el bloque cuenta como escrito cuando ≥ W nodos
  confirmaron. Con `R = 3, W = 2` se tolera la caída de un nodo *durante* la escritura sin reintentos.
* **Fallos en la tubería**: si el nodo `k+1` no responde, `k` salta al siguiente; si la cabeza no
  responde, el cliente entra por la posición siguiente; si `|acks| < W`, se pide una **reubicación**
  excluyendo los nodos fallidos (hasta 5 intentos).
* El resto de réplicas (hasta `R`) lo completa el líder en segundo plano: la escritura **no espera** a las
  copias tardías.

Durabilidad aproximada: si cada nodo falla independientemente con probabilidad `p` en la ventana de
reparación `T`, un bloque se pierde sólo si los `R` nodos que lo contienen fallan en esa ventana: `p^R`.
Con `R = 3` y `p = 10⁻²` por ventana resulta ≈ 10⁻⁶; la **re-replicación automática reinicia la
cuenta** cada vez que se repara, por lo que `T` es el tiempo de detección (≈ `dn_dead_after`) + gracia +
copia, no la vida del dato.

## 4. Lectura

1. El ControlNode devuelve por bloque las réplicas **vivas primero**, las de la zona del cliente primero y
   en orden aleatorio entre iguales (reparte la carga de lectura entre las `R` réplicas).
2. El cliente descarga hasta `workers` bloques en paralelo (`get`) o con *read-ahead* de 3 bloques
   (`open`); verifica `SHA-256(bloque) = sha` del metadato.
3. Ante error de conexión, token vencido, corrupción o ausencia, pasa a la siguiente réplica; si agota
   todas, **refresca** ubicaciones (puede haber re-replicación en curso) y repite (3 rondas).

## 5. Detección de fallos, re-replicación y balanceo

```
cada repl_interval en el líder (y warm-up completado):
    para cada bloque con réplicas_vivas < R:
        si algún poseedor está caído hace menos de repl_grace:   continuar      # fallo posiblemente transitorio
        destino ← choose(candidatos, R − |vivas|, excluidos = poseedores ∪ en_curso)
        ordenar(destino, "copiar bloque desde {fuentes vivas}")                 # DN→DN, verificado
        al confirmar: Raft.replica_update(+ réplica)
    para cada bloque con réplicas_vivas > R:
        quitar las de nodos más llenos, sin bajar de 2 zonas
cada balance_interval:
    si no hay bloques sub/sobre-replicados:
        hi, lo ← nodo más/menos usado
        si uso(hi) − uso(lo) > max(2·bs, 20 % · uso_medio):
            copiar hasta 8 bloques hi → lo (respetando zonas)    # el recorte retirará la réplica de hi
```

El *warm-up* tras un cambio de líder evita confundir «aún no recibí heartbeats» con «todos murieron».
La decisión de borrar físicamente una réplica sólo se toma cuando el bloque sigue teniendo ≥1 réplica
confirmada o cuando el propio DataNode informó de su pérdida.

## 6. Consenso (Raft) y lecturas

* **Elección**: temporizadores aleatorios U[Tmin, Tmax] (1–2 s por defecto; 0,5–1 s en pruebas). La probabilidad
  de empate se reduce con la aleatoriedad y con Pre-Vote; el *failover* medido tiene mediana de 1,5 s y máximo de 3,9 s (ver
  `RESULTADOS.md`).
* **Pre-Vote + leader stickiness**: un nodo sólo inicia una elección real si una mayoría diría "sí";
  los seguidores ignoran solicitudes de voto mientras oyen al líder. Un nodo aislado no infla el término.
* **Lease de lectura**: el líder atiende lecturas locales mientras la mayoría haya confirmado su
  liderazgo hace menos de `0,6 · Tmin`. Es seguro porque ningún seguidor votará por otro candidato antes
  de `Tmin` desde su último contacto con el líder (premisa: deriva de reloj acotada). Sin lease, una ronda de
  heartbeats (ReadIndex) confirma el liderazgo.
* **Group commit**: las propuestas concurrentes se agrupan en un solo `append` + `fsync` y una ronda de
  replicación; el rendimiento de escritura de metadatos crece con la concurrencia.
* **Snapshots**: cada `snapshot_threshold` entradas aplicadas se serializa la máquina de estados y se
  compacta el log; los seguidores muy atrasados reciben `InstallSnapshot`.

## 7. Control de concurrencia en archivos

| Mecanismo | Uso | Garantía |
|---|---|---|
| Commit atómico de layout | toda escritura | todo o nada; lectores ven la versión anterior o la nueva |
| Versión + `if_version` (CAS) | actualizaciones concurrentes | el segundo escritor falla con `ECONFLICT` en vez de pisar |
| `append`/`update` con versión base | `ab`, `r+b` | detecta cualquier commit intercalado |
| Locks compartido/exclusivo | exclusión mutua cooperativa y obligatoria | *lease* con renovación; el servidor rechaza escritores sin el lock |
| *Fencing token* | cada adquisición incrementa un contador global | un cliente "zombi" (pausado, lease vencido) se identifica por su token menor |
