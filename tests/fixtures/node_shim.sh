#!/usr/bin/env bash
# Sustituye a deploy/aws/node.sh en las pruebas locales: opera sobre un LocalCluster (DEMO_CLUSTER_DIR).
set -euo pipefail
cmd="${1:-}"; name="${2:-}"
CL=("$DFSHA_VENV_BIN/dfsha-cluster" --dir "$DEMO_CLUSTER_DIR")
case "$cmd" in
  kill|stop) "${CL[@]}" kill "$name" ;;
  start|restart) "${CL[@]}" start "$name" ;;
  leader) "${CL[@]}" status | awk '/\(líder\)/{print $2; exit}' ;;
  blk) f=$(ls "$DEMO_CLUSTER_DIR"/data/"$name"/blocks/*/*.blk | head -1); echo "$f"; xxd "$f" | head -4 ;;
  ps|logs) echo "$name: (simulado)" ;;
  *) echo "comando no soportado por el shim: $cmd" >&2; exit 2 ;;
esac
