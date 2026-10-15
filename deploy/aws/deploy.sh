#!/usr/bin/env bash
# Copia el código + el bundle de cada VM, construye la imagen y arranca el contenedor (en paralelo).
#   KEY_FILE=~/labsuser.pem ./deploy.sh              # todas las VM
#   ONLY=c1,dn2 KEY_FILE=... ./deploy.sh             # sólo algunas (p. ej. tras un cambio de código)
# Es IDEMPOTENTE: se puede repetir; los datos (/opt/dfsha/data) y el estado Raft se conservan.
set -euo pipefail
source "$(dirname "$0")/common.sh"
cd "$HERE"
need rsync "brew install rsync"; need ssh "cliente SSH"; need curl "curl"
[[ -f "$KEY_FILE" ]] || die "no existe la clave SSH $KEY_FILE (KEY_FILE=...)"
[[ -d "$HERE/bundles" ]] || die "falta bundles/: ejecute primero ./gen-bundles.sh"
RSYNC_SSH="ssh $(ssh_opts)"
want() { [[ -z "${ONLY:-}" ]] || [[ ",$ONLY," == *",$1,"* ]]; }

deploy_one() {
  local role="$1" name="$2" pub="$4"
  local d="$HERE/bundles/$name"
  read -r api int cmd < "$d/ports"
  echo "==> $name ($role) @ $pub"
  local tries=0
  until ssh_to "$pub" "command -v docker >/dev/null && sudo systemctl is-active --quiet docker" 2>/dev/null; do
    tries=$((tries + 1)); (( tries > 60 )) && { echo "!! $name: Docker no está listo tras 5 min" >&2; return 1; }
    sleep 5
  done
  rsync -az --delete -e "$RSYNC_SSH" --exclude .git --exclude .venv --exclude .cluster --exclude .demo --exclude __pycache__ \
        --exclude bench/results --exclude report --exclude deploy/aws/state --exclude deploy/aws/bundles --exclude deploy/aws/evidence \
        "$REPO/" "$SSH_USER@$pub:/opt/dfsha/src/"
  ssh_to "$pub" "sudo mkdir -p /opt/dfsha/pki /opt/dfsha/secrets /opt/dfsha/data /opt/dfsha/results && sudo chown -R $SSH_USER /opt/dfsha"
  if [[ "$role" == client ]]; then
    rsync -az -e "$RSYNC_SSH" "$d/ca.pem" "$SSH_USER@$pub:/opt/dfsha/pki/ca.pem"
    rsync -az -e "$RSYNC_SSH" "$d/client.env" "$SSH_USER@$pub:/opt/dfsha/client.env"
    ssh_to "$pub" "cd /opt/dfsha/src && sudo docker build -q -t dfsha:latest -f docker/Dockerfile . >/dev/null && sudo chown -R 10001:10001 /opt/dfsha/results && echo 'imagen cliente lista'"
    return 0
  fi
  rsync -az -e "$RSYNC_SSH" "$d/ca.pem" "$d/$name.crt" "$d/$name.key" "$SSH_USER@$pub:/opt/dfsha/pki/"
  local mounts="-v /opt/dfsha/pki:/pki:ro -v /opt/dfsha/data:/data"
  if [[ "$role" == control ]]; then
    rsync -az -e "$RSYNC_SSH" "$d/cluster.key" "$d/admin.pass" "$SSH_USER@$pub:/opt/dfsha/secrets/"
    mounts="$mounts -v /opt/dfsha/secrets:/secrets:ro"
  fi
  rsync -az -e "$RSYNC_SSH" "$d/node.env" "$SSH_USER@$pub:/opt/dfsha/node.env"
  ssh_to "$pub" "bash -s" <<REMOTE
set -e
cd /opt/dfsha/src
sudo docker build -q -t dfsha:latest -f docker/Dockerfile . >/dev/null
sudo chown -R 10001:10001 /opt/dfsha/pki /opt/dfsha/data
sudo chmod 600 /opt/dfsha/pki/*.key
if [ -d /opt/dfsha/secrets ] && ls /opt/dfsha/secrets/* >/dev/null 2>&1; then sudo chown -R 10001:10001 /opt/dfsha/secrets; sudo chmod 600 /opt/dfsha/secrets/*; fi
sudo docker rm -f dfsha-node >/dev/null 2>&1 || true
sudo docker run -d --name dfsha-node --restart unless-stopped --init \
  --log-opt max-size=10m --log-opt max-file=3 --cap-drop ALL --security-opt no-new-privileges:true \
  --env-file /opt/dfsha/node.env $mounts -p $api:$api -p $int:$int dfsha:latest $cmd >/dev/null
sleep 3
sudo docker ps --filter name=dfsha-node --format '   contenedor: {{.Status}}'
REMOTE
}

pids=(); names=()
while read -r role name priv pub az; do
  want "$name" || continue
  deploy_one "$role" "$name" "$priv" "$pub" &
  pids+=($!); names+=("$name")
done < <(inv_lines)
rc=0
for i in "${!pids[@]}"; do wait "${pids[$i]}" || { echo "!! falló el despliegue de ${names[$i]}" >&2; rc=1; }; done
(( rc == 0 )) || die "hubo nodos con error (revise arriba y repita: ONLY=<nodo> ./deploy.sh)"

note "Comprobación de salud desde esta máquina (HTTPS verificado contra la CA)"
bad=0
while read -r role name priv pub az; do
  [[ "$role" == client ]] && continue
  want "$name" || continue
  read -r api int cmd < "$HERE/bundles/$name/ports"
  ok=0
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 4 --cacert "$HERE/client/ca.pem" "https://$pub:$api/health" >/tmp/dfsha_health.$$ 2>/dev/null; then ok=1; break; fi
    sleep 2
  done
  if (( ok )); then echo "  ✓ $name  $(cat /tmp/dfsha_health.$$)"; else echo "  ✗ $name NO responde en https://$pub:$api/health"; bad=1; fi
done < <(inv_lines)
rm -f /tmp/dfsha_health.$$
(( bad == 0 )) || die "algún nodo no responde: ./node.sh logs <nodo>   y revise el security group / las claves"
echo
echo "Desplegado. Desde su portátil:  source deploy/aws/client/env.sh && dfsha login -u admin && dfsha nodes"
