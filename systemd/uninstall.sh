#!/usr/bin/env bash
# Reverses install.sh. Leaves .env / personalities alone.

set -euo pipefail

USER_NAME="$(id -un)"
SVC_DIR="$HOME/.config/systemd/user"

systemctl --user stop g1-core.service 2>/dev/null || true
systemctl --user disable g1-core.service 2>/dev/null || true
rm -f "$SVC_DIR/g1-core.service"
systemctl --user daemon-reload

pkill -9 -f 'python3?[^\n]*g1-core/main\.py' 2>/dev/null || true
pkill -9 -x ngrok 2>/dev/null || true

echo "Uninstalled. Linger left enabled (use \`sudo loginctl disable-linger $USER_NAME\` to fully revert)."
