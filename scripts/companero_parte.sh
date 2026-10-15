#!/usr/bin/env bash
# Ejecuta SU parte de la demostración desde el computador de un compañero:
#     bash scripts/companero_parte.sh 2,3,5          (los números de paso se los dice Juan)
# Muestra los comandos en pantalla uno a uno; pulse Enter para pasar al siguiente. Para un ensayo sin pausas añada --auto.
set -euo pipefail
cd "$(dirname "$0")/.."
PASOS="${1:?uso: bash scripts/companero_parte.sh 2,3,5 [--auto]}"; shift || true
CLIENTE="${DFSHA_CLIENTE:-$PWD/../dfsha-cliente}"
[[ -f "$CLIENTE/env.sh" ]] || { echo "✗ No encuentro ../dfsha-cliente/env.sh (¿corrió antes companero_inicio.sh?)"; exit 1; }
command -v uv >/dev/null 2>&1 || export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
# shellcheck disable=SC1091
source "$CLIENTE/env.sh"
if [[ -z "${DFSHA_PASSWORD:-}" ]]; then read -r -s -p "Contraseña de admin: " DFSHA_PASSWORD; echo; fi
export DFSHA_PASSWORD SIZE_MIB="${SIZE_MIB:-32}" DEMO_DIR="${DEMO_DIR:-demo-$(whoami | tr -cd 'a-z0-9')}"
rm -rf .demo-cloud
exec bash scripts/demo_cloud.sh --only "$PASOS" "$@"
