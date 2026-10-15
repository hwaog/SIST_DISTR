#!/usr/bin/env bash
# PRIMER USO en el computador de un compañero (una sola vez):  bash scripts/companero_inicio.sh
# Instala lo que falte, se conecta al cluster de AWS y muestra los nodos. Si ve «TODO LISTO», ya puede grabar su parte.
set -euo pipefail
cd "$(dirname "$0")/.."
CLIENTE="${DFSHA_CLIENTE:-$PWD/../dfsha-cliente}"
if [[ ! -f "$CLIENTE/env.sh" ]]; then
  echo "✗ No encuentro la carpeta 'dfsha-cliente' junto a esta carpeta."
  echo "  Descomprima el zip COMPLETO que le mandó Juan: deben quedar lado a lado 'dfsha' y 'dfsha-cliente'."; exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "→ Instalando 'uv' (una sola vez, tarda ~30 s)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1 || { echo "✗ Cierre este terminal, ábralo de nuevo y repita el comando."; exit 1; }
fi
echo "→ Preparando el programa (la primera vez tarda 1-2 min)…"
uv sync --inexact -q
# shellcheck disable=SC1091
source "$CLIENTE/env.sh"
if [[ -z "${DFSHA_PASSWORD:-}" ]]; then read -r -s -p "Contraseña de admin (se la dio Juan): " DFSHA_PASSWORD; echo; fi
export DFSHA_PASSWORD
echo "→ Conectando con el cluster en AWS…"
if uv run dfsha login -u "${DFSHA_USER:-admin}" -p "$DFSHA_PASSWORD" && uv run dfsha nodes; then
  echo; echo "✔ TODO LISTO. Para grabar su parte use:  bash scripts/companero_parte.sh PASOS   (Juan le dice cuáles)"
else
  MI_IP=$(curl -s --max-time 8 https://checkip.amazonaws.com || echo "?")
  echo; echo "✗ No se pudo conectar. Lo más probable: su internet actual no está autorizado en AWS."
  echo "  Mándele a Juan este número:  $MI_IP   (y avísele si cambia de wifi). Si el error dice 'contraseña', revise la clave."
  exit 1
fi
