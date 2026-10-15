#!/usr/bin/env bash
# Comprueba TODO lo necesario ANTES de gastar créditos.   Uso: ./preflight.sh
set -uo pipefail
source "$(dirname "$0")/common.sh"
fail=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; fail=1; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

note "Herramientas locales"
for c in aws ssh rsync curl uv; do command -v "$c" >/dev/null 2>&1 && ok "$c" || bad "falta '$c'"; done
command -v bash >/dev/null && ok "bash $BASH_VERSION"

note "Clave SSH ($KEY_FILE)"
if [[ -f "$KEY_FILE" ]]; then
  perm=$(stat -f '%Lp' "$KEY_FILE" 2>/dev/null || stat -c '%a' "$KEY_FILE")
  [[ "$perm" == "400" || "$perm" == "600" ]] && ok "permisos $perm" || bad "permisos $perm: ejecute  chmod 400 $KEY_FILE"
else
  bad "no existe. En AWS Academy: Learner Lab → «AWS Details» → «Download PEM» y guárdela como ~/labsuser.pem"
fi

note "Credenciales y región de AWS"
if command -v aws >/dev/null 2>&1; then
  if id=$(aws sts get-caller-identity --output text --query 'Arn' 2>/dev/null); then
    ok "credenciales válidas: $id"
  else
    bad "credenciales ausentes o vencidas. Learner Lab → «AWS Details» → «AWS CLI: Show» y pegue las 3 líneas en ~/.aws/credentials (caducan al cerrar el laboratorio)"
  fi
  echo "  región: $AWS_DEFAULT_REGION"
  if aws ec2 describe-key-pairs --key-names "${KEY_NAME:-vockey}" >/dev/null 2>&1; then ok "key pair '${KEY_NAME:-vockey}' existe"; else bad "no existe el key pair '${KEY_NAME:-vockey}' en esta región"; fi
  n=$(aws ec2 describe-instances --filters Name=instance-state-name,Values=pending,running --query 'length(Reservations[].Instances[])' --output text 2>/dev/null || echo "?")
  echo "  instancias en ejecución ahora: $n   (el Learner Lab suele permitir ≤ 9 y ≤ 32 vCPU)"
  [[ "$n" =~ ^[0-9]+$ ]] && (( n + ${N_CONTROL:-3} + ${N_DATA:-4} + ${N_CLIENT:-1} > 9 )) && warn "lanzaría $((n + ${N_CONTROL:-3} + ${N_DATA:-4} + ${N_CLIENT:-1})) instancias: podría exceder el límite de 9 (reduzca N_DATA)"
fi

note "Proyecto local"
[[ -d "$REPO/.venv" ]] && ok "entorno uv sincronizado" || warn "ejecute 'uv sync' en la raíz del repo"
[[ -f "$REPO/docker/Dockerfile" ]] && ok "docker/Dockerfile" || bad "falta docker/Dockerfile"
if [[ -f "$HERE/inventory.txt" ]]; then ok "inventory.txt presente"; else warn "aún no hay inventory.txt (lo crea ./launch.sh)"; fi
echo
(( fail )) && { echo "Hay problemas: corríjalos antes de continuar."; exit 1; } || echo "Todo listo para ./launch.sh"
