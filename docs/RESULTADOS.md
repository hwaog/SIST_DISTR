# Benchmarks y análisis de resultados

## Entorno y metodología

* Máquina: arm64 · 10 CPU · macOS-26.6.2-arm64-arm-64bit · Python 3.12.13. **Los ControlNodes, DataNodes y clientes comparten esta única máquina** (loopback, un solo disco).
* Cada nodo es un proceso independiente con TLS/mTLS reales. Datos aleatorios incompresibles. Cada medición se repite 3 veces (media ± desviación estándar; 2 veces en los experimentos con tope). `fsync` desactivado salvo donde se indica.
* Reproducible: `uv run python bench/bench.py all` (resultados crudos en `bench/results/*.json`).
* **Salvedad metodológica**: en una sola máquina el cuello de botella es la CPU compartida (TLS, SHA-256, AES-GCM, Python), no la red ni los discos. Por eso se incluye un experimento con un **tope de ancho de banda por DataNode** (40 MiB/s, como el de una VM con red/disco propios) que sí permite observar el escalado horizontal. Las cifras absolutas deben leerse como un orden de magnitud, no como rendimiento de producción.

## Costo de la criptografía por bloque (microbenchmark, 1 hilo, bloque de 4 MiB)

| Operación | MiB/s | ms por bloque de 4 MiB |
|---|---|---|
| AES-256-GCM cifrar | 2 136 | 1,9 |
| AES-256-GCM descifrar | 2 217 | 1,8 |
| SHA-256 | 3 200 | 1,3 |

El cifrado en reposo y la verificación de integridad cuestan ≈3,1 ms por bloque de 4 MiB (hardware AES del procesador): **no son el cuello de botella**; el costo dominante es TLS + HTTP + copias de memoria en Python.

## Memoria de los metadatos y velocidad de la máquina de estados

| Concepto | Valor medido |
|---|---|
| RAM por archivo (inodo + permisos + DEK envuelta) | ≈0,81 KiB |
| RAM por bloque (id, longitud, SHA-256, 3 réplicas) | ≈0,46 KiB |
| Comandos aplicados por la máquina de estados (un hilo, sin red) | ≈27 764 /s |

Con bloques de 4 MiB el metadato ocupa ≈0,011 % del dato almacenado: **1 PiB** de datos son ≈118 GiB de RAM en el ControlNode; con bloques de 64 MiB (como HDFS) bajan a ≈7 GiB. Es el límite de escala del plano de control (un único grupo Raft con metadatos en memoria) y la razón de que el tamaño de bloque sea configurable por archivo. La máquina de estados no es el cuello de botella de las operaciones de metadatos: aplica ≈27 764 comandos/s frente a los ≈1 954 /s que se obtienen de extremo a extremo (HTTPS + Raft).

## Escalado del ancho de banda con el número de DataNodes

### a) Sin tope: la CPU compartida es el límite (archivo de 256 MiB, 8 transferencias paralelas)

| Replicación | DataNodes | Subida MiB/s | Bajada MiB/s |
|---|---|---|---|
| ×1 | 1 | 703 ± 20 | 718 ± 78 |
| ×1 | 2 | 780 ± 110 | 724 ± 25 |
| ×1 | 3 | 1087 ± 68 | 715 ± 13 |
| ×1 | 4 | 1029 ± 107 | 586 ± 61 |
| ×1 | 6 | 943 ± 79 | 598 ± 32 |
| ×3 | 3 | 342 ± 16 | 688 ± 42 |
| ×3 | 4 | 353 ± 32 | 648 ± 39 |
| ×3 | 6 | 388 ± 8 | 617 ± 71 |

Con un único DataNode y replicación ×1 ya se alcanzan ≈703 MiB/s: el nodo no es el límite, lo es la máquina. Con ×3 la subida baja a ≈342 MiB/s porque cada byte se escribe y se transmite tres veces por la misma CPU y el mismo disco; la bajada (≈688 MiB/s) apenas cambia porque se lee una sola réplica.

![Throughput sin tope por nodo](report/figs/bench_escalado.png)

### b) Con tope de 40 MiB/s por DataNode (emula red/disco por nodo): escalado casi lineal

| Replicación | DataNodes | Subida MiB/s | Techo teórico | Eficiencia | Bajada MiB/s |
|---|---|---|---|---|---|
| ×1 | 1 | 40 | 40 | 101 % | 40 |
| ×1 | 2 | 57 | 80 | 72 % | 56 |
| ×1 | 3 | 87 | 120 | 73 % | 84 |
| ×1 | 4 | 100 | 160 | 63 % | 96 |
| ×1 | 6 | 153 | 240 | 64 % | 144 |
| ×1 | 8 | 169 | 320 | 53 % | 159 |
| ×3 | 3 | 38 | 40 | 95 % | 105 |
| ×3 | 4 | 43 | 53 | 81 % | 109 |
| ×3 | 6 | 55 | 80 | 69 % | 178 |
| ×3 | 8 | 75 | 107 | 70 % | 211 |

Con ×1, pasar de 1 a 8 DataNodes multiplica la subida por **4,2** (40 → 169 MiB/s) y la bajada de 40 a 159 MiB/s: al repartir los bloques entre más nodos, el ancho de banda agregado crece. Con ×3 el techo teórico de subida es `tope·N/3` porque cada bloque ocupa tres nodos; la eficiencia (≈70-95 %) la reduce el desbalance estadístico de una colocación aleatoria con pocos bloques por nodo y la sobrecarga fija de TLS/sesión. La bajada con ×3 escala igualmente (de 105 a 211 MiB/s), pues las lecturas se reparten entre réplicas.

![Throughput con tope de 40 MiB/s por DataNode](report/figs/bench_escalado_tope.png)

## Tamaño de bloque (256 MiB, 4 DataNodes, ×3, 8 hilos)

| Bloque | Bloques por archivo | Subida MiB/s | Bajada MiB/s |
|---|---|---|---|
| 1MiB | 256 | 266 ± 16 | 567 ± 12 |
| 2MiB | 128 | 336 ± 20 | 642 ± 18 |
| 4MiB | 64 | 366 ± 17 | 683 ± 25 |
| 8MiB | 32 | 400 ± 31 | 654 ± 32 |
| 16MiB | 16 | 323 ± 18 | 567 ± 69 |
| 32MiB | 8 | 198 ± 49 | 292 ± 61 |

Bloques de 1 MiB pagan demasiada sobrecarga por petición; bloques de 32 MiB dejan sólo 8 bloques para 8 hilos (sin solape) y exigen mucha memoria por transferencia. La subida es máxima con 8MiB; **4 MiB** (valor por defecto) logra la mejor bajada y una subida a ≈91 % del máximo, con metadatos moderados (≈0,5 KiB de RAM por bloque de 4 MiB, ver sección anterior).

![Tamaño de bloque](report/figs/bench_bloque.png)

## Paralelismo por bloque (256 MiB, 4 DataNodes, ×3)

| Hilos | Subida MiB/s | Bajada MiB/s |
|---|---|---|
| 1 | 141 ± 4 | 394 ± 16 |
| 2 | 229 ± 5 | 679 ± 21 |
| 4 | 322 ± 19 | 789 ± 16 |
| 8 | 380 ± 29 | 730 ± 8 |
| 16 | 420 ± 20 | 718 ± 7 |
| 32 | 299 ± 13 | 589 ± 12 |

Pasar de 1 a 8 hilos acelera la subida **2,7×** y la bajada **1,9×**; más allá de 16 la contención de CPU hace que el rendimiento caiga (32 hilos). El valor por defecto es 8.

![Paralelismo](report/figs/bench_workers.png)

## Clientes concurrentes (6 DataNodes, ×3; cada cliente sube y baja 64 MiB)

| Clientes | Throughput agregado MiB/s | Por cliente MiB/s | Tiempo total s |
|---|---|---|---|
| 1 | 357 | 357 | 0,4 |
| 2 | 413 | 207 | 0,6 |
| 4 | 464 | 116 | 1,1 |
| 8 | 542 | 68 | 1,9 |
| 16 | 483 | 30 | 4,2 |

El throughput agregado crece de 357 a 542 MiB/s al pasar de 1 a 8 clientes y se estabiliza (≈483 MiB/s con 16): se satura la CPU de la única máquina que ejecuta clientes *y* servidores; en un despliegue real los clientes aportan su propia CPU y red. Todas las operaciones fueron correctas (contenido verificado byte a byte).

![Clientes concurrentes](report/figs/bench_clientes.png)

## Operaciones de metadatos (3 ControlNodes)

| fsync | Clientes | stat linealizable ops/s (p99) | stat relajado ops/s | mkdir (escritura Raft) ops/s (p99) |
|---|---|---|---|---|
| off | 1 | 1861 (1,2 ms) | 1412 | 412 (4 ms) |
| off | 8 | 3579 (3,5 ms) | 3974 | 1101 (22 ms) |
| off | 32 | 3524 (24,7 ms) | 3959 | 1954 (47 ms) |
| on | 1 | 1772 (1,2 ms) | 1597 | 71 (18 ms) |
| on | 8 | 3025 (5,0 ms) | 3692 | 189 (69 ms) |
| on | 32 | 3260 (24,9 ms) | 3996 | 604 (84 ms) |

* Las **lecturas linealizables** las sirve el líder con *lease* (sin RPC): ≈3579 ops/s con latencia p99 de pocos ms.
* Con `fsync` **durable** (en macOS, `F_FULLFSYNC`, ≈4 ms por llamada) una escritura aislada tarda ≈14 ms (71 ops/s), pero el **group commit** agrupa las propuestas concurrentes en un único `fsync` y una única ronda de replicación: con 32 clientes se alcanzan **604 ops/s** (8,5× más). Sin fsync, 1954 ops/s.
* Las lecturas relajadas (cualquier ControlNode) no mejoran mucho en esta máquina porque el cliente y los tres ControlNodes compiten por la misma CPU; el diseño permite repartir la carga de lectura entre las réplicas.

![Operaciones de metadatos](report/figs/bench_metadatos.png)

## Disponibilidad y recuperación ante fallos

* **Conmutación de líder** (SIGKILL al líder, 5 repeticiones; tiempo hasta que una escritura vuelve a tener éxito): 1,53, 3,14, 3,94, 1,54, 1,54 s → mediana **1,54 s**, máximo 3,94 s (temporizadores de elección de 0,5–1,0 s en el cluster de pruebas).
* **Re-replicación** (256 MiB, SIGKILL a un DataNode): el cluster detecta la subreplicación a los 5,4 s y recupera las 3 réplicas vivas de todos los bloques a los **9,8 s** (incluye `dn_dead_after`=6 s y `repl_grace`=3 s).
* **Carga continua durante fallos** (72 s, 3 clientes leyendo y 3 escribiendo 2 MiB sin pausa) con este plan: 8s kill dn1; 16s start dn1; 24s kill leader c2; 34s start c2; 40s kill dn2; 48s kill follower c1; 58s start all. Resultado: **14580 lecturas y 4137 escrituras, 0 fallos → disponibilidad 100,00 % / 100,00 %**. Latencia de lectura p50 14 ms, p99 28 ms, máx. 2,4 s; escritura p50 47 ms, p99 99 ms, máx. 2,4 s. Los máximos corresponden a las operaciones en vuelo cuando cae el líder (reintentos transparentes del cliente).

![Disponibilidad durante fallos](report/figs/bench_disponibilidad.png)

## Conclusiones de los benchmarks

1. El **diseño escala horizontalmente**: al repartir bloques entre más DataNodes el ancho de banda agregado crece casi linealmente cuando cada nodo tiene capacidad propia (experimento con tope), tanto en subida como en bajada.
2. La **criptografía no es un cuello de botella** (≈3 ms por bloque de 4 MiB en un solo hilo).
3. El **paralelismo por bloque** (8 hilos) y la **tubería de replicación** son las palancas de rendimiento; el *group commit* lo es para los metadatos.
4. El **servicio no se interrumpe** ante la caída de DataNodes, del líder o de seguidores (uno a la vez, f=1): los reintentos transparentes del cliente convierten los fallos en picos de latencia, no en errores.
5. Límite de la medición: una sola máquina; las cifras absolutas son un límite inferior del comportamiento distribuido real.
