#!/usr/bin/env bash
# Genera (o REUTILIZA) la CA y el secreto maestro, emite un certificado por nodo con los SAN reales del inventario
# y prepara lo que recibirá cada VM.
#
#   ADMIN_PASSWORD='clave-larga' ./gen-bundles.sh [inventory.txt]       ->  ./bundles/<nodo>/   ./client/env.sh
#
# IMPORTANTE — estado persistente en ./state/ (ignorado por git):
#   state/pki/ca.{pem,key}  y  state/cluster.key  se crean UNA sola vez.  Volver a ejecutar este script (p. ej. tras
#   reiniciar el laboratorio, cuando cambian los DNS públicos) sólo re-emite los certificados.  Si se regeneraran
#   cluster.key o la CA, los archivos ya guardados serían ILEGIBLES (sus claves de datos están envueltas con ese secreto).
set -euo pipefail
source "$(dirname "$0")/common.sh"
cd "$HERE"
INV="${1:-$INV}"
STATE="$HERE/state"; OUT="$HERE/bundles"
mkdir -p "$STATE/pki"; chmod 700 "$STATE" "$STATE/pki"
PKI_CLI=(uv run --project "$REPO" dfsha-pki)

if [[ ! -f "$STATE/admin.pass" ]]; then
  : "${ADMIN_PASSWORD:?primera ejecución: defina ADMIN_PASSWORD (clave inicial del usuario admin)}"
  (umask 077; printf '%s' "$ADMIN_PASSWORD" > "$STATE/admin.pass")
fi
if [[ ! -f "$STATE/pki/ca.key" ]]; then note "Creando CA y secreto maestro (sólo la primera vez)"; fi
"${PKI_CLI[@]}" init --dir "$STATE/pki" >/dev/null
"${PKI_CLI[@]}" secret --file "$STATE/cluster.key" >/dev/null

# Primera pasada: PEERS (ControlNodes), CONTROLS (URLs internas) y ENDPOINTS (URLs públicas)
PEERS=""; CONTROLS=""; ENDPOINTS=""; ci=0
while read -r role name priv pub az; do
  [[ "$role" == control ]] || continue
  [[ -n "$pub" ]] || die "$name no tiene DNS público en el inventario (¿instancia sin IP pública?)"
  ci=$((ci + 1)); api=$((8440 + ci)); int=$((9440 + ci))
  PEERS+="${name}=https://${pub}:${api},https://${priv}:${int};"
  CONTROLS+="https://${priv}:${int},"
  ENDPOINTS+="https://${pub}:${api},"
done < <(inv_lines)
(( ci >= 1 )) || die "el inventario no tiene ControlNodes"
PEERS="${PEERS%;}"; CONTROLS="${CONTROLS%,}"; ENDPOINTS="${ENDPOINTS%,}"

rm -rf "$OUT" "$HERE/client"; mkdir -p "$OUT" "$HERE/client"
ci=0; di=0
while read -r role name priv pub az; do
  d="$OUT/$name"; mkdir -p "$d"
  cp "$STATE/pki/ca.pem" "$d/ca.pem"
  case "$role" in
    control)
      ci=$((ci + 1)); api=$((8440 + ci)); int=$((9440 + ci))
      "${PKI_CLI[@]}" issue --dir "$STATE/pki" --name "$name" --role controlnode --san "$priv" --san "$pub" >/dev/null
      cp "$STATE/pki/$name.crt" "$STATE/pki/$name.key" "$d/"
      cp "$STATE/cluster.key" "$d/cluster.key"; cp "$STATE/admin.pass" "$d/admin.pass"
      cat > "$d/node.env" <<ENV
DFSHA_NODE_ID=$name
DFSHA_PEERS=$PEERS
DFSHA_DATA_DIR=/data
DFSHA_CERT=/pki/$name.crt
DFSHA_KEY=/pki/$name.key
DFSHA_CA=/pki/ca.pem
DFSHA_CLUSTER_SECRET_FILE=/secrets/cluster.key
DFSHA_ADMIN_PASSWORD_FILE=/secrets/admin.pass
DFSHA_LOG_LEVEL=info
ENV
      echo "$api $int dfsha-control" > "$d/ports" ;;
    data)
      di=$((di + 1)); api=$((7440 + di)); int=$((7540 + di))
      "${PKI_CLI[@]}" issue --dir "$STATE/pki" --name "$name" --role datanode --san "$priv" --san "$pub" >/dev/null
      cp "$STATE/pki/$name.crt" "$STATE/pki/$name.key" "$d/"      # un DataNode NO recibe cluster.key
      cat > "$d/node.env" <<ENV
DFSHA_NODE_ID=$name
DFSHA_CONTROLS=$CONTROLS
DFSHA_DATA_DIR=/data
DFSHA_CERT=/pki/$name.crt
DFSHA_KEY=/pki/$name.key
DFSHA_CA=/pki/ca.pem
DFSHA_ZONE=$az
DFSHA_API_PORT=$api
DFSHA_INTERNAL_PORT=$int
DFSHA_ADVERTISE_PUBLIC=https://$pub:$api
DFSHA_ADVERTISE_INTERNAL=https://$priv:$int
DFSHA_CAPACITY=${DFSHA_DN_CAPACITY:-20GiB}
DFSHA_LOG_LEVEL=info
ENV
      echo "$api $int dfsha-data" > "$d/ports" ;;
    client)   # sólo la CA pública y los endpoints
      echo "DFSHA_ENDPOINTS=$ENDPOINTS" > "$d/client.env"
      echo "0 0 none" > "$d/ports" ;;
    *) die "rol desconocido en el inventario: $role" ;;
  esac
done < <(inv_lines)

cp "$STATE/pki/ca.pem" "$HERE/client/ca.pem"
cat > "$HERE/client/env.sh" <<ENV
# source deploy/aws/client/env.sh   (desde su portátil)
export DFSHA_ENDPOINTS='$ENDPOINTS'
export DFSHA_CA='$HERE/client/ca.pem'
export DFSHA_USER=admin
# export DFSHA_PASSWORD='...'      # o use:  dfsha login
ENV
echo "Listo: bundles/ (por nodo), client/env.sh y state/ (CA + secreto maestro: CUSTODIE state/, no se versiona)."
echo "Certificados emitidos con SAN = IP privada + DNS público. Siguiente: KEY_FILE=$KEY_FILE ./deploy.sh"
