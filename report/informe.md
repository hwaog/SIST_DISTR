{{include report/marco_teorico.md}}

# Descripción del servicio y problema abordado

{{include docs/ESPECIFICACION.md skip_h1}}

# Arquitectura del sistema y diagramas

{{include docs/ARQUITECTURA.md skip_h1}}

![Mapa de réplicas de un archivo de 8 bloques en 5 DataNodes y 2 zonas](report/figs/mapa_replicas.png)

# Especificación de protocolos y APIs

{{include docs/API.md skip_h1}}

# Algoritmos de particionamiento y distribución

{{include docs/ALGORITMOS.md skip_h1}}

# Replicación y consistencia de datos

{{include docs/CONSISTENCIA.md skip_h1}}

# Seguridad

![Seguridad por capas: TLS/mTLS, tokens de capacidad, cifrado en reposo y PKI](report/figs/seguridad.png)

{{include docs/SEGURIDAD.md skip_h1}}

# Entorno de ejecución: nativo y Docker

![Despliegue de referencia en AWS Academy](report/figs/aws.png)

{{include docs/OPERACION.md skip_h1}}

{{include docs/DESPLIEGUE_NUBE.md}}

# Pruebas y análisis de resultados

{{include docs/PRUEBAS.md skip_h1}}

{{include docs/RESULTADOS.md skip_h1}}

# Cumplimiento de los criterios de evaluación

| Criterio (peso) | Evidencia en este proyecto |
|---|---|
| **1. Definición del servicio y diseño arquitectónico (15 %)** | Requisitos funcionales RF1–RF5 y no funcionales RNF1–RNF8 con mecanismo y verificación (cap. «Descripción del servicio»); arquitectura Cliente/Servidor con servidor compuesto y distribuido, planos de control y datos, flujos de escritura/lectura/failover/reparación (cap. «Arquitectura») |
| **2. Especificaciones de comunicaciones (15 %)** | Tabla de enlaces (Cliente↔Control, Cliente↔Data, Control↔Control, Control↔Data, Data↔Data) con protocolo, autenticación y contenido; cada endpoint REST y mensaje Raft; formato de errores, idempotencia y tokens; justificación de REST frente a gRPC/MOM (cap. «Especificación de protocolos y APIs») |
| **3. Diseño para escalabilidad (15 %)** | Particionamiento en bloques, colocación con diversidad de zonas y balance, E/S paralela, tubería, asignación por lotes, balanceador al añadir nodos, lecturas relajadas; evidencia: benchmarks de escalado (con y sin tope por nodo), tamaño de bloque, paralelismo, clientes y metadatos; prueba de integración del balanceo |
| **4. Alta disponibilidad (15 %)** | Raft ×3 sin punto único de fallo; replicación ×3; re-replicación y reparación; failover transparente; evidencia: 175 pruebas de caos (SIGKILL de DataNodes y líder a mitad de transferencias, pérdida de quórum, caída total del plano de control, corrupción, disco borrado) y **100 % de disponibilidad medida** (14 580 lecturas y 4 137 escrituras sin fallos) durante un plan de fallos |
| **5. Replicación y consistencia de datos (15 %)** | Metadatos linealizables (Raft con lease/check-quorum/Pre-Vote), bloques inmutables con SHA-256 extremo a extremo, commit atómico, CAS, locks con fencing, idempotencia; evidencia: 175 pruebas de Raft con caos aleatorio (invariante: ningún commit confirmado se pierde), prueba de determinismo de la máquina de estados, pruebas de integración de concurrencia |
| **6. Seguridad (15 %)** | TLS en todos los enlaces, mTLS con roles, cifrado en reposo con sobre de claves, 2FA, API keys, scrypt, ACL, tokens sellados, throttling, auditoría; evidencia: pruebas que verifican cifrado en disco, rechazo de tokens ajenos/falsos, mTLS con certificados incorrectos, ACL entre usuarios, fuerza bruta y 2FA |
| **7. Video e informe final (10 %)** | Este informe y el video de demostración; repositorio documentado y reproducible (`README.md`, `uv sync`, `dfsha-cluster up`) |

# Limitaciones y trabajo futuro

| Limitación | Impacto | Mejora propuesta |
|---|---|---|
| Membresía de Raft estática | Cambiar la lista de ControlNodes exige reiniciar el grupo | Cambios de configuración de un servidor a la vez (tesis de Raft §4) con nodos *learner* |
| Un solo grupo Raft | Techo de metadatos por la RAM y el throughput de un líder (≈0,6 k escrituras/s con fsync durable y ≈2 k sin fsync, medidas con 32 clientes) | Particionar el namespace en varios grupos Raft (federación) |
| Cifrado en reposo con claves custodiadas por el ControlNode | Un ControlNode comprometido en ejecución ve los datos | KEK en un KMS externo (AWS KMS / Vault) o cifrado de extremo a extremo en el cliente |
| Sin integración con el SO (FUSE/NFS) | Transparencia de acceso por SDK/CLI/shell, no a nivel de sistema operativo | Puerta de enlace WebDAV/NFS o montaje FUSE sobre el SDK |
| Sin cuotas por usuario ni límites de tasa | Un usuario puede consumir toda la capacidad | Cuotas de bytes/archivos en la máquina de estados |
| Replicación simple ×R | 3× de almacenamiento | *Erasure coding* (p. ej. RS 6+3) para datos fríos |
| Rotación de `cluster.key` manual | — | Reenvolver las DEK en línea con versionado de KEK |

## Estado de verificación de los artefactos de despliegue

{{include docs/ESTADO_VERIFICACION.md}}

# Conclusiones

* Se diseñó e implementó DFSha, un DFS por bloques con un plano de control **sin punto único de
  fallo** (Raft propio, validado con pruebas de caos) y un plano de datos con **replicación en tubería**,
  reparación automática y **cifrado en reposo con sobres de claves**.
* La **separación de planos** y la **inmutabilidad de bloques** simplifican radicalmente la consistencia:
  los metadatos son linealizables y los datos se validan con un hash extremo a extremo; las operaciones
  complejas (reemplazo, anexado, actualización parcial) se reducen a "bloques nuevos + commit atómico".
* La **seguridad** se diseñó por capas (TLS/mTLS con roles, tokens de capacidad, AES-GCM, 2FA, ACL) y cada
  control tiene una prueba automática que intenta romperlo.
* Las mediciones muestran que el sistema **mantiene el servicio** (100 % de operaciones exitosas
  durante un plan de fallos que mata DataNodes, el líder y un seguidor), conmuta de líder con una
  mediana de 1,5 s y re-replica 256 MiB en ≈10 s. Todas las cifras de rendimiento se midieron en **una sola máquina**, por lo
  que son un límite inferior del comportamiento distribuido real y deben leerse con esa salvedad.
* Lección de ingeniería: las pruebas de integración y caos encontraron defectos que el análisis no
  había anticipado (sesgo de la colocación, `with open(...)` que confirmaba una escritura fallida,
  rechazos falsos en seguidores atrasados, `fsync` no durable en macOS); todos quedaron corregidos y con
  prueba de regresión.

# Referencias

1. S. Ghemawat, H. Gobioff, S.-T. Leung. *The Google File System*. SOSP 2003.
2. K. Shvachko, H. Kuang, S. Radia, R. Chansler. *The Hadoop Distributed File System*. IEEE MSST 2010.
3. D. Ongaro, J. Ousterhout. *In Search of an Understandable Consensus Algorithm (Raft)*. USENIX ATC 2014.
4. D. Ongaro. *Consensus: Bridging Theory and Practice* (tesis doctoral, Stanford, 2014) — Pre-Vote, lecturas con lease, cambios de membresía.
5. R. Sandberg et al. *Design and Implementation of the Sun Network Filesystem*. 1985.
6. J. Howard et al. *Scale and Performance in a Distributed File System* (AFS). ACM TOCS 1988.
7. M. Herlihy, J. Wing. *Linearizability: A Correctness Condition for Concurrent Objects*. ACM TOPLAS 1990.
8. S. Gilbert, N. Lynch. *Brewer's Conjecture and the Feasibility of Consistent, Available, Partition-Tolerant Web Services*. 2002.
9. M. Mitzenmacher. *The Power of Two Choices in Randomized Load Balancing*. IEEE TPDS 2001.
10. E. Rescorla. *The Transport Layer Security (TLS) Protocol Version 1.3*. RFC 8446, 2018.
11. M. Dworkin. *Recommendation for Block Cipher Modes of Operation: GCM and GMAC*. NIST SP 800-38D, 2007.
12. D. M'Raihi et al. *TOTP: Time-Based One-Time Password Algorithm*. RFC 6238, 2011.
13. C. Percival, S. Josefsson. *The scrypt Password-Based Key Derivation Function*. RFC 7914, 2016.
14. H. Krawczyk, P. Eronen. *HKDF*. RFC 5869, 2010.
15. Enunciado del Proyecto 1 — ST0263/SI3007 «DFSha», 2026-2.

# Anexo A. Reproducibilidad

```bash
git clone <repositorio> && cd SIST-DISTR
uv sync --extra dev --extra report
uv run dfsha-cluster up --control 3 --data 5 --zones 2
eval "$(uv run dfsha-cluster env)" && uv run dfsha login -u admin -p dfsha-admin-dev
uv run pytest                          # las 175 pruebas (≈6 min)
uv run python bench/bench.py all       # benchmarks -> bench/results/*.json
uv run python report/figs.py && uv run python report/build_report.py
```
