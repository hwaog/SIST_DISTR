#!/usr/bin/env bash
# Demostración guiada EN LA NUBE (video).  Misma estructura que scripts/demo.sh, contra el cluster desplegado en AWS.
#
#   source deploy/aws/client/env.sh
#   export DFSHA_PASSWORD='...'  KEY_FILE=~/labsuser.pem
#   scripts/demo_cloud.sh                    interactivo (Enter entre pasos)
#   scripts/demo_cloud.sh --auto             sin pausas (ensayo)
#   scripts/demo_cloud.sh --only 2,3,5       SÓLO esos pasos (cada persona del equipo ejecuta los suyos desde su dispositivo)
#
# Variables opcionales:  SIZE_MIB (tamaño del archivo de prueba, 96 por defecto; 32 recomendado para el video: Internet doméstico),
#                        DEMO_DIR (directorio remoto de trabajo, «proyectos»; use uno distinto por persona si ensayan a la vez).
# Los pasos 4, 6 y 7 operan los nodos por SSH (deploy/aws/node.sh: necesitan inventory.txt y KEY_FILE = «kit de operador»).
# Detiene contenedores con deploy/aws/node.sh (SSH).  NODE_SH permite sustituirlo (las pruebas locales usan un shim).
set -uo pipefail
cd "$(dirname "$0")/.."
AUTO=0; ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --auto) AUTO=1 ;;
    --only) ONLY="${2:?--only necesita una lista, p. ej. --only 2,3,5}"; shift ;;
    *) echo "uso: $0 [--auto] [--only 1,2,...]" >&2; exit 2 ;;
  esac
  shift
done
want() { [[ -z "$ONLY" || ",$ONLY," == *",$1,"* ]]; }
SIZE_MIB="${SIZE_MIB:-96}"; PROJ="${DEMO_DIR:-proyectos}"
: "${DFSHA_ENDPOINTS:?ejecute antes: source deploy/aws/client/env.sh}" "${DFSHA_CA:?falta DFSHA_CA}" "${DFSHA_PASSWORD:?defina DFSHA_PASSWORD}"
export DFSHA_USER="${DFSHA_USER:-admin}" DFSHA_HOME="${DFSHA_HOME:-$PWD/.demo-cloud/client}"
BIN="${DFSHA_VENV_BIN:-.venv/bin}"; [[ -x $BIN/dfsha ]] || { echo "ejecute primero: uv sync"; exit 1; }
NODE_SH="${NODE_SH:-deploy/aws/node.sh}"
if [[ "$NODE_SH" == deploy/aws/node.sh ]] && { want 4 || want 6 || want 7; } && [[ ! -f "${INVENTORY:-deploy/aws/inventory.txt}" ]]; then
  echo "Los pasos 4, 6 y 7 operan los nodos por SSH y necesitan deploy/aws/inventory.txt + KEY_FILE (kit de operador)." >&2
  echo "Desde un dispositivo sin acceso SSH use:  --only 2,3,5,8,9" >&2; exit 1
fi
WORK=.demo-cloud; mkdir -p "$WORK"
dfsha() { "$BIN/dfsha" "$@"; }
say()  { printf '\n\033[1;36m━━ %s\033[0m\n' "$*"; }
note() { printf '\033[2m   %s\033[0m\n' "$*"; }
run()  { printf '\033[1;33m$ %s\033[0m\n' "$*"; eval "$@"; }
pause(){ if ((AUTO)); then sleep 1; else read -r -p $'\033[2m   [Enter]\033[0m ' _; fi; }
PY="$BIN/python"
# Sesión silenciosa si no se ejecuta el paso 1 (que es el que muestra el login)
want 1 || dfsha login -u "$DFSHA_USER" -p "$DFSHA_PASSWORD" >/dev/null
# Los pasos 4-7 necesitan el archivo de prueba (local y remoto) aunque no se haya ejecutado el paso 3
prep_video() {
  [[ -f $WORK/video.bin ]] || head -c $((SIZE_MIB*1048576)) /dev/urandom > "$WORK/video.bin"
  dfsha stat "$PROJ/videos/video.bin" >/dev/null 2>&1 || { dfsha mkdir -p "$PROJ/videos" && dfsha put "$WORK/video.bin" "$PROJ/videos/video.bin" >/dev/null; }
}

if want 1; then
say "1. El cluster REAL en la nube: ControlNodes (Raft) y DataNodes en distintas zonas de disponibilidad"
note "endpoints públicos: $DFSHA_ENDPOINTS"
run "dfsha login -u $DFSHA_USER -p \"\$DFSHA_PASSWORD\""
run "dfsha nodes"
pause

fi

if want 2; then
say "2. RF1 · Sistema de archivos jerárquico + usuarios"
run "dfsha mkdir -p $PROJ/videos"
run "dfsha useradd alice -p alice-password-1 || true"
run "dfsha ls -l / && dfsha tree / -L 2"
pause

fi

if want 3; then
say "3. RF2 · Un archivo de ${SIZE_MIB} MiB sube y baja por Internet: bloques cifrados, en paralelo, replicados entre zonas"
run "head -c $((SIZE_MIB*1048576)) /dev/urandom > $WORK/video.bin"
run "dfsha put $WORK/video.bin $PROJ/videos/video.bin"
run "dfsha blocks $PROJ/videos/video.bin | head -14"
note "cada bloque tiene 3 réplicas en nodos distintos y abarcando varias zonas de disponibilidad"
run "dfsha get $PROJ/videos/video.bin $WORK/video.out && cmp $WORK/video.bin $WORK/video.out && echo 'contenido idéntico (SHA-256 verificado por bloque)'"
pause

fi

if want 4; then
say "4. Cifrado en reposo: el disco de un DataNode en la nube"
run "$NODE_SH blk dn1"
note "cabecera DFSB (formato de bloque cifrado con AES-256-GCM): sin clave no hay texto en claro"
pause

fi

if want 5; then
say "5. Seguridad: permisos, ACL y segundo factor (2FA TOTP)"
dfsha useradd alice -p alice-password-1 >/dev/null 2>&1 || true
run "DFSHA_USER=alice DFSHA_PASSWORD=alice-password-1 DFSHA_HOME=$WORK/alice dfsha ls /home/admin || true"
run "dfsha setfacl -m u:alice:r-x /home/admin && DFSHA_USER=alice DFSHA_PASSWORD=alice-password-1 DFSHA_HOME=$WORK/alice dfsha ls /home/admin"
run "$PY - <<'PY'
import os
from dfsha.client.sdk import Client
from dfsha.common import errors as E
from dfsha.security.auth import totp_now
ep, ca, pw = os.environ['DFSHA_ENDPOINTS'].split(','), os.environ['DFSHA_CA'], os.environ['DFSHA_PASSWORD']
adm = Client(ep, ca); adm.login(os.environ['DFSHA_USER'], pw)
try: adm.userdel('bob')   # la demo es repetible
except E.DfshaError: pass
adm.useradd('bob', 'bob-password-1234')
bob = Client(ep, ca); bob.login('bob', 'bob-password-1234')
secret = bob.enable_2fa('bob-password-1234')['secret']
print('2FA activado para bob; secreto TOTP (se cargaría en Google Authenticator):', secret)
try: Client(ep, ca).login('bob', 'bob-password-1234')
except E.Unauthenticated as e: print('login sin segundo factor ->', e)
c = Client(ep, ca); c.login('bob', 'bob-password-1234', totp_now(secret)); print('login con código TOTP válido ->', c.whoami()['name'], '(2fa =', c.whoami()['totp'], ')')
PY"
CTRL=${DFSHA_ENDPOINTS%%,*}
run "curl -s -m 5 ${CTRL/https/http}/health || echo 'HTTP en claro: rechazado (todo es TLS)'"
pause

fi

if want 6; then
say "6. Alta disponibilidad en la nube (1/3): caída abrupta de un DataNode"
prep_video
run "$NODE_SH kill dn2"
run "dfsha get $PROJ/videos/video.bin $WORK/video.out2 && cmp $WORK/video.bin $WORK/video.out2 && echo 'lectura correcta con un DataNode caído (conmutación transparente)'"
note "esperando a que el cluster lo declare muerto y re-replique sus bloques..."
for i in $(seq 1 150); do
  o=$(dfsha nodes 2>/dev/null)
  # dn2 declarado muerto Y ningún bloque sub-replicado (el cálculo es en el momento: si dn2 guardaba réplicas, ya se re-replicaron)
  if echo "$o" | grep -q "dn2.*NO" && echo "$o" | grep -q "sub-replicados=0"; then break; fi
  sleep 1
done
run "dfsha nodes | tail -12"
run "$NODE_SH start dn2"
pause

fi

if want 7; then
say "7. Alta disponibilidad en la nube (2/3): caída del LÍDER Raft en mitad de una subida"
prep_video
LEADER=$($NODE_SH leader)
note "líder actual: $LEADER"
(sleep 1; $NODE_SH kill "$LEADER") &
run "dfsha put $WORK/video.bin $PROJ/videos/video_durante_failover.bin"
wait
run "dfsha get $PROJ/videos/video_durante_failover.bin $WORK/v3.out && cmp $WORK/video.bin $WORK/v3.out && echo 'subida completada pese a la caída del líder: contenido íntegro'"
run "dfsha nodes | head -9"
run "$NODE_SH start $LEADER"
pause

fi

if want 8; then
say "8. Consistencia: control optimista y commit atómico"
run "$PY - <<'PY'
import os
from dfsha.client.sdk import Client
from dfsha.common import errors as E
ep, ca = os.environ['DFSHA_ENDPOINTS'].split(','), os.environ['DFSHA_CA']
c = Client(ep, ca); c.login(os.environ['DFSHA_USER'], os.environ['DFSHA_PASSWORD'])
c.send(b'v1', '/home/admin/doc'); v = c.stat('/home/admin/doc')['version']
c.send(b'v2', '/home/admin/doc', if_version=v)
try: c.send(b'v3-obsoleto', '/home/admin/doc', if_version=v)
except E.Conflict as e: print('CAS rechazado:', e)
a, b = c.open('/home/admin/doc','r+b'), c.open('/home/admin/doc','r+b')
a.write(b'AA'); b.write(b'BB'); a.close()
try: b.close()
except E.Conflict as e: print('segundo escritor optimista rechazado:', e)
print('contenido final:', c.receive('/home/admin/doc'))
PY"
pause

fi

if want 9; then
say "9. Estado final y evidencia de rendimiento"
run "dfsha fsck"
if [ -d bench/results_cloud ] && ls bench/results_cloud/*.json >/dev/null 2>&1; then
  note "resultados de los benchmarks en VMs separadas (bench/results_cloud/):"
  run "ls -1 bench/results_cloud/"
else
  note "(aún no hay bench/results_cloud/: ejecute deploy/aws/bench-remote.sh)"
fi
run "dfsha rm -r $PROJ"
fi
