#!/usr/bin/env bash
# Crea el security group y las instancias EC2 de DFSha.   Escribe ./inventory.txt
#   N_CONTROL=3 N_DATA=4 N_CLIENT=1 ALLOW_CIDR="$(curl -s https://checkip.amazonaws.com)/32" ./launch.sh
set -euo pipefail
source "$(dirname "$0")/common.sh"
cd "$HERE"

REGION="${AWS_REGION:-us-east-1}"
KEY_NAME="${KEY_NAME:-vockey}"             # par de claves que provee AWS Academy
N_CONTROL="${N_CONTROL:-3}"
N_DATA="${N_DATA:-4}"
N_CLIENT="${N_CLIENT:-1}"                  # VM cliente DENTRO del VPC: necesaria para medir sin el cuello del uplink doméstico
TYPE="${INSTANCE_TYPE:-t3.small}"
CLIENT_TYPE="${CLIENT_TYPE:-t3.medium}"
DATA_DISK_GB="${DATA_DISK_GB:-30}"
ALLOW_CIDR="${ALLOW_CIDR:-0.0.0.0/0}"      # RESTRINJA a su IP/32 (SSH y APIs públicas)
VOLUME_TYPE="${VOLUME_TYPE:-gp3}"         # gp2 si su laboratorio no permite gp3
USE_EIP="${USE_EIP:-0}"                    # 1 = IP elástica (estable; el límite por región suele ser 5)
export AWS_DEFAULT_REGION="$REGION"
need aws "instale AWS CLI v2"
aws sts get-caller-identity >/dev/null 2>&1 || die "credenciales de AWS ausentes o vencidas (vea ./preflight.sh)"

if aws ec2 describe-instances --filters "Name=tag-key,Values=dfsha-role" "Name=instance-state-name,Values=pending,running,stopped,stopping" \
     --query 'Reservations[].Instances[].InstanceId' --output text | grep -q 'i-'; then
  die "ya existen instancias de DFSha. Use ./refresh-inventory.sh --start para reutilizarlas o ./teardown.sh para empezar de cero"
fi

note "AMI Amazon Linux 2023 más reciente"
AMI=$(aws ec2 describe-images --owners amazon \
  --filters "Name=name,Values=al2023-ami-2023*-x86_64" "Name=state,Values=available" \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNETS=()
while read -r sn; do [[ -n "$sn" ]] && SUBNETS+=("$sn"); done < <(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" "Name=default-for-az,Values=true" \
  --query 'Subnets[].SubnetId' --output text | tr '\t' '\n')
(( ${#SUBNETS[@]} >= 2 )) || die "se necesitan ≥2 subredes (zonas de disponibilidad) en el VPC por defecto; encontradas: ${#SUBNETS[@]}"
echo "    AMI=$AMI VPC=$VPC subredes=${#SUBNETS[@]} (se reparte una instancia por zona, rotando)"

note "Security group"
SG=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=dfsha-sg" "Name=vpc-id,Values=$VPC" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)
if [[ -z "$SG" || "$SG" == "None" ]]; then
  SG=$(aws ec2 create-security-group --group-name dfsha-sg --description "DFSha" --vpc-id "$VPC" --query GroupId --output text)
  # Públicos: SSH + APIs de clientes (ControlNodes 8441-8449, DataNodes 7441-7449)
  aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 22 --cidr "$ALLOW_CIDR" >/dev/null
  aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 8441-8449 --cidr "$ALLOW_CIDR" >/dev/null
  aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 7441-7449 --cidr "$ALLOW_CIDR" >/dev/null
  # Los miembros del propio grupo (p. ej. la VM cliente de los benchmarks) también usan SSH y las APIs públicas, pero
  # desde su IP PRIVADA (el DNS público se resuelve a la IP privada dentro del VPC), que no está en ALLOW_CIDR.
  aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 22 --source-group "$SG" >/dev/null
  aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 8441-8449 --source-group "$SG" >/dev/null
  aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 7441-7449 --source-group "$SG" >/dev/null
  # Internos (mTLS): SOLO entre miembros del propio grupo
  aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 9441-9449 --source-group "$SG" >/dev/null
  aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 7541-7549 --source-group "$SG" >/dev/null
fi
echo "    SG=$SG"

USERDATA=$(mktemp)
cat > "$USERDATA" <<'UD'
#!/bin/bash
dnf install -y docker rsync
systemctl enable --now docker
usermod -aG docker ec2-user
mkdir -p /opt/dfsha && chown ec2-user /opt/dfsha
UD

launch_one() {  # role name idx type disk
  local role="$1" name="$2" idx="$3" itype="$4" disk="$5"
  local subnet="${SUBNETS[$(( (idx - 1) % ${#SUBNETS[@]} ))]}"
  aws ec2 run-instances --image-id "$AMI" --instance-type "$itype" --key-name "$KEY_NAME" \
    --security-group-ids "$SG" --subnet-id "$subnet" --associate-public-ip-address \
    --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=$disk,VolumeType=$VOLUME_TYPE}" \
    --user-data "file://$USERDATA" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=dfsha-$name},{Key=dfsha-role,Value=$role},{Key=dfsha-name,Value=$name}]" \
    --query 'Instances[0].InstanceId' --output text
}

note "Lanzando $N_CONTROL ControlNodes, $N_DATA DataNodes y $N_CLIENT cliente(s)"
IDS=()
for i in $(seq 1 "$N_CONTROL"); do IDS+=("$(launch_one control "c$i" "$i" "$TYPE" 10)"); done
for j in $(seq 1 "$N_DATA");    do IDS+=("$(launch_one data "dn$j" "$j" "$TYPE" "$DATA_DISK_GB")"); done
for k in $(seq 1 "$N_CLIENT");  do [[ "$N_CLIENT" -gt 0 ]] && IDS+=("$(launch_one client "cl$k" "$k" "$CLIENT_TYPE" 20)"); done
echo "    ${IDS[*]}"
note "Esperando a que arranquen y pasen los chequeos de estado (≈2-3 min)"
aws ec2 wait instance-running --instance-ids "${IDS[@]}"
aws ec2 wait instance-status-ok --instance-ids "${IDS[@]}"

if [[ "$USE_EIP" == "1" ]]; then
  note "Asignando IP elástica a cada instancia"
  for id in "${IDS[@]}"; do
    alloc=$(aws ec2 allocate-address --domain vpc --tag-specifications 'ResourceType=elastic-ip,Tags=[{Key=dfsha,Value=1}]' --query AllocationId --output text)
    aws ec2 associate-address --instance-id "$id" --allocation-id "$alloc" >/dev/null
  done
  sleep 5
fi

note "Inventario -> inventory.txt   (rol nombre ip_privada dns_publico zona)"
aws ec2 describe-instances --instance-ids "${IDS[@]}" \
  --query 'Reservations[].Instances[].[Tags[?Key==`dfsha-role`]|[0].Value, Tags[?Key==`dfsha-name`]|[0].Value, PrivateIpAddress, PublicDnsName, Placement.AvailabilityZone]' \
  --output text | sort -k1,1 -k2,2 > inventory.txt
cat inventory.txt
rm -f "$USERDATA"
echo
echo "Siguiente paso:  ADMIN_PASSWORD='una-clave-larga' ./gen-bundles.sh && KEY_FILE=$KEY_FILE ./deploy.sh"
