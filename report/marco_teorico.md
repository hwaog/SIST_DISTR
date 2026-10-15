# Objetivo y marco teórico

## Objetivo general

Diseñar e implementar un sistema de archivos distribuido (DFS) que permita almacenar y acceder a
archivos grandes de forma distribuida en múltiples nodos, considerando alta disponibilidad,
rendimiento, transparencia y seguridad, y en el que tanto la escritura como la lectura se reparten
entre varios nodos.

## Objetivos específicos

1. Definir formalmente el servicio (requisitos funcionales y no funcionales) para la **Opción 1**:
   arquitectura Cliente/Servidor con un servidor compuesto y distribuido (S2S).
2. Implementar un plano de datos basado en **bloques replicados** y un plano de control **sin punto
   único de fallo**.
3. Resolver el problema de **consistencia** de los datos replicados con garantías explícitas y verificables.
4. Garantizar **seguridad** en comunicaciones, almacenamiento, acceso y entre nodos.
5. Demostrar con pruebas automáticas y mediciones la **escalabilidad**, la **disponibilidad** y el
   **rendimiento** del sistema, y desplegarlo de forma nativa, en Docker y en IaaS.

## Marco teórico

### Sistemas de archivos distribuidos

Un DFS permite compartir y acceder de forma concurrente a archivos almacenados en distintos nodos.
**NFS** (Sun, 1985) expone un sistema de archivos remoto al sistema operativo del cliente mediante RPC y
ofrece transparencia de acceso total con semántica *close-to-open*; **AFS** (Andrew, CMU) añade
caché de archivos completos con *callbacks*; **SMB/CIFS** es el equivalente del mundo Windows. Estos
sistemas se integran con el sistema de archivos del SO (**basados en bloques/POSIX**), pero su escalabilidad
está limitada por el servidor que mantiene el estado.

**GFS** (Google, 2003) y **HDFS** (Hadoop, 2010) resuelven la escala con una arquitectura *maestro–trabajadores*:
un **NameNode/master** guarda los metadatos (espacio de nombres y ubicación de bloques) y los
**DataNodes/chunkservers** almacenan bloques grandes replicados; los clientes consultan al maestro
y transfieren los datos directamente con los nodos de almacenamiento. Optimizan archivos grandes de
escritura única y lectura múltiple (*WORM*), replican en tubería y usan conocimiento de racks para
colocar réplicas. Su talón de Aquiles histórico fue el maestro como punto único de fallo.

Los **sistemas de objetos** (p. ej. AWS S3) distribuyen a nivel de archivo completo, no soportan
actualización parcial y se usan mediante SDK/CLI, sin integrarse en el SO. DFSha se sitúa entre ambos
mundos: **internamente basado en bloques** (como HDFS), con un cliente por **SDK/CLI** (como los
sistemas de objetos), pero que además admite lectura aleatoria, anexado y actualización parcial por
*copy-on-write* de bloques inmutables.

### Consenso y consistencia

Replicar el estado de un servicio exige acordar un orden único de operaciones. **Paxos** y **Raft**
(Ongaro y Ousterhout, 2014) resuelven el consenso tolerando la caída de una minoría de réplicas: Raft
descompone el problema en elección de líder, replicación del log y seguridad, y garantiza que las entradas
comprometidas nunca se pierdan ni se reordenen. Una máquina de estados determinista alimentada por ese
log produce réplicas idénticas, y las lecturas servidas por un líder confirmado son **linealizables**
(Herlihy y Wing, 1990). El teorema **CAP** (Brewer; Gilbert y Lynch, 2002) obliga a elegir, ante una
partición, entre consistencia y disponibilidad: el plano de control de DFSha es **CP** (rechaza en vez
de arriesgar divergencia); el plano de datos obtiene disponibilidad con réplicas inmutables.

### Seguridad

* **TLS / mTLS** (RFC 8446): confidencialidad e integridad en tránsito; con certificados de cliente
  los nodos se autentican mutuamente.
* **Cifrado autenticado AES-256-GCM** (NIST SP 800-38D) para datos en reposo, y **cifrado de sobre**
  (*envelope encryption*, como en los KMS de la nube): una clave de datos por archivo, envuelta por una
  clave maestra de la que se derivan las demás con HKDF.
* **Contraseñas con scrypt** (RFC 7914, función de derivación *memory-hard*), **TOTP** (RFC 6238) como
  segundo factor, **API keys** y listas de control de acceso (ACL).

### Principios de diseño aplicados

Separación de plano de control y de datos; inmutabilidad de bloques; verificación de integridad extremo a
extremo; idempotencia de las operaciones reintentadas; diseño para el fallo (todo fallo es el caso
normal); y automatización de la reparación.
