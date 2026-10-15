# Utilidades compartidas por los scripts de deploy/aws (se "source"-ean; no se ejecutan).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Variables opcionales en deploy/aws/.env (vea .env.example). Las ya exportadas en el terminal tienen prioridad.
load_env_file() {
  local f="$1" line k v
  [[ -f "$f" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*([A-Z_][A-Z0-9_]*)=(.*)$ ]] || continue
    k="${BASH_REMATCH[1]}"; v="${BASH_REMATCH[2]}"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    v="${v/#\~/$HOME}"
    [[ -n "${!k:-}" ]] || export "$k=$v"
  done < "$f"
}
load_env_file "$HERE/.env"
REPO="${DFSHA_REPO:-$(cd "$HERE/../.." && pwd)}"   # (DFSHA_REPO sólo lo usan las pruebas)
INV="${INVENTORY:-$HERE/inventory.txt}"
KEY_FILE="${KEY_FILE:-$HOME/labsuser.pem}"
SSH_USER="${SSH_USER:-ec2-user}"
# La región se toma de AWS_REGION (o AWS_DEFAULT_REGION); el AWS CLI v2 sólo lee AWS_DEFAULT_REGION, así que se exporta aquí:
# NO hace falta `aws configure set region` (que falla en el terminal web del Learner Lab: su HOME es de sólo lectura).
export AWS_DEFAULT_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"

die()  { echo "ERROR: $*" >&2; exit 1; }
note() { echo "==> $*"; }
need() { command -v "$1" >/dev/null 2>&1 || die "falta el comando '$1' ($2)"; }

ssh_opts() { echo "-i $KEY_FILE -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes"; }
# ssh_to <host> <comando...>
ssh_to() { local h="$1"; shift; ssh $(ssh_opts) "$SSH_USER@$h" "$@"; }
# ssh_n: igual que ssh_to pero SIN consumir stdin (obligatorio dentro de bucles `while read`)
ssh_n() { local h="$1"; shift; ssh -n $(ssh_opts) "$SSH_USER@$h" "$@"; }

# inventario: "rol nombre ip_privada dns_publico zona" (comentarios con #)
inv_lines() { [[ -f "$INV" ]] || die "no existe $INV (ejecute ./launch.sh o copie inventory.example)"; grep -v -E '^\s*(#|$)' "$INV"; }
# inv_field <nombre> <n>   (1=rol 2=nombre 3=ip_priv 4=dns_pub 5=zona)
inv_field() { inv_lines | awk -v n="$1" -v f="$2" '$2==n {print $f}'; }
inv_names() { inv_lines | awk '{print $2}'; }
