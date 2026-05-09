#!/usr/bin/env bash
# Idempotent installer for the g1-core USER systemd unit.
#
# Usage (on the Orin, as the unitree user):
#   bash ~/g1-core/systemd/install.sh
#
# What it does:
#   1. Disables / stops the older g1-brain service if present (free port +
#      free ngrok agent).
#   2. Installs the unit file under ~/.config/systemd/user/.
#   3. Enables linger so the service runs without an SSH login.
#   4. Enables + (re)starts the unit.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"
USER_NAME="$(id -un)"
SVC_DIR="$HOME/.config/systemd/user"

echo "==> 1. Sanity"
if [[ ! -f .env ]]; then
  echo "ERROR: $REPO/.env missing. cp .env.example .env and fill in your keys first." >&2
  exit 1
fi

echo "==> 2. Stop predecessor (g1-brain) if active"
systemctl --user stop g1-brain.service 2>/dev/null || true
systemctl --user disable g1-brain.service 2>/dev/null || true
sudo systemctl stop g1-brain.service 2>/dev/null || true
sudo systemctl disable g1-brain.service 2>/dev/null || true

echo "==> 3. Normalize line endings + chmod"
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix -q systemd/*.sh systemd/*.service 2>/dev/null || true
else
  for f in systemd/*.sh systemd/*.service; do
    [[ -f "$f" ]] && sed -i 's/\r$//' "$f"
  done
fi
chmod +x systemd/*.sh

echo "==> 4. Install unit at $SVC_DIR/g1-core.service"
mkdir -p "$SVC_DIR"
install -m 0644 systemd/g1-core.service "$SVC_DIR/g1-core.service"

echo "==> 5. Linger (boot without login)"
sudo loginctl enable-linger "$USER_NAME"

echo "==> 6. Reload + enable + restart"
systemctl --user daemon-reload
systemctl --user enable g1-core.service
systemctl --user restart g1-core.service

echo "==> 7. Status"
sleep 1
systemctl --user --no-pager status g1-core.service || true

echo
echo "Done. Live logs:  journalctl --user -u g1-core -f"
echo "Stop:            systemctl --user stop g1-core"
echo "Disable:         systemctl --user disable g1-core && sudo loginctl disable-linger $USER_NAME"
