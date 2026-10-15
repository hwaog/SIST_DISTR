#!/usr/bin/env bash
# Empaqueta la entrega SIN basura: sólo lo versionado en git (nada de .venv, .env, claves ni estado local).
#   scripts/make_entrega.sh            ->  ../dfsha-entrega.zip
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -z "$(git status --porcelain)" ]] || { echo "ERROR: hay cambios sin commitear (git add -A && git commit)"; exit 1; }
OUT="../dfsha-entrega-$(date +%Y%m%d).zip"
git archive --format=zip --prefix=dfsha/ -o "$OUT" HEAD
echo "Creado $OUT ($(du -h "$OUT" | cut -f1))"
if unzip -l "$OUT" | grep -E '\.venv/|\.env$|\.key$|state/|bundles/' >/dev/null; then echo "ERROR: el zip contiene material sensible"; exit 1; fi
echo "Sin material sensible. Contenido de primer nivel:"; unzip -l "$OUT" | awk '{print $4}' | grep -E '^dfsha/[^/]+/?$' | sed 's#^#  #'
