#!/usr/bin/env bash
# One-shot redeploy from the operator laptop to the robot.
#
#   bash scripts/deploy.sh
#   ROBOT=unitree@10.0.0.42 bash scripts/deploy.sh    # override target
#   bash scripts/deploy.sh --no-restart                 # just sync files
#
# Why this exists:
#   - rsync the repo (preserves .env + state on the robot).
#   - Restart the user-systemd unit; preflight.py handles CRLF + port
#     cleanup + network wait, so this script never has to.
#   - Tail the journal so we see boot output without a second SSH.

set -euo pipefail

ROBOT="${ROBOT:-unitree@192.168.123.164}"
REMOTE_DIR="${REMOTE_DIR:-/home/unitree/g1-core}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

restart=true
for arg in "$@"; do
  case "$arg" in
    --no-restart) restart=false ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
  esac
done

echo "==> rsync ${LOCAL_DIR} → ${ROBOT}:${REMOTE_DIR}"
rsync -avz --delete \
  --exclude .git --exclude .venv --exclude __pycache__ \
  --exclude state --exclude .env \
  "${LOCAL_DIR}/" "${ROBOT}:${REMOTE_DIR}/"

if ! $restart; then
  echo "==> --no-restart: skipping restart"
  exit 0
fi

echo "==> systemctl --user restart g1-core"
ssh "${ROBOT}" '
  set -e
  systemctl --user restart g1-core
  sleep 2
  echo
  systemctl --user --no-pager status g1-core | head -n 10
  echo "--- journalctl --user -u g1-core -n 40 --no-pager ---"
  journalctl --user -u g1-core -n 40 --no-pager
'
