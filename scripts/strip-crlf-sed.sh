#!/usr/bin/env bash
# Strip Windows CRLF (trailing ^M) from all shell, systemd unit, Python,
# personality JSON, dotenv-ish text under the repo. Run after copying from Windows.
#
#   bash scripts/strip-crlf-sed.sh
#
# Equivalent one-liner (from repo root):
#   find . -type f \( -name '*.sh' -o -name '*.service' -o -name '*.py' -o \
#     -path './personalities/*.json' \) \
#     ! -path './.git/*' ! -path './.venv/*' ! -path '*/__pycache__/*' \
#     -exec sed -i 's/\r$//' {} +
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

find "$REPO" -type f \( \
    -name '*.sh' -o \
    -name '*.service' -o \
    -name '*.py' -o \
    \( -path '*/personalities/*.json' -o -path '*/personalities/*.JSON' \) \
  \) \
  ! -path '*/.git/*' \
  ! -path '*/.venv/*' \
  ! -path '*/__pycache__/*' \
  -exec sed -i 's/\r$//' {} +

# Optional .env — safe to strip trailing CR on each line only
[[ -f .env ]] && sed -i 's/\r$//' .env

chmod +x systemd/*.sh scripts/*.sh 2>/dev/null || true
[[ -f systemd/bootstrap.py ]] && chmod +x systemd/bootstrap.py

echo "[strip-crlf-sed] done under $REPO"
