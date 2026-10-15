#!/usr/bin/env bash
# Prepara lo que necesita un COMPAÑERO de equipo para usar el cluster desde OTRO dispositivo.
#
#   ./share-client.sh IP_DEL_COMPAÑERO [IP2 ...]          abre las APIs a esas IP y genera share/dfsha-cliente.zip
#   ./share-client.sh --with-ssh IP [IP2 ...]             además incluye el «kit de operador» (inventario + clave SSH)
#   ./share-client.sh                                     sólo regenera el zip (sin tocar el security group)
#
# Cada IP es la PÚBLICA del compañero (que ejecute:  curl https://checkip.amazonaws.com ); se admite 1.2.3.4 o 1.2.3.4/32.
# Se abren SÓLO los puertos de API de clientes (8441-8449, 7441-7449); NUNCA el SSH (22). El zip NO contiene contraseñas:
# la clave de admin se la pasa usted por un canal privado.  Si cambian de red (otro wifi), repitan el comando con la IP nueva.
set -euo pipefail
source "$(dirname "$0")/common.sh"
WITH_SSH=0; CIDRS=()
for a in "$@"; do
  case "$a" in
    --with-ssh) WITH_SSH=1 ;;
    -h|--help) sed -n 2,11p "$0"; exit 0 ;;
    *) [[ "$a" == */* ]] || a="$a/32"
       [[ "$a" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || die "IP/CIDR no válido: $a"
       CIDRS+=("$a") ;;
  esac
done
[[ -f "$HERE/client/ca.pem" && -f "$HERE/client/env.sh" ]] || die "faltan client/ca.pem y client/env.sh (ejecute antes ./gen-bundles.sh)"

if ((${#CIDRS[@]})); then
  need aws "instale AWS CLI v2"
  aws sts get-caller-identity >/dev/null 2>&1 || die "AWS no acepta sus credenciales (vencieron o no están cargadas en ESTE terminal). Copie de nuevo las 3 líneas de AWS Details → AWS CLI → Show y repita"
  SG=$(aws ec2 describe-security-groups --filters Name=group-name,Values=dfsha-sg --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)
  [[ -n "$SG" && "$SG" != None ]] || die "no existe el security group dfsha-sg en la región $AWS_DEFAULT_REGION (¿otra región? ¿se borró el despliegue con teardown.sh?)"
  for cidr in "${CIDRS[@]}"; do
    [[ "$cidr" == 0.0.0.0/0 ]] && echo "AVISO: 0.0.0.0/0 abre las APIs a todo Internet (siguen protegidas por TLS + autenticación). Ciérrelo al terminar."
    for port in 8441-8449 7441-7449; do
      if err=$(aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port "$port" --cidr "$cidr" 2>&1 >/dev/null); then
        note "abierto $port para $cidr"
      elif [[ "$err" == *Duplicate* ]]; then
        note "$port para $cidr: ya estaba abierto"
      else
        die "no se pudo abrir $port para $cidr: $err"
      fi
    done
  done
fi

OUT="$HERE/share"; D="$OUT/dfsha-cliente"
rm -rf "$D"; mkdir -p "$D"
cp "$HERE/client/ca.pem" "$D/ca.pem"
ENDPOINTS=$(sed -n "s/^export DFSHA_ENDPOINTS=//p" "$HERE/client/env.sh")
[[ -n "$ENDPOINTS" ]] || die "no se encontró DFSHA_ENDPOINTS en client/env.sh"
cat > "$D/env.sh" <<ENV
# Uso:  source env.sh      (desde cualquier carpeta; localiza ca.pem junto a este archivo)
export DFSHA_ENDPOINTS=$ENDPOINTS
export DFSHA_CA="\$(cd "\$(dirname "\${BASH_SOURCE[0]:-\$0}")" && pwd)/ca.pem"
export DFSHA_USER="\${DFSHA_USER:-admin}"
# La contraseña NO está aquí: exporte  DFSHA_PASSWORD='...'  (se la da el operador por un canal privado)
ENV
cat > "$D/LEEME.txt" <<'TXT'
DFSha — acceso al cluster desde otro dispositivo
=================================================
1. Requisitos (una vez):  git, uv (https://docs.astral.sh/uv/)  — macOS, Linux o Windows con WSL2.
2. git clone <URL del repositorio> && cd <repo> && uv sync
3. Descomprima este zip en cualquier carpeta y, en SU terminal:
       source /ruta/a/dfsha-cliente/env.sh
       export DFSHA_PASSWORD='...'          # la que le dio el operador
       uv run dfsha login -u "$DFSHA_USER" -p "$DFSHA_PASSWORD"
       uv run dfsha nodes                   # debe verse 1 líder, 2 seguidores y los DataNodes
4. Su IP pública debe haber sido autorizada por el operador (./share-client.sh SU_IP). Si dice "timed out", es eso:
   cambie de red o pídale que repita el comando con su IP actual (curl https://checkip.amazonaws.com).
TXT
if ((WITH_SSH)); then
  [[ -f "$INV" ]] || die "no existe el inventario $INV"; [[ -f "$KEY_FILE" ]] || die "no existe la clave SSH $KEY_FILE"
  mkdir -p "$D/operador"
  cp "$INV" "$D/operador/inventory.txt"; cp "$KEY_FILE" "$D/operador/labsuser.pem"; chmod 400 "$D/operador/labsuser.pem"
  cat > "$D/operador/instalar.sh" <<'SH'
#!/usr/bin/env bash
# Instala el kit de operador en su clon del repositorio:   ./instalar.sh /ruta/al/repositorio
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="${1:?uso: ./instalar.sh /ruta/al/repositorio}"
AWS="$REPO/deploy/aws"; [[ -d "$AWS" ]] || { echo "no encuentro $AWS"; exit 1; }
mkdir -p "$AWS/client"
cp "$HERE/inventory.txt" "$AWS/inventory.txt"
cp "$HERE/../ca.pem" "$AWS/client/ca.pem"; cp "$HERE/../env.sh" "$AWS/client/env.sh"
install -m 400 "$HERE/labsuser.pem" "$HOME/labsuser.pem"
echo "Listo. Pruebe:  cd $REPO && source deploy/aws/client/env.sh && KEY_FILE=~/labsuser.pem deploy/aws/node.sh ps dn1"
echo "Borre ~/labsuser.pem cuando termine el video."
SH
  chmod +x "$D/operador/instalar.sh"
  echo "AVISO: el zip incluye la CLAVE SSH del laboratorio: envíelo por un canal privado y bórrelo al terminar."
fi
rm -f "$OUT/dfsha-cliente.zip"
need zip "instale zip"
(cd "$OUT" && zip -qr dfsha-cliente.zip dfsha-cliente)
# Paquete TODO-EN-UNO para quien no usa git: el proyecto (lo ya commiteado) + dfsha-cliente, lado a lado
if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  P="$OUT/paquete-companero"; rm -rf "$P" "$OUT/paquete-companero.zip"; mkdir -p "$P"
  git -C "$REPO" archive --prefix=dfsha/ HEAD | tar -x -C "$P"
  cp -R "$D" "$P/dfsha-cliente"
  cat > "$P/LEEME.txt" <<'TXT'
PASO A PASO (2 comandos):
 1. Descomprima este zip. Debe quedar una carpeta con DOS carpetas dentro: «dfsha» y «dfsha-cliente».
 2. Abra el programa «Terminal». Escriba  cd  (con un espacio), arrastre la carpeta «dfsha» a la ventana y pulse Enter.
 3. Escriba y pulse Enter:      bash scripts/companero_inicio.sh
    (le pedirá la contraseña que le dio Juan; al final debe decir «TODO LISTO»)
 4. Cuando grabe su parte, en esa misma carpeta:      bash scripts/companero_parte.sh PASOS
    (Juan le dice PASOS, por ejemplo 2,3,5)
Si algo falla, haga captura del mensaje y mándelo al grupo.
TXT
  (cd "$OUT" && zip -qr paquete-companero.zip paquete-companero)
  note "para los compañeros: $OUT/paquete-companero.zip ($(du -h "$OUT/paquete-companero.zip" | cut -f1)) — contiene lo YA commiteado: haga commit antes si cambió algo"
fi
note "listo: $OUT/dfsha-cliente.zip ($(du -h "$OUT/dfsha-cliente.zip" | cut -f1))$([[ $WITH_SSH == 1 ]] && echo ' · incluye kit de operador')"
