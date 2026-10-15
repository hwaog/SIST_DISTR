# Despliegue de DFSha en AWS Academy — guía paso a paso

> Esta guía lleva de **cero a un cluster DFSha corriendo en la nube, validado, medido y con evidencia para el informe**.
> Los scripts de esta carpeta están probados con un `aws`/`ssh`/`rsync`/`curl` simulados (`tests/test_aws_scripts.py`) y
> el validador (`dfsha-cloud`) está probado contra un cluster local; **lo único que no se pudo ejecutar fue contra AWS real**
> (no había credenciales). Por eso cada paso indica la salida esperada y qué hacer si algo falla.
>
> **Tiempo total:** 1,5–2,5 h (la mayor parte es espera de `launch` y `deploy`). **Costo:** unos 0,2 USD/hora con la topología por
> defecto (7 × t3.small + 1 × t3.medium); el Learner Lab da 50–100 USD de crédito.

## 0. Qué se va a crear

```
 Su portátil ──HTTPS──▶  VPC por defecto · security group dfsha-sg
 (CLI, validate.sh)        ┌─────────────┬─────────────┬─────────────┐
                           │ AZ us-east-1a│ AZ us-east-1b│ AZ us-east-1c│
                           │ c1 Control   │ c2 Control   │ c3 Control   │  ← Raft (3 réplicas de metadatos)
                           │ dn1 · dn4    │ dn2          │ dn3          │  ← 4 DataNodes, réplicas ×3 entre AZ
                           │ cl1 cliente  │              │              │  ← VM cliente (mide sin el cuello de su internet)
                           └─────────────┴─────────────┴─────────────┘
 Puertos PÚBLICOS: 22, 8441-8449 (API ControlNode), 7441-7449 (API DataNode) → su IP (ALLOW_CIDR) y los miembros del security group
 Puertos INTERNOS (mTLS): 9441-9449, 7541-7549 → sólo entre miembros del security group
```

Cada VM corre **un contenedor Docker** con la misma imagen (`docker/Dockerfile`). Tras terminar tendrá en `deploy/aws/evidence/`
la evidencia que el informe incorpora **automáticamente**.

---

## 1. Requisitos (una sola vez, en SU máquina)

| Herramienta | Instalar (macOS) | Comprobar |
|---|---|---|
| AWS CLI v2 | `brew install awscli` | `aws --version` |
| uv | `brew install uv` | `uv --version` |
| rsync, ssh, curl | ya vienen con macOS | `rsync --version` |
| Proyecto sincronizado | `cd <repo> && uv sync --extra dev --extra report` | `ls .venv` |

No hace falta Docker en su máquina para este camino (Docker se instala en las VM por *user-data*).

## 2. Iniciar el Learner Lab y obtener credenciales

1. En AWS Academy: *Courses → Learner Lab → **Start Lab***. Espere al círculo **verde** (1–3 min).
2. Pulse **AWS Details** → junto a *AWS CLI* pulse **Show**. Copie las 3 líneas (`aws_access_key_id`, `aws_secret_access_key`,
   `aws_session_token`) en `~/.aws/credentials`:
   ```ini
   [default]
   aws_access_key_id=ASIA...
   aws_secret_access_key=...
   aws_session_token=...
   ```
   y fije la región (el laboratorio sólo permite `us-east-1` y `us-west-2`). Basta con una variable de entorno; los scripts la
   exportan por usted, así que **no hace falta `aws configure set region`**:
   ```bash
   export AWS_REGION=us-east-1
   ```
   > ⚠ **Ejecute todo esto en el terminal de SU máquina (macOS/Linux), no en el terminal web del Learner Lab (Vocareum).** Ese
   > terminal ya trae credenciales y región configuradas, pero su `HOME` es de sólo lectura (de ahí el error
   > `[Errno 13] Permission denied: '/mnt/vocwork…/.aws/config'` al ejecutar `aws configure set …`) y no tiene este repositorio, `uv`
   > ni la clave SSH, que los scripts necesitan.
3. Pulse **Download PEM** (junto a *SSH key*) → guarde el archivo como `~/labsuser.pem` y protéjalo:
   ```bash
   chmod 400 ~/labsuser.pem
   export KEY_FILE=~/labsuser.pem
   ```
4. Compruebe:
   ```bash
   aws sts get-caller-identity        # debe mostrar un ARN con "assumed-role/LabRole"
   ```

> ⚠ Las credenciales **caducan** al terminar la sesión del laboratorio (≈4 h). Si más adelante ve `ExpiredToken` o
> `AuthFailure`, repita el paso 2.2.

## 3. Pre-chequeo (gratis, evita gastar créditos en balde)

```bash
cd deploy/aws
./preflight.sh
```
Debe terminar con `Todo listo para ./launch.sh`. Cada línea `✗` dice exactamente qué corregir.

## 4. Lanzar la infraestructura

```bash
# ALLOW_CIDR limita SSH y las APIs públicas a SU IP (recomendado). Para que el profesor pueda probar desde otra red
# use temporalmente ALLOW_CIDR=0.0.0.0/0 (la API sigue protegida por TLS + autenticación).
ALLOW_CIDR="$(curl -s https://checkip.amazonaws.com)/32" ./launch.sh
```

**Qué hace:** crea el *security group* `dfsha-sg`, lanza 3 ControlNodes + 4 DataNodes + 1 cliente repartidos **una por zona de
disponibilidad (rotando)**, espera a que pasen los chequeos de estado (≈2–3 min) y escribe `inventory.txt`.

**Salida esperada (final):**
```
==> Inventario -> inventory.txt   (rol nombre ip_privada dns_publico zona)
client    cl1  172.31.x.x  ec2-….compute-1.amazonaws.com  us-east-1a
control   c1   172.31.x.x  ec2-….compute-1.amazonaws.com  us-east-1a
control   c2   …                                            us-east-1b
control   c3   …                                            us-east-1c
data      dn1 … us-east-1a   (dn2 us-east-1b, dn3 us-east-1c, dn4 us-east-1d …)
```
Verifique que los DataNodes quedaron en **≥ 2 zonas distintas** (columna final).

| Si ocurre… | Haga |
|---|---|
| `You have requested more instances than your current instance limit` | el laboratorio permite ≈9: `N_DATA=3 N_CLIENT=1 ./launch.sh` (3+3+1 = 7) |
| `InstanceType ... is not supported` / `UnauthorizedOperation` | `INSTANCE_TYPE=t2.small CLIENT_TYPE=t2.medium ./launch.sh` |
| `volume type gp3 ... not` | `VOLUME_TYPE=gp2 ./launch.sh` |
| `ya existen instancias de DFSha` | ya lanzó antes: use `./refresh-inventory.sh` (reutilizar) o `./teardown.sh` (borrar) |
| `InvalidKeyPair.NotFound` | `KEY_NAME=<su-par> ./launch.sh` (el del laboratorio se llama `vockey`) |
| Falla a medias | `./teardown.sh --yes` y repita |

## 5. Generar certificados y secretos

```bash
ADMIN_PASSWORD='una-clave-larga-y-unica' ./gen-bundles.sh
```
* Crea **una sola vez** la CA privada y el secreto maestro del cluster en `state/` (**no se versiona**; haga una copia:
  `cp -r state ~/dfsha-state-backup`). Emite un certificado por nodo con SAN = IP privada + DNS público.
* Reglas de mínimo privilegio: cada nodo recibe sólo su par de claves; **sólo** los ControlNodes reciben `cluster.key`; la clave de la CA
  nunca sale de `state/`.
* ⚠ **Nunca borre `state/` si ya hay datos**: los archivos guardados quedarían ilegibles (sus claves de datos están envueltas con ese
  secreto). Volver a ejecutar este script es seguro: sólo re-emite los certificados.

## 6. Desplegar

```bash
KEY_FILE=~/labsuser.pem ./deploy.sh
```
Tarda **5–10 min** (construye la imagen en cada VM, en paralelo). Es **idempotente**: puede repetirlo (conserva datos y estado Raft);
`ONLY=dn2 ./deploy.sh` redespliega un solo nodo.

**Salida esperada (final):**
```
==> Comprobación de salud desde esta máquina (HTTPS verificado contra la CA)
  ✓ c1   {"ok":true,"id":"c1","role":"leader","term":1,...}
  ✓ c2   {"ok":true,"id":"c2","role":"follower",...}
  ✓ c3   …
  ✓ dn1  {"ok":true,"id":"dn1","zone":"us-east-1a","registered":true,...}   (… dn2, dn3, dn4)
Desplegado. Desde su portátil:  source deploy/aws/client/env.sh && dfsha login -u admin && dfsha nodes
```

| Si ocurre… | Causa probable | Solución |
|---|---|---|
| `Permission denied (publickey)` | `KEY_FILE` incorrecta o sin `chmod 400` | revise `echo $KEY_FILE`; usuario SSH es `ec2-user` |
| `Docker no está listo tras 5 min` | *user-data* no terminó | `ssh -i $KEY_FILE ec2-user@<dns> 'cloud-init status --wait; docker --version'` y repita |
| `Unavailable: el cluster no respondió dentro de 45s: timed out` al ejecutar `bench-remote.sh` | SG creado con una versión anterior de `launch.sh`: la VM cliente (dentro del VPC) no podía usar las APIs públicas | añada las reglas al SG existente (una vez): `SG=$(aws ec2 describe-security-groups --filters Name=group-name,Values=dfsha-sg --query 'SecurityGroups[0].GroupId' --output text); for p in 22 8441-8449 7441-7449; do aws ec2 authorize-security-group-ingress --group-id $SG --protocol tcp --port $p --source-group $SG; done` |
| `✗ c1 NO responde en https://…:8441/health` | security group / su IP cambió | `curl -v --cacert client/ca.pem https://<dns>:8441/health`; si su IP cambió: `aws ec2 authorize-security-group-ingress --group-id <sg> --protocol tcp --port 8441-8449 --cidr <nueva-ip>/32` (y 7441-7449, 22) |
| `./node.sh logs c1` muestra `address already in use` | contenedor previo | `./node.sh restart c1` |
| `CERTIFICATE_VERIFY_FAILED` | el DNS público cambió (se reinició el laboratorio) | §12: `refresh-inventory.sh` → `gen-bundles.sh` → `deploy.sh` |
| Los DataNodes no se registran (`registered:false`) | aún no hay líder o no alcanzan el puerto interno | espere 20 s; `./node.sh logs dn1`; compruebe que el SG permite 9441-9449 **desde el propio SG** |

## 7. Comprobación manual rápida

```bash
cd ../..                                   # raíz del repo
source deploy/aws/client/env.sh
uv run dfsha login -u admin                # pide la clave del paso 5
uv run dfsha nodes
```
Debe ver **1 `leader` y 2 `follower`**, y **4 DataNodes vivos** con zonas = nombres de AZ. `deploy/aws/status.sh` muestra además el estado
de cada contenedor.

## 8. Validación automática + evidencia  ⭐ (esto alimenta el informe)

```bash
cd deploy/aws
DFSHA_PASSWORD='la-clave-del-paso-5' KEY_FILE=~/labsuser.pem ./validate.sh            # ≈ 5–10 min
```
Ejecuta **10 pasos** y escribe `deploy/aws/evidence/<fecha>/{resultados.json,resumen.md,log.txt}` (y el enlace `evidence/latest`):

| # | Paso | Qué demuestra | PASS esperado |
|---|---|---|---|
| 1 | Conectividad y líder único | HTTPS verificado contra la CA; un solo líder Raft | 3 nodos, 1 líder |
| 2 | Login y topología | ≥3 DataNodes vivos, ≥2 zonas | `WARN` si sólo hay 1 zona |
| 3 | Controles de seguridad de red | HTTP plano rechazado; TLS sin la CA rechazado; API y bloques exigen token; **puerto interno inalcanzable desde Internet** | `PASS` (si lo ejecuta desde su portátil) |
| 4 | put/get con SHA-256 | transferencia real sobre Internet; MiB/s | contenido idéntico |
| 5 | Mapa de bloques | cada bloque con 3 réplicas repartidas en ≥2 zonas | `PASS` |
| 6 | Cifrado en reposo | inspecciona el disco de un DataNode por SSH: cabecera `DFSB`, 0 coincidencias del texto claro | `PASS` |
| 7 | Failover del líder | `docker kill` del líder → tiempo hasta volver a escribir; datos íntegros; el nodo se reincorpora | `PASS` (≈2–6 s) |
| 8 | Caída de un DataNode | lectura inmediata correcta; detección y re-replicación (`dn_dead_after`=10 s + `repl_grace`=15 s + copia) | `PASS` (≈30–60 s) |
| 9 | fsck final | todos los bloques sanos | `PASS` |
| 10 | Limpieza | borra los archivos de prueba | `PASS` |

`WARN` es aceptable si lo entiende (p. ej. paso 3 ejecutado desde dentro del VPC). **Cualquier `FAIL` hay que corregirlo**; revise
`evidence/<fecha>/log.txt`. Si el paso 8 falla por tiempo, repita con `./validate.sh --wait-repair-s 600`. El validador **vuelve a arrancar** los contenedores que detiene.

## 9. Benchmarks con nodos en máquinas distintas

Se ejecutan **desde la VM cliente** (mismo VPC): medir desde su portátil mediría su conexión doméstica, no el sistema.

```bash
DFSHA_PASSWORD='…' KEY_FILE=~/labsuser.pem ./bench-remote.sh scaling      # throughput vs nº de DataNodes activos (≈10–15 min)
DFSHA_PASSWORD='…' KEY_FILE=~/labsuser.pem ./bench-remote.sh throughput   # efecto de hilos de transferencia
DFSHA_PASSWORD='…' KEY_FILE=~/labsuser.pem ./bench-remote.sh metadata     # ops/s y latencia de metadatos
# SIZE_MIB=512 REPS=5 ./bench-remote.sh scaling      para mediciones más largas
```
Trae los JSON a `bench/results_cloud/`. El informe los compara automáticamente con los de una sola máquina.
Interpretación: con nodos en VMs separadas **sí** debería verse que el ancho de banda agregado crece con el nº de DataNodes
(acotado por el ancho de banda de red de la VM cliente: una t3.medium ofrece hasta ~5 Gbps en ráfagas).

## 10. Capturas de pantalla para el informe

Guárdelas en `deploy/aws/evidence/capturas/` con nombre `NN-descripcion.png`; el informe las incluye solas, en orden.

| Archivo sugerido | Qué capturar |
|---|---|
| `01-consola-ec2.png` | Consola EC2 → Instancias: las 8 instancias `dfsha-*` en ejecución, con la columna *Zona de disponibilidad* visible |
| `02-security-group.png` | EC2 → Grupos de seguridad → `dfsha-sg` → *Reglas de entrada*: públicos desde su IP; 9441-9449 y 7541-7549 con origen = el propio grupo |
| `03-dfsha-nodes.png` | Terminal: `dfsha nodes` (líder, seguidores, DataNodes con zonas) |
| `04-dfsha-blocks.png` | Terminal: `dfsha put` de un archivo grande + `dfsha blocks` (réplicas en distintas zonas) |
| `05-cifrado-en-disco.png` | Terminal: `./node.sh blk dn1` (cabecera `DFSB` en el disco de la VM) |
| `06-failover-lider.png` | Terminal: `dfsha nodes` antes y después de `./node.sh kill $(./node.sh leader)` |
| `07-re-replicacion.png` | Terminal: `./node.sh kill dn2` y luego `dfsha nodes`/`dfsha fsck` hasta volver a sano |
| `08-validate.png` | Terminal: resumen final de `validate.sh` (10 pasos PASS) |
| `09-benchmark-escalado.png` | Terminal: salida de `bench-remote.sh scaling` |

## 11. Regenerar el informe con la evidencia

```bash
cd ../..
uv run python report/build_report.py                      # Word (regenera la sección «Despliegue en la nube»)
uv run --with markdown python report/build_pdf.py         # PDF
uv run python scripts/check_entrega.py                    # qué falta antes de entregar
git add -A && git commit -m "docs: evidencia del despliegue en AWS Academy"
```
La sección *Despliegue en la nube* pasa de «pendiente de ejecución» a mostrar la topología real, la tabla de validación, los tiempos de
*failover*/reparación, la comparación de escalado nube vs una sola máquina y sus capturas.

## 12. Pausar y reanudar el laboratorio (sin perder datos)

Al terminar la sesión AWS Academy **detiene** las instancias (los discos EBS persisten; confirme el comportamiento en su curso). Al
volver, las IP/DNS públicos **cambian**, y los certificados llevan el DNS público en su SAN, así que hay que re-emitirlos:

```bash
cd deploy/aws
./refresh-inventory.sh --start          # arranca las instancias detenidas y rehace inventory.txt
./gen-bundles.sh                        # CONSERVA la CA y el secreto maestro (state/); sólo re-emite certificados
KEY_FILE=~/labsuser.pem ./deploy.sh     # reinicia los contenedores con los DNS nuevos; datos y metadatos intactos
source client/env.sh && cd ../.. && uv run dfsha nodes
```

## 13. Terminar y limpiar (¡importante!)

```bash
cd deploy/aws
./teardown.sh                  # pide confirmación; borra instancias, IP elásticas y el security group (DESTRUYE los datos)
```
Verifique en la consola EC2 que no quedan instancias, volúmenes ni IP elásticas. En el Learner Lab pulse **End Lab** al terminar la sesión.
Si desea conservar datos entre sesiones **no** ejecute `teardown.sh`; use el paso 13.

## 14. Operación diaria

```bash
./status.sh                    # contenedores + estado del cluster
./node.sh logs c1 100          # log de un nodo
./node.sh kill  $(./node.sh leader)   # caos: matar al líder
./node.sh start c1             # volver a arrancarlo
./node.sh blk   dn1            # primeros bytes de un bloque en disco
./node.sh shell dn1            # SSH interactivo
```

## 15. Notas de seguridad del despliegue

* `ALLOW_CIDR` abre SSH y las APIs de cliente: déjelo en su IP/32. Con `0.0.0.0/0` la API sigue exigiendo TLS y autenticación, pero queda
  expuesta a escaneos.
* Los puertos **internos** (9441-9449, 7541-7549) sólo aceptan tráfico del propio *security group* **y** exigen mTLS con certificado emitido por
  la CA del cluster.
* `state/` (CA + secreto maestro) y `bundles/` (claves privadas) **no se versionan** (`.gitignore`). La clave privada de cada nodo viaja por SSH una vez.
* Cambie la clave de `admin` tras el despliegue (`dfsha passwd`) y use API keys para automatizar.

## 16. Si prefiere GCP (Compute Engine) u otro IaaS

Los scripts de despliegue sólo necesitan **VM con Docker, SSH y un `inventory.txt`**; sólo `launch.sh`/`teardown.sh`/`refresh-inventory.sh` son de AWS.

1. Cree 8 VM (Ubuntu 22.04) en ≥ 2–3 zonas (`gcloud compute instances create c1 --zone us-central1-a --machine-type e2-small …`) e instale Docker
   (`curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER`) y `rsync`.
2. Reglas de firewall: públicas `tcp:22,7441-7449,8441-8449` desde su IP; internas `tcp:7541-7549,9441-9449` **sólo** con *source tags* de las propias VM.
3. Escriba `inventory.txt` a mano con el formato de `inventory.example` (`rol nombre ip_privada dns_o_IP_pública zona`; la IP pública sirve como «DNS»: va en el SAN).
4. `SSH_USER=<su-usuario> KEY_FILE=~/.ssh/google_compute_engine ADMIN_PASSWORD=… ./gen-bundles.sh && SSH_USER=… KEY_FILE=… ./deploy.sh`, y el resto igual (`validate.sh`, `bench-remote.sh`).

## 17. Lista final («definición de terminado»)

- [ ] `./preflight.sh` verde, `./launch.sh`, `./gen-bundles.sh`, `./deploy.sh` sin errores
- [ ] `dfsha nodes` muestra 1 líder, 2 seguidores y 4 DataNodes en ≥2 zonas
- [ ] `./validate.sh` → «TODOS LOS PASOS SUPERADOS»; existe `evidence/latest/resultados.json`
- [ ] `./bench-remote.sh scaling` (y `throughput`, `metadata`) → `bench/results_cloud/*.json`
- [ ] ≥ 3 capturas en `evidence/capturas/` (ideal las 9 de la tabla)
- [ ] Informe regenerado (.docx y .pdf) y `scripts/check_entrega.py` sin ✗
- [ ] `./teardown.sh` ejecutado **o** laboratorio terminado, para no gastar créditos
