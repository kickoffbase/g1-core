#!/usr/bin/env bash
# One-line health probe — handy from the operator laptop:
#   bash scripts/health.sh
#   bash scripts/health.sh https://your-ngrok-domain  # remote
set -u
URL="${1:-http://127.0.0.1:8766}"
KEY="${WEBHOOK_API_KEY:-}"

if [[ -n "$KEY" ]]; then
  curl -sS -H "X-API-Key: $KEY" "$URL/health" || true
else
  curl -sS "$URL/health" || true
fi
echo
