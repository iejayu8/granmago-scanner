#!/usr/bin/env bash
# Suite completa de GranMago (obligatoria antes de publicar una versión).
# Incluye el render headless del panel con jsdom: si falta, lo instala en
# ${GRANMAGO_JSDOM_DIR:-$HOME/.cache/granmago-jsdom} y falla si el render se omite.
set -euo pipefail
cd "$(dirname "$0")/.."
JSDOM_DIR="${GRANMAGO_JSDOM_DIR:-$HOME/.cache/granmago-jsdom}"
if [ ! -d "$JSDOM_DIR/node_modules/jsdom" ] && [ ! -d /workspace/granmago-robustez-1.7.2/node_modules/jsdom ]; then
  mkdir -p "$JSDOM_DIR" && (cd "$JSDOM_DIR" && npm install --silent --no-audit --no-fund jsdom >/dev/null)
fi
export GRANMAGO_JSDOM_DIR="${GRANMAGO_JSDOM_DIR:-$JSDOM_DIR}"
[ -d "$GRANMAGO_JSDOM_DIR/node_modules/jsdom" ] || export GRANMAGO_JSDOM_DIR=/workspace/granmago-robustez-1.7.2
out="$(python3 -m unittest discover -s tests -v 2>&1)" && rc=0 || rc=$?
echo "$out" | tail -n 25
if echo "$out" | grep -q "render headless omitido"; then echo "ERROR: render headless del panel omitido (falta node/jsdom)"; exit 2; fi
exit $rc
