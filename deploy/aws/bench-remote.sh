#!/usr/bin/env bash
# Ejecuta los benchmarks en la VM CLIENTE (mismo VPC) y trae los resultados a bench/results_cloud/.
#   DFSHA_PASSWORD='...' KEY_FILE=~/labsuser.pem ./bench-remote.sh [scaling|throughput|metadata|all]
# Medir desde su portátil mediría su conexión doméstica, no el sistema: por eso se usa la VM cliente.
set -euo pipefail
source "$(dirname "$0")/common.sh"
kind="${1:-all}"
: "${DFSHA_PASSWORD:?defina DFSHA_PASSWORD}"
client=$(inv_lines | awk '$1=="client"{print $2; exit}'); [[ -n "$client" ]] || die "el inventario no tiene VM cliente (lance con N_CLIENT=1)"
host=$(inv_field "$client" 4)
note "Benchmarks '$kind' desde $client ($host)"
ssh_n "$host" "sudo docker run --rm --network host --env-file /opt/dfsha/client.env -e DFSHA_PASSWORD=$(printf %q "$DFSHA_PASSWORD") \
  -v /opt/dfsha/pki:/pki:ro -v /opt/dfsha/results:/results dfsha:latest dfsha-cloud bench $kind --ca /pki/ca.pem --out /results --size-mib ${SIZE_MIB:-256} --reps ${REPS:-3}"
mkdir -p "$REPO/bench/results_cloud"
rsync -az -e "ssh $(ssh_opts)" "$SSH_USER@$host:/opt/dfsha/results/" "$REPO/bench/results_cloud/"
echo "Resultados en bench/results_cloud/  (ejecute luego:  uv run python report/cloud_section.py)"
