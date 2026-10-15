#!/usr/bin/env bash
# Reconstruye inventory.txt a partir de las etiquetas de EC2 (útil tras detener/reiniciar el laboratorio, cuando los
# DNS públicos cambian).  Con --start arranca antes las instancias detenidas.
#
#   ./refresh-inventory.sh --start && ./gen-bundles.sh && KEY_FILE=~/labsuser.pem ./deploy.sh
#
# Los datos NO se pierden: viven en el disco EBS (/opt/dfsha/data) y gen-bundles.sh conserva la CA y el secreto maestro.
set -euo pipefail
source "$(dirname "$0")/common.sh"
cd "$HERE"
export AWS_DEFAULT_REGION="${AWS_REGION:-us-east-1}"
need aws "instale AWS CLI v2"
if [[ "${1:-}" == "--start" ]]; then
  stopped=$(aws ec2 describe-instances --filters "Name=tag-key,Values=dfsha-role" "Name=instance-state-name,Values=stopped" \
            --query 'Reservations[].Instances[].InstanceId' --output text)
  if [[ -n "$stopped" ]]; then
    note "Arrancando instancias detenidas: $stopped"
    aws ec2 start-instances --instance-ids $stopped >/dev/null
    aws ec2 wait instance-running --instance-ids $stopped
    aws ec2 wait instance-status-ok --instance-ids $stopped
  fi
fi
aws ec2 describe-instances --filters "Name=tag-key,Values=dfsha-role" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].[Tags[?Key==`dfsha-role`]|[0].Value, Tags[?Key==`dfsha-name`]|[0].Value, PrivateIpAddress, PublicDnsName, Placement.AvailabilityZone]' \
  --output text | sort -k1,1 -k2,2 > inventory.txt.new
[[ -s inventory.txt.new ]] || { rm -f inventory.txt.new; die "no hay instancias de DFSha en ejecución (¿laboratorio detenido? use --start)"; }
mv inventory.txt.new inventory.txt
cat inventory.txt
echo "Siguiente: ./gen-bundles.sh && KEY_FILE=$KEY_FILE ./deploy.sh"
