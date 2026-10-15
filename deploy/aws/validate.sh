#!/usr/bin/env bash
# Validación completa del despliegue + EVIDENCIA para el informe (deploy/aws/evidence/<fecha>/).
#   DFSHA_PASSWORD='...' KEY_FILE=~/labsuser.pem ./validate.sh [--size-mib 64]
# Incluye caos real: detiene el contenedor del líder y de un DataNode (los vuelve a arrancar al terminar).
# Ejecútelo desde SU PORTÁTIL (así también se comprueba que los puertos internos no son alcanzables desde Internet).
set -euo pipefail
source "$(dirname "$0")/common.sh"
: "${DFSHA_PASSWORD:?defina DFSHA_PASSWORD (clave de admin)}"
[[ -f "$HERE/client/env.sh" ]] || die "falta client/env.sh: ejecute ./gen-bundles.sh"
# shellcheck disable=SC1091
source "$HERE/client/env.sh"
cd "$REPO"
exec uv run dfsha-cloud validate --inventory "$INV" --ssh-key "$KEY_FILE" --ssh-user "$SSH_USER" --out "$HERE/evidence/$(date +%Y%m%d-%H%M%S)" "$@"
