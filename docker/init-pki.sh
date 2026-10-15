#!/bin/sh
# Genera la CA, un certificado por nodo y el secreto maestro, y REPARTE a cada nodo SOLO lo que necesita.
# Idempotente: si /state ya tiene la CA y el secreto, los reutiliza (reiniciar el stack no invalida nada).
#
# Variables:  CONTROLS="c1 c2 c3"   DATAS="dn1 dn2 ..."   ADMIN_PASSWORD=...
#             STATE_DIR=/state      (privado de este contenedor: ca.key, claves emitidas, cluster.key)
#             OUT_ROOT=/out         (salida: un directorio -> un volumen por nodo)
#
# Resultado (principio de mínimo privilegio):
#   $OUT_ROOT/pki/<nodo>/{ca.pem,<nodo>.crt,<nodo>.key}   cada nodo ve SÓLO su par de claves
#   $OUT_ROOT/pki/client/ca.pem                           el cliente necesita únicamente la CA pública
#   $OUT_ROOT/sec/<controlnode>/{cluster.key,admin.pass}  el secreto maestro lo reciben SÓLO los ControlNodes
#   $STATE_DIR/ca/ca.key                                  la clave de la CA no sale de este contenedor
set -eu
STATE="${STATE_DIR:-/state}"
OUT="${OUT_ROOT:-/out}"
CAD="$STATE/ca"
: "${CONTROLS:?defina CONTROLS}" "${DATAS:?defina DATAS}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:?defina ADMIN_PASSWORD}"

umask 077
mkdir -p "$CAD"
chmod 700 "$STATE" "$CAD"
dfsha-pki init --dir "$CAD" >/dev/null
dfsha-pki secret --file "$STATE/cluster.key" >/dev/null
for n in $CONTROLS; do
  [ -f "$CAD/$n.crt" ] || dfsha-pki issue --dir "$CAD" --name "$n" --role controlnode --san "$n" --san localhost >/dev/null
done
for n in $DATAS; do
  [ -f "$CAD/$n.crt" ] || dfsha-pki issue --dir "$CAD" --name "$n" --role datanode --san "$n" --san localhost >/dev/null
done

# Reparto: cada directorio de salida es un volumen distinto, montado SOLO en su contenedor.
give() {  # give <dir> : dueño uid 10001 (usuario 'dfsha' de la imagen). Sin root (pruebas locales) se ignora.
  chown -R 10001:10001 "$1" 2>/dev/null || true
}
for n in $CONTROLS $DATAS; do
  d="$OUT/pki/$n"; mkdir -p "$d"
  cp "$CAD/ca.pem" "$CAD/$n.crt" "$CAD/$n.key" "$d/"
  chmod 755 "$d"; chmod 644 "$d/ca.pem" "$d/$n.crt"; chmod 600 "$d/$n.key"
  give "$d"
done
mkdir -p "$OUT/pki/client"
cp "$CAD/ca.pem" "$OUT/pki/client/ca.pem"
chmod 755 "$OUT/pki/client"; chmod 644 "$OUT/pki/client/ca.pem"; give "$OUT/pki/client"
for n in $CONTROLS; do
  d="$OUT/sec/$n"; mkdir -p "$d"
  cp "$STATE/cluster.key" "$d/cluster.key"
  printf '%s' "$ADMIN_PASSWORD" > "$d/admin.pass"
  chmod 700 "$d"; chmod 600 "$d/cluster.key" "$d/admin.pass"
  give "$d"
done
echo "PKI lista. Cada nodo recibió únicamente su certificado y su clave; ca.key permanece en $CAD."
