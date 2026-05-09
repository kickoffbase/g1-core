#!/usr/bin/env bash
# Service entrypoint. Kept stupidly small on purpose:
# preflight already did the cleanup, supervision lives inside main.py
# (services start their own threads), and systemd handles whole-process
# restart. This wrapper just picks the right interpreter and execs.

set -u
cd "$(dirname "$0")/.."

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 2>/dev/null || echo /usr/bin/python3)"
fi

echo "[run] starting g1-core (python=${PYTHON_BIN})"
exec "${PYTHON_BIN}" -m main
