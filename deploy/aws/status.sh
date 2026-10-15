#!/usr/bin/env bash
# Estado de todos los contenedores y del cluster.   ./status.sh
set -uo pipefail
source "$(dirname "$0")/common.sh"
printf '%-6s %-8s %-46s %s\n' ROL NODO DNS CONTENEDOR
while read -r role name priv pub az; do
  st=$(ssh_n "$pub" "sudo docker ps -a --filter name=dfsha-node --format '{{.Status}}'" 2>/dev/null </dev/null || echo "SIN CONEXIÓN SSH")
  [[ "$role" == client ]] && st="(cliente: imagen en /opt/dfsha)"
  printf '%-6s %-8s %-46s %s\n' "$role" "$name" "$pub" "${st:-sin contenedor}"
done < <(inv_lines)
echo
if [[ -f "$HERE/client/env.sh" ]] && command -v uv >/dev/null; then
  # shellcheck disable=SC1091
  source "$HERE/client/env.sh"
  (cd "$REPO" && uv run dfsha nodes) || echo "(inicie sesión primero:  source deploy/aws/client/env.sh && uv run dfsha login -u admin)"
fi
