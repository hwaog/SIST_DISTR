#!/usr/bin/env bash
# Termina TODAS las instancias de DFSha, libera IP elásticas y borra el security group.  ¡DESTRUYE LOS DATOS!
#   ./teardown.sh          pide confirmación         ./teardown.sh --yes   sin preguntar
set -euo pipefail
source "$(dirname "$0")/common.sh"
export AWS_DEFAULT_REGION="${AWS_REGION:-us-east-1}"
need aws "instale AWS CLI v2"
IDS=$(aws ec2 describe-instances --filters "Name=tag-key,Values=dfsha-role" "Name=instance-state-name,Values=pending,running,stopped,stopping" \
      --query 'Reservations[].Instances[].InstanceId' --output text)
echo "Instancias a terminar: ${IDS:-ninguna}"
if [[ "${1:-}" != "--yes" ]]; then read -r -p "¿Seguro? Se perderán los datos (y/N) " a; [[ "$a" == y ]] || exit 1; fi
if [[ -n "$IDS" ]]; then
  aws ec2 terminate-instances --instance-ids $IDS >/dev/null
  aws ec2 wait instance-terminated --instance-ids $IDS
fi
for alloc in $(aws ec2 describe-addresses --filters "Name=tag:dfsha,Values=1" --query 'Addresses[].AllocationId' --output text); do
  aws ec2 release-address --allocation-id "$alloc" || true
done
SG=$(aws ec2 describe-security-groups --filters Name=group-name,Values=dfsha-sg --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo None)
if [[ "$SG" != None && -n "$SG" ]]; then
  for _ in 1 2 3 4 5 6; do aws ec2 delete-security-group --group-id "$SG" 2>/dev/null && break || sleep 10; done
fi
rm -f "$HERE/inventory.txt"
echo "Recursos de DFSha eliminados. (state/ conserva la CA y el secreto maestro por si desea reutilizarlos; bórrelo si no.)"
