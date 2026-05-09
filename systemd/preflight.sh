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
echo "[preflight] normalizing line endings"
if command -v dos2unix >/dev/null 2>&1; then
  dos2unix -q systemd/*.sh systemd/*.service 2>/dev/null || true
else
  for f in systemd/*.sh systemd/*.service; do
    [[ -f "$f" ]] && sed -i 's/\r$//' "$f" 2>/dev/null || true
  done
fi
chmod +x systemd/*.sh 2>/dev/null || true

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
