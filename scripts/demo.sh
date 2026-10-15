#!/usr/bin/env bash
# Demostración guiada de DFSha (≈12 min) para el video.  Cada paso espera [Enter].
#   scripts/demo.sh           interactivo (grabación)
#   scripts/demo.sh --auto    sin pausas (validación automática)
set -uo pipefail
cd "$(dirname "$0")/.."
AUTO=0; [[ "${1:-}" == "--auto" ]] && AUTO=1
export DFSHA_HOME="$PWD/.demo/client" DFSHA_USER=admin DFSHA_PASSWORD=dfsha-admin-dev
BIN=.venv/bin; [[ -x $BIN/dfsha ]] || { echo "ejecute primero: uv sync"; exit 1; }
dfsha() { "$BIN/dfsha" "$@"; }
cluster() { "$BIN/dfsha-cluster" --dir .demo "$@"; }
say()  { printf '\n\033[1;36m━━ %s\033[0m\n' "$*"; }
note() { printf '\033[2m   %s\033[0m\n' "$*"; }
run()  { printf '\033[1;33m$ %s\033[0m\n' "$*"; eval "$@"; }
pause(){ if ((AUTO)); then sleep 1; else read -r -p $'\033[2m   [Enter]\033[0m ' _; fi; }

cluster destroy >/dev/null 2>&1 || true
rm -rf .demo; mkdir -p .demo

say "1. Cluster: 3 ControlNodes (Raft) + 5 DataNodes en 2 zonas — procesos reales con TLS y mTLS"
run "cluster up --control 3 --data 5 --zones 2 --no-fsync --balance-interval 3"
eval "$(cluster env)"
run "dfsha login -u admin -p dfsha-admin-dev"
run "dfsha nodes"
pause

say "2. RF1 · Gestión del sistema de archivos (abstracción jerárquica tipo Linux)"
run "dfsha mkdir -p proyectos/videos"
run "dfsha useradd alice -p alice-password-1"
run "dfsha ls -l / && dfsha tree / -L 2"
pause

say "3. RF2 · Transferencia: un archivo de 48 MiB se parte en bloques, se cifra y se reparte en paralelo"
run "head -c 50331648 /dev/urandom > .demo/video.bin"
run "dfsha put .demo/video.bin proyectos/videos/video.bin"
run "dfsha blocks proyectos/videos/video.bin"
note "cada bloque tiene 3 réplicas, en nodos distintos y abarcando ambas zonas"
run "dfsha get proyectos/videos/video.bin .demo/video.out && cmp .demo/video.bin .demo/video.out && echo 'contenido idéntico (SHA-256 verificado por bloque)'"
pause

say "4. Cifrado en reposo: lo que realmente hay en el disco de un DataNode"
F=$(ls .demo/data/dn1/blocks/*/*.blk | head -1)
run "ls -lh $F && xxd $F | head -5"
run "grep -c 'TEXTO-SECRETO' $F || true"
pause

say "5. RF3 · Acceso tipo archivo: open / read / write / lock desde el SDK"
run ".venv/bin/python - <<'PY'
import os
from dfsha.client.sdk import Client
ep, ca = os.environ['DFSHA_ENDPOINTS'].split(','), os.environ['DFSHA_CA']
c = Client(ep, ca, credentials=('admin','dfsha-admin-dev')); c.login('admin','dfsha-admin-dev')
P = '/shared/diario.txt'
with c.open(P,'w') as f: f.write('línea 1\\n')
with c.open(P,'a') as f: f.write('línea 2 (append)\\n')
c.chmod(P, 0o666)
with c.open(P,'r+b') as f:
    f.seek(0); f.write('LÍNEA'.encode())      # actualización parcial: copy-on-write de un bloque
print(c.open(P).read().decode(), end='')
print('versión del archivo:', c.stat(P)['version'], '(una por cada commit atómico)')
with c.lock(P,'exclusive') as lk:
    print('lock exclusivo adquirido, fencing token =', lk.fence)
    other = Client(ep, ca, credentials=('alice','alice-password-1')); other.login('alice','alice-password-1')
    for what, fn in (('lock compartido', lambda: other.lock(P,'shared')), ('escritura', lambda: other.send(b'intruso', P))):
        try: fn()
        except Exception as e: print(f'alice ({what}) rechazada por el servidor ->', e)
print('lock liberado; alice ya puede escribir:', bool(other.send(b'alice escribe', P)))
PY"
pause

say "6. Seguridad: permisos, ACL, 2FA, TLS/mTLS"
run "dfsha logout; DFSHA_USER=alice DFSHA_PASSWORD=alice-password-1 dfsha ls /home/admin || true"
run "DFSHA_USER=alice DFSHA_PASSWORD=alice-password-1 dfsha put .demo/video.out /home/admin/intruso.bin || true"
run "dfsha setfacl -m u:alice:r-x /home/admin && dfsha getfacl /home/admin"
run "DFSHA_USER=alice DFSHA_PASSWORD=alice-password-1 dfsha ls /home/admin"
note "Segundo factor (2FA TOTP): sin el código, la clave sola ya no basta"
run ".venv/bin/python - <<'PY'
import os
from dfsha.client.sdk import Client
from dfsha.common import errors as E
from dfsha.security.auth import totp_now
ep, ca = os.environ['DFSHA_ENDPOINTS'].split(','), os.environ['DFSHA_CA']
adm = Client(ep, ca); adm.login('admin', 'dfsha-admin-dev')
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
note "HTTP en claro y clientes sin certificado en el puerto interno son rechazados:"
CTRL=$(echo "$DFSHA_ENDPOINTS" | cut -d, -f1)
run "curl -s -m 3 ${CTRL/https/http}/health || echo 'HTTP plano rechazado'"
INT=$(echo "$CTRL" | sed -E 's/:([0-9]+)$/:\1/' | awk -F: '{print $3+100}')
run "curl -s -m 3 --cacert \$DFSHA_CA https://127.0.0.1:$INT/metrics || echo 'sin certificado de cliente: handshake mTLS rechazado'"
pause

say "7. Alta disponibilidad (1/3): matar un DataNode en caliente"
run "cluster kill dn2"
run "dfsha get proyectos/videos/video.bin .demo/video.out2 && cmp .demo/video.bin .demo/video.out2 && echo 'lectura correcta con un DataNode caído (conmutación transparente)'"
note "esperando a que el cluster declare muerto a dn2 y re-replique sus bloques..."
for i in $(seq 1 90); do
  o=$(dfsha nodes 2>/dev/null)
  if echo "$o" | grep -q "dn2.*NO" && echo "$o" | grep -q "sub-replicados=0" && ! echo "$o" | grep -q "'replicated': 0"; then break; fi
  sleep 1
done
run "dfsha nodes | tail -12"
run "dfsha fsck"
pause

say "8. Alta disponibilidad (2/3): matar al LÍDER Raft en mitad de una subida"
run "dfsha nodes | head -8"
(sleep 0.4; cluster kill leader) &
run "dfsha put .demo/video.bin proyectos/videos/video_durante_failover.bin"
wait
run "dfsha get proyectos/videos/video_durante_failover.bin .demo/v3.out && cmp .demo/video.bin .demo/v3.out && echo 'subida completada pese a la caída del líder: contenido íntegro'"
run "dfsha nodes | head -8"
pause

say "9. Alta disponibilidad (3/3): corrupción silenciosa (bit rot) en el disco de un DataNode"
run "for f in .demo/data/dn3/blocks/*/*.blk; do printf '\\xff\\xff\\xff\\xff' | dd of=\$f bs=1 seek=100 conv=notrunc 2>/dev/null; done; echo \"bits corrompidos en \$(ls .demo/data/dn3/blocks/*/*.blk | wc -l | tr -d ' ') bloques de dn3\""
run "dfsha verify proyectos/videos/video.bin"
note "la lectura sigue siendo correcta: el cliente detecta el fallo (GCM + SHA-256) y usa otra réplica"
run "dfsha scrub dn3"
note "el DataNode puso los blobs en cuarentena y el cluster los re-replica desde réplicas sanas"
for i in $(seq 1 60); do if dfsha fsck 2>/dev/null | grep -q "sub-replicados=0 sobre-replicados=0 perdidos=0"; then break; fi; sleep 1; done
run "ls .demo/data/dn3/corrupt/ | head -3"
run "dfsha fsck"
pause

say "10. Consistencia: escritores concurrentes, versiones y commit atómico"
run ".venv/bin/python - <<'PY'
import os
from dfsha.client.sdk import Client
from dfsha.common import errors as E
ep, ca = os.environ['DFSHA_ENDPOINTS'].split(','), os.environ['DFSHA_CA']
c = Client(ep, ca, credentials=('admin','dfsha-admin-dev')); c.login('admin','dfsha-admin-dev')
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

say "11. Escalabilidad: añadir un DataNode en caliente (el balanceador migra réplicas hacia él)"
run "dfsha nodes | grep -E 'dn[0-9]' | head -8"
run ".venv/bin/python -c \"
from dfsha.cluster import LocalCluster
c = LocalCluster.load('.demo'); print('nuevo nodo:', c.add_data_node())\""
note "esperando al balanceador (copia bloques al nodo vacío sin reducir la redundancia)..."
for i in $(seq 1 90); do
  n=$(dfsha nodes 2>/dev/null | grep -E "dn6" | grep -oE "[0-9]+ +[0-9.]+ [KMG]iB" | head -1 | awk '{print $1}')
  [ "${n:-0}" -ge 12 ] 2>/dev/null && break
  sleep 1
done
run "dfsha nodes | grep -E 'dn[0-9]|bloques=' | head -9"
pause

say "12. Fin — resumen de estado y limpieza"
run "dfsha df && dfsha fsck"
run "cluster destroy"
