#!/usr/bin/env bash
# Preflight runs before EVERY g1-core start. It is idempotent.
#
# Goals:
#   1. Make sure no stale process is squatting on our ports — a hung
#      python or a leftover ngrok from g1-brain is the #1 cause of
#      "service silently doesn't work after reboot".
#   2. Sanitize line endings (the repo is sometimes cross-edited from
#      Windows; CRLF in a shell script == cryptic `$'\r'` errors).
#   3. Make sure dependencies are present.
#
# We intentionally use `|| true` everywhere — preflight must not fail
# the unit even if a probe is missing.

set -u
cd "$(dirname "$0")/.."
REPO="$(pwd)"

# ── .env probe ──────────────────────────────────────────────────────────
read_env() {
  local key="$1" raw
  [[ -f .env ]] || { echo ""; return 0; }
  raw="$(grep -E "^${key}=" .env 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
  raw="${raw%%#*}"
  raw="$(printf '%s' "$raw" | tr -d '\r' | xargs)"
  raw="${raw%\"}"; raw="${raw#\"}"
  raw="${raw%\'}"; raw="${raw#\'}"
  echo "$raw"
}

PORT="$(read_env WEBHOOK_PORT)";  PORT="${PORT:-8766}"

echo "[preflight] repo=$REPO port=$PORT"

# ── CRLF rescue ────────────────────────────────────────────────────────
# Run `sed -i 's/\r$//'` over EVERY text file in the repo on every boot.
# This means: no matter how the operator copied files (scp from Windows,
# WinSCP drag-and-drop, mounted SMB share, GitHub zip download, …), the
# next `systemctl --user restart g1-core` heals them. We exclude .git/
# .venv/__pycache__ to keep the wall-time under a few hundred ms.
echo "[preflight] normalizing line endings (whole repo)"
if command -v find >/dev/null 2>&1; then
  find . \
      \( -path ./.git -o -path ./.venv -o -path '*/__pycache__' \) -prune -o \
      -type f \( -name '*.sh' -o -name '*.service' -o -name '*.py' \
              -o -name '*.json' -o -name '*.md' -o -name '*.toml' \
              -o -name '*.yml' -o -name '*.yaml' \
              -o -name '.env' -o -name '.env.example' -o -name '.editorconfig' \
              -o -name '.gitattributes' \) \
      -print0 2>/dev/null \
    | xargs -0 -r sed -i 's/\r$//' 2>/dev/null || true
fi
chmod +x systemd/*.sh scripts/*.sh systemd/bootstrap.py 2>/dev/null || true

# ── Conflicting g1-brain ────────────────────────────────────────────────
# g1-brain is the predecessor service. If it's still active it will hold
# its own port AND its own ngrok agent — and ngrok free-tier allows ONE
# agent per account. That's why the user often sees "tunnel down".
if command -v systemctl >/dev/null 2>&1; then
  if systemctl --user is-active --quiet g1-brain.service 2>/dev/null; then
    echo "[preflight] stopping conflicting g1-brain.service (user)"
    systemctl --user stop g1-brain.service 2>/dev/null || true
  fi
  if systemctl is-active --quiet g1-brain.service 2>/dev/null; then
    echo "[preflight] stopping conflicting g1-brain.service (system) — needs sudo"
    sudo -n systemctl stop g1-brain.service 2>/dev/null || true
  fi
fi

# ── Stale processes ────────────────────────────────────────────────────
echo "[preflight] killing stale main.py / g1-core / ngrok"
pkill -9 -f 'python3?[^\n]*g1-core/main\.py' 2>/dev/null || true
pkill -9 -f 'python3?[^\n]*g1-brain/main\.py' 2>/dev/null || true
pkill -9 -x ngrok 2>/dev/null || true

# ── Free ports ─────────────────────────────────────────────────────────
free_port() {
  local port="$1"
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${port}/tcp" 2>/dev/null || true
  elif command -v lsof >/dev/null 2>&1; then
    local pids
    pids="$(lsof -ti "tcp:${port}" 2>/dev/null || true)"
    [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null || true
  fi
}
echo "[preflight] freeing ports ${PORT}, 4040, 4041"
free_port "${PORT}"
free_port 4040
free_port 4041

# ── Make sure 'ngrok' is reachable if we're going to use it ────────────
NGROK_ENABLED="$(read_env NGROK_ENABLED)"
case "${NGROK_ENABLED,,}" in
  ""|true|1|yes)
    if ! command -v ngrok >/dev/null 2>&1; then
      echo "[preflight] WARN: NGROK_ENABLED but 'ngrok' not in PATH — tunnel will be skipped"
    fi
    ;;
esac

# ── Repo-level audio sanity ────────────────────────────────────────────
mkdir -p state

echo "[preflight] done"
