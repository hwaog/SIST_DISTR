# Seguridad de DFSha

## 1. Modelo de amenazas

| Actor | Capacidad asumida | Objetivo defendido |
|---|---|---|
| Intruso de red (en Internet o en la red privada) | Observa y modifica tráfico | Confidencialidad e integridad en tránsito; suplantación de nodos |
| Usuario legítimo malicioso | Credenciales válidas propias | No leer/escribir archivos ajenos; no escalar privilegios |
| Atacante sin credenciales | Alcanza las APIs públicas | Fuerza bruta, tokens forjados, ids maliciosos, cuerpos gigantes |
| Ladrón de disco / DataNode comprometido en reposo | Lee el disco de un DataNode | Confidencialidad de los datos en reposo |
| Nodo no autorizado | Intenta unirse o hablar con el plano de control | Integridad del consenso y de los metadatos |

Fuera de alcance: un ControlNode comprometido *activamente* (custodia las claves envueltas) y
ataques físicos a la memoria de un nodo en ejecución.

## 2. Cifrado en las comunicaciones

* **Todo** el tráfico es TLS ≥ 1.2: cliente↔ControlNode, cliente↔DataNode, ControlNode↔ControlNode (Raft),
  ControlNode↔DataNode y DataNode↔DataNode. Ningún puerto atiende HTTP en claro (verificado por test).
* **PKI propia** (`pki.py`): una CA raíz privada firma un certificado ECDSA P-256 por nodo. El campo
  `OU` del certificado codifica el **rol** (`controlnode`, `datanode`, `admin`) y `CN` el id del nodo.
* **Segmentación**: cada nodo abre dos listeners:
  * **público** (TLS de servidor): lo usan los clientes; autentican con JWT/API key o token de bloque.
  * **interno** (**mTLS**, `CERT_REQUIRED`): sólo nodos con certificado de la CA. Además, cada ruta
    interna exige un **rol** concreto (`/raft/*` sólo `controlnode`; `/internal/dn/*` sólo `datanode`
    y el CN debe coincidir con el `id` declarado, así un DataNode no puede suplantar a otro). En
    producción el puerto interno se restringe además por *security group*/firewall a la red privada.
* La identidad se extrae del certificado verificado en el *handshake* (`common/net.py`, protocolo
  uvicorn propio que registra `(CN, OU)` por conexión).

Pruebas: sin certificado → handshake rechazado; certificado válido de rol equivocado → 403; certificado
de otra CA → rechazado; DataNode que se hace pasar por otro → 403.

## 3. Cifrado en el almacenamiento (envelope encryption)

```
cluster.key (256 bit, sólo ControlNodes)
   ├─ HKDF("kek")         → KEK : envuelve la DEK de cada archivo (AES-GCM)
   ├─ HKDF("jwt")         → firma de sesiones (HS256)
   ├─ HKDF("block-token") → sella los tokens de bloque (AES-GCM); también se entrega, por mTLS, a los DataNodes
   └─ HKDF("secrets")     → envuelve secretos TOTP

DEK (256 bit aleatoria por archivo) ── sólo existe envuelta en la máquina de estados (Raft) ──
```

* Cada bloque se cifra con **AES-256-GCM** y un *nonce* aleatorio de 96 bit. El AAD liga el
  texto cifrado a `(block_id, generación)`: copiar un blob sobre otro bloque falla la autenticación.
* **Los DataNodes no guardan la clave.** La DEK llega *dentro del token sellado* de cada petición y vive en
  memoria durante la operación. Robar el disco de un DataNode no revela clave ni texto claro (verificado
  por test: no aparece texto en claro en `blocks/*.blk`).
* Los blobs son **autoverificables sin clave**: una cola SHA-256 permite al *scrubber* detectar *bit
  rot* en reposo sin descifrar.
* La tubería de replicación reenvía el blob **ya cifrado**: los nodos intermedios ni ven el texto claro
  ni necesitan la DEK.

## 4. Autenticación y control de acceso

### Usuarios
* **Usuario/clave** con `scrypt` (N=2¹⁵, r=8, p=1; memory-hard), con tiempo de verificación igualado
  para usuarios inexistentes (no se filtra qué usuarios existen).
* **2FA TOTP** (RFC 6238; compatible con Google Authenticator/Authy). El secreto se guarda envuelto. Se
  impide el *replay* (cada *step* sólo se acepta una vez, registrado por Raft) y se prueba contra los
  vectores del RFC.
* **API keys** (`dfsha_ak_<id>.<secreto>`) para automatización: sólo se almacena el SHA-256 del secreto;
  revocables.
* **Sesiones** JWT (HS256) con versión de token por usuario: cambiar la clave, deshabilitar o activar 2FA
  **invalida** de inmediato todas las sesiones previas.
* **Anti fuerza bruta**: bloqueo exponencial tras 5 fallos por usuario (y 50 por IP); aplica también a
  códigos 2FA erróneos.

### Autorización
* Permisos **POSIX** (dueño/grupo/otros, `rwx`) y **ACL** por usuario o grupo (`setfacl`). Para llegar a
  un archivo se exige `x` en cada directorio del camino; crear/borrar exige `w`+`x` en el padre.
* Rol `admin`: gestión de usuarios y DataNodes.
* Todas las comprobaciones viven en la máquina de estados (se aplican de forma idéntica en todas las
  réplicas y sin condiciones de carrera entre comandos concurrentes).
* `..` en rutas se resuelve léxicamente en el servidor; los `block_id` deben coincidir con
  `^[0-9a-f]{32}$` (no hay *path traversal* hacia el disco del DataNode).

### Acceso a datos: tokens de capacidad
Un cliente sólo puede tocar un bloque si el ControlNode le entregó un **token sellado** (AES-GCM, TTL
15 min) ligado a `(operación, block_id, usuario, DEK, tubería)`. Los DataNodes no consultan ACL: validan el
token. Un token de lectura no sirve para escribir, ni para otro bloque; uno falsificado o vencido es
rechazado (verificado por test).

## 5. Auditoría y observabilidad

* `audit.log` (JSON por línea, en cada ControlNode): logins correctos/fallidos, cambios de clave/2FA,
  altas/bajas de usuarios, creación/revocación de API keys, mutaciones de espacio de nombres, commits,
  muerte de DataNodes, réplicas corruptas.
* `/metrics` (Prometheus) en el puerto interno.

## 6. Endurecimiento operativo (checklist)

* Claves privadas con permisos `0600`, directorios `0700`; contenedores como usuario no root (uid 10001),
  con `cap_drop: ALL` y `no-new-privileges`.
* **Mínimo privilegio en el material criptográfico (Docker Compose).** Cada nodo monta un volumen propio con
  `ca.pem` + su certificado y clave; `cluster.key` sólo existe en los volúmenes de los 3 ControlNodes (los
  DataNodes nunca lo ven, coherente con §3) y `ca.key` sólo en el volumen `state` del contenedor efímero
  `pki-init`. Así, comprometer un DataNode no entrega ni la clave maestra ni la capacidad de emitir certificados.
  Una versión anterior montaba un único volumen compartido con todas las claves privadas y `ca.key`; se corrigió
  y quedó protegido por `tests/test_compose.py` (cada secreto tiene exactamente los titulares esperados) y por
  `scripts/compose_dryrun.py`.
* En AWS, `deploy/aws/state/` (CA + `cluster.key`) permanece en la máquina del operador, con permisos `0700`, y
  **nunca se versiona** (`.gitignore`); a cada VM sólo llega su paquete.
* Puertos internos (mTLS) cerrados a Internet (*security group* sólo desde el propio grupo).
* Rotar `cluster.key` requiere re-envolver DEK (procedimiento manual documentado en OPERACION.md; no
  automatizado).
* Cambiar la clave de `admin` tras el bootstrap; usar API keys de menor privilegio para scripts.

## 7. Limitaciones conocidas (honestas)

* El ControlNode puede desenvolver DEK (es quien las custodia): un ControlNode comprometido en
  ejecución ve los datos. Mitigación futura: KMS externo (p. ej. AWS KMS) para la KEK, o cifrado de
  extremo a extremo en el cliente.
* El bloqueo anti fuerza bruta es en memoria del líder (se reinicia con un cambio de líder).
* Revocación de certificados de nodo: no hay CRL; se resuelve reemitiendo la CA o retirando el nodo.
* Sin protección contra DoS volumétrico (se delegaría a un balanceador/WAF delante de los puertos públicos).
