#!/usr/bin/env bash
# Idempotent installer for the g1-core USER systemd unit.
#
# Usage (on the Orin, as the unitree user):
#   bash ~/g1-core/systemd/install.sh
#
# What it does:
#   1. Disables / stops the older g1-brain service if present (free port +
#      free ngrok agent).
#   2. Registers ngrok for this user, if ngrok is installed.
#   3. Installs the unit file under ~/.config/systemd/user/.
#   4. Enables linger so the service runs without an SSH login.
#   5. Enables + (re)starts the unit.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"
USER_NAME="$(id -un)"
SVC_DIR="$HOME/.config/systemd/user"
DEFAULT_NGROK_AUTHTOKEN="3Dgg9hbyrwUSFdhOrAsqwFCy3jR_NGrdTCmjmfKYxEqAduxp"

read_env() {
  local key="$1"
  local raw=""
  [[ -f .env ]] || { echo ""; return 0; }
  raw="$(sed -n "s/^${key}=//p" .env 2>/dev/null | tail -n1 || true)"
  raw="${raw%%#*}"
  raw="$(printf '%s' "$raw" | tr -d '\r' | xargs)"
  raw="${raw%\"}"; raw="${raw#\"}"
  raw="${raw%\'}"; raw="${raw#\'}"
  echo "$raw"
}

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

echo "==> 3. Normalize line endings + chmod (full repo, via Python)"
# Python tolerates CRLF in its own source, so even if THIS shell script
# itself got CRLF-corrupted after copy-from-Windows, the line above failed
# loud and clear instead of silently mis-installing. preflight.py does the
# same sweep on every service start as a safety net.
python3 systemd/preflight.py --fix-only
chmod +x systemd/*.sh systemd/*.py scripts/*.sh

echo "==> 4. Configure ngrok for $USER_NAME"
if command -v ngrok >/dev/null 2>&1; then
  NGROK_AUTHTOKEN="$(read_env NGROK_AUTHTOKEN)"
  NGROK_AUTHTOKEN="${NGROK_AUTHTOKEN:-$DEFAULT_NGROK_AUTHTOKEN}"
  ngrok config add-authtoken "$NGROK_AUTHTOKEN" >/dev/null
  ngrok config check
else
  echo "WARNING: 'ngrok' is not installed or not in PATH; tunnel cannot start." >&2
fi

echo "==> 5. Install unit at $SVC_DIR/g1-core.service"
mkdir -p "$SVC_DIR"
install -m 0644 systemd/g1-core.service "$SVC_DIR/g1-core.service"

echo "==> 6. Linger (boot without login)"
sudo loginctl enable-linger "$USER_NAME"

echo "==> 7. Reload + enable + restart"
systemctl --user daemon-reload
systemctl --user enable g1-core.service
systemctl --user restart g1-core.service

echo "==> 8. Status"
sleep 1
systemctl --user --no-pager status g1-core.service || true

echo
echo "Done. Live logs:  journalctl --user -u g1-core -f"
echo "Stop:            systemctl --user stop g1-core"
echo "Disable:         systemctl --user disable g1-core && sudo loginctl disable-linger $USER_NAME"
