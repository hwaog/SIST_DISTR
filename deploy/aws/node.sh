#!/usr/bin/env bash
# Operar un nodo por SSH sin recordar su DNS.
#   ./node.sh kill  c1        # SIGKILL al contenedor  (caída abrupta: lo que se muestra en el video)
#   ./node.sh stop  dn2       # parada ordenada (SIGTERM)
#   ./node.sh start dn2       # volver a arrancarlo
#   ./node.sh logs  c1 [N]    # últimas N líneas del log (por defecto 40)
#   ./node.sh ps    c1        # estado del contenedor
#   ./node.sh shell dn1       # sesión SSH interactiva
#   ./node.sh blk   dn1       # primeros bytes de un bloque en disco (cifrado en reposo)
#   ./node.sh leader          # nombre del líder Raft actual
set -euo pipefail
source "$(dirname "$0")/common.sh"
cmd="${1:-}"; name="${2:-}"; arg="${3:-40}"
if [[ "$cmd" == leader ]]; then
  while read -r role n priv pub az; do
    [[ "$role" == control ]] || continue
    api=$(( 8440 + ${n#c} ))
    out=$(curl -fsS --max-time 4 --cacert "$HERE/client/ca.pem" "https://$pub:$api/v1/node" 2>/dev/null </dev/null || true)
    [[ "$out" == *'"role":"leader"'* ]] && { echo "$n"; exit 0; }
  done < <(inv_lines)
  die "no se encontró líder (¿elección en curso?)"
fi
[[ -n "$cmd" && -n "$name" ]] || die "uso: ./node.sh {kill|stop|start|restart|logs|ps|shell|blk} <nodo>   |   ./node.sh leader"
host=$(inv_field "$name" 4); [[ -n "$host" ]] || die "'$name' no está en $INV"
case "$cmd" in
  kill)    ssh_n "$host" "sudo docker kill dfsha-node"  && echo "$name: contenedor terminado (SIGKILL)";;
  stop)    ssh_n "$host" "sudo docker stop dfsha-node"  && echo "$name: detenido";;
  start)   ssh_n "$host" "sudo docker start dfsha-node" && echo "$name: arrancado";;
  restart) ssh_n "$host" "sudo docker restart dfsha-node" && echo "$name: reiniciado";;
  logs)    ssh_n "$host" "sudo docker logs --tail $arg dfsha-node 2>&1";;
  ps)      ssh_n "$host" "sudo docker ps -a --filter name=dfsha-node --format '{{.Names}}  {{.Status}}  {{.Ports}}'";;
  shell)   ssh_to "$host";;
  blk)     ssh_n "$host" "f=\$(sudo find /opt/dfsha/data/blocks -name '*.blk' | head -1); echo \"\$f\"; sudo xxd \"\$f\" | head -6";;
  *) die "comando desconocido: $cmd";;
esac
