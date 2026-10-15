# Especificación de protocolos y APIs

Todos los nodos exponen una API HTTPS/JSON (REST). El ControlNode publica además su esquema OpenAPI
interactivo en `https://<controlnode>:<puerto-api>/docs`.

| Enlace | Protocolo | Puerto | Autenticación | Contenido |
|---|---|---|---|---|
| Cliente ↔ ControlNode | HTTPS REST/JSON | `api` (p. ej. 8441) | `Authorization: Bearer <JWT>` o `X-API-Key` | metadatos, autenticación, asignación y ubicación de bloques, locks |
| Cliente ↔ DataNode | HTTPS binario | `api` (p. ej. 7441) | `Authorization: Bearer <token de bloque>` | bloques |
| ControlNode ↔ ControlNode | HTTPS **mTLS** JSON (Raft) | `interno` (9441) | certificado rol `controlnode` | `pre_vote`, `request_vote`, `append_entries`, `install_snapshot` |
| ControlNode ↔ DataNode | HTTPS **mTLS** JSON | `interno` | certificado rol `datanode` / `controlnode` | registro, heartbeat, informe de bloques, órdenes de replicar/borrar |
| DataNode ↔ DataNode | HTTPS **mTLS** binario | `interno` | certificado rol `datanode` + token de bloque | tubería de escritura, copia para re-replicación |

**Por qué REST/HTTPS y no gRPC/MOM**: el enunciado lo admite explícitamente; REST es depurable con
`curl`, FastAPI genera OpenAPI y validación automática, y el cuello de botella real (copiar bloques de
varios MiB) no se beneficia de gRPC. No se usa un MOM porque la coordinación necesaria es síncrona y
con confirmación (quórum), no un flujo de eventos desacoplado.

## Convenciones

* **Errores**: `{"error": {"code": "EACCES", "message": "...", "extra": {...}}}` con estado HTTP
  coherente. Códigos estables: `ENOENT`(404) `EEXIST`(409) `ENOTDIR`/`EISDIR`/`EINVAL`(400) `ENOTEMPTY`(409)
  `EACCES`(403) `EAUTH`(401) `ELOCKED`(423) `ECONFLICT`(412) `ETHROTTLED`(429) `ENOSPC`(507)
  `EUNAVAIL`(503) `ENOTLEADER`(503, `extra.leader` = URL del líder) `ECORRUPT`(422) `ESTALE`(410).
* **Idempotencia**: las mutaciones aceptan `X-Request-Id`; reenviarlo devuelve el resultado original
  (también el error original) sin re-ejecutar.
* **Lecturas relajadas**: `?stale=1` permite servir la consulta desde cualquier ControlNode.
* **Paginación**: `GET /v1/files/locations?start=&limit=` (≤1024 bloques por página).
* **Versionado**: prefijo `/v1`.

## ControlNode — API pública

### Sistema
| Método y ruta | Descripción |
|---|---|
| `GET /health` | sin auth: `{ok, id, role, term, leader}` |
| `GET /v1/node` | sin auth: rol Raft, término, índices de commit/aplicado |
| `GET /v1/cluster` | estado completo: ControlNodes, DataNodes, df, fsck, Raft, contadores de mantenimiento |

### Autenticación
| Método y ruta | Cuerpo | Respuesta |
|---|---|---|
| `POST /v1/auth/login` | `{username, password, otp?}` | `{token, expires_at, user, groups, admin}` · `EAUTH` con `extra.otp_required` si falta 2FA |
| `POST /v1/auth/refresh` | – | token nuevo |
| `GET /v1/whoami` | – | `{name, groups, admin, enabled, totp}` |
| `POST /v1/auth/password` | `{old, new}` | invalida sesiones previas |
| `POST /v1/auth/totp/enable` · `/disable` | `{password[, otp]}` | `{secret, uri}` al activar |
| `POST /v1/auth/apikeys` · `GET` · `DELETE /{id}` | `{label}` | `{id, key}` (la clave sólo se muestra una vez) |

### Sistema de archivos (RF1)
| Ruta | Parámetros |
|---|---|
| `GET /v1/fs/stat?path=` · `GET /v1/fs/ls?path=` · `GET /v1/fs/df` | lecturas linealizables (o `stale`) |
| `POST /v1/fs/mkdir` | `{path, parents, mode}` |
| `POST /v1/fs/rmdir` | `{path}` |
| `POST /v1/fs/rm` | `{path, recursive, lock_id?}` |
| `POST /v1/fs/mv` | `{src, dst}` (atómico; sobre directorio destino mueve dentro) |
| `POST /v1/fs/chmod` · `chown` · `setacl` · `setrepl` | `{path, mode}` · `{path, owner?, group?}` · `{path, entries:[{t,n,p}]}` · `{path, repl}` |

### Acceso y transferencia de archivos (RF2, RF3)
| Ruta | Descripción |
|---|---|
| `POST /v1/files/begin` | `{path, mode: replace\|append\|update, overwrite, repl?, block_size?, lock_id?}` → `{sid, block_size, min_repl, version, blocks[], session_ttl}` |
| `POST /v1/files/{sid}/blocks` | `{count, zone?, exclude[]}` → `blocks[{block_id, pipeline[{id,url}], token}]` |
| `POST /v1/files/{sid}/realloc` | `{block_id, zone?, exclude[]}` → nueva tubería y token (reubicación tras fallo) |
| `POST /v1/files/{sid}/commit` | `{layout[{id,len,sha,reps[]}\|{id}], size, if_version?, lock_id?}` → `{inode, version, size}` (atómico) |
| `POST /v1/files/{sid}/abort` · `/renew` | cancelar / extender el *lease* de la sesión |
| `GET /v1/files/locations?path=&start=&limit=&zone=` | `{size, version, block_size, repl, nblocks, blocks[{block_id,len,sha,locs[{id,url}],token}]}` |

### Locks
`POST /v1/locks/acquire` `{path, mode: shared|exclusive, ttl, lock_id?}` → `{lock_id, fence, expires}` ·
`POST /v1/locks/renew` · `POST /v1/locks/release` `{lock_id, ttl}`.

### Administración (rol admin)
`GET|POST /v1/admin/users` · `PATCH|DELETE /v1/admin/users/{name}` · `GET /v1/admin/datanodes` ·
`POST /v1/admin/datanodes/{id}/state` `{state: active|decommissioning|removed}` ·
`POST /v1/admin/datanodes/{id}/scrub` (verificación inmediata de integridad en reposo; devuelve los bloques corruptos puestos en cuarentena) · `GET /v1/admin/fsck`.

## ControlNode — API interna (mTLS)

| Ruta | Rol exigido | Descripción |
|---|---|---|
| `POST /raft/{pre_vote,request_vote,append_entries,install_snapshot}` | `controlnode` | RPC de Raft (ver abajo) |
| `POST /internal/dn/register` | `datanode` (CN = `dn`) | alta del DataNode; devuelve la clave de tokens de bloque |
| `POST /internal/dn/heartbeat` | `datanode` | `{dn, used, capacity, nblocks, load}` a **todos** los ControlNodes |
| `POST /internal/dn/blockreport` | `datanode` | `{dn, blocks:[{id,n}]}` al líder; reconciliación |
| `POST /internal/dn/corrupt` | `datanode` | réplicas corruptas halladas |
| `GET /metrics` | cualquier par mTLS | Prometheus |

### Mensajes Raft (JSON)
* `append_entries`: `{term, leader_id, prev_index, prev_term, entries[{t,i,c}], leader_commit}` →
  `{term, success, match_index | conflict_index, conflict_term}`
* `request_vote` / `pre_vote`: `{term, candidate_id, last_log_index, last_log_term}` → `{term, granted}`
* `install_snapshot`: `{term, leader_id, last_index, last_term, data(base64)}` → `{term}`

## DataNode — API pública (token de bloque)

| Ruta | Descripción |
|---|---|
| `PUT /v1/blocks/{id}` | cuerpo = texto claro del bloque. Cabeceras: `Authorization: Bearer <token w>`, `X-Block-Sha256`, `X-Pipe-Pos` (posición en la tubería, 0 por defecto). Respuesta: `{acks:[dn…]}` |
| `GET /v1/blocks/{id}?offset=&length=` | texto claro (descifrado en el nodo con la DEK del token) + `X-Block-Sha256`. `offset/length` permiten lecturas parciales |
| `GET /health` | `{ok, id, zone, registered, blocks, used}` |

## DataNode — API interna (mTLS)

| Ruta | Rol | Descripción |
|---|---|---|
| `PUT /internal/blocks/{id}` | `datanode` | siguiente salto de la tubería: cuerpo = blob cifrado; `X-Block-Token`, `X-Pipe-Pos`, `X-Blob-Sha256` |
| `GET /internal/blocks/{id}/raw` | `datanode` | blob cifrado (para re-replicación) |
| `POST /internal/replicate` | `controlnode` | `{block_id, sources[]}`: copiar y verificar desde una fuente |
| `POST /internal/delete` | `controlnode` | `{blocks[]}`: borrar |
| `POST /internal/scrub` | `controlnode`, `admin` | verificación de integridad inmediata |

## Tokens

| Token | Formato | Vida | Contenido |
|---|---|---|---|
| Sesión de usuario | JWT HS256 | 8 h | `sub, ver, iat, exp, jti, amr` |
| Bloque (lectura) | `v1.` + base64url(AES-GCM) | 15 min | `{op:"r", bid, dek, uid}` |
| Bloque (escritura) | ídem | 15 min | `{op:"w", bid, dek, pipe[{id,iu}], uid, max, gen}` |
