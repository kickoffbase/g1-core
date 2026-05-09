#!/usr/bin/env bash
# One-line health probe — handy from the operator laptop:
#   bash scripts/health.sh
#   bash scripts/health.sh https://your-ngrok-domain  # remote
#
# We always send `ngrok-skip-browser-warning` so free-tier ngrok never
# returns its HTML interstitial (which would look like the service is
# broken when it's actually fine).
set -u
URL="${1:-http://127.0.0.1:8766}"
KEY="${WEBHOOK_API_KEY:-}"

ARGS=(-sS -H "ngrok-skip-browser-warning: 1" -A "g1-core-cli/1.0")
[[ -n "$KEY" ]] && ARGS+=(-H "X-API-Key: $KEY")

curl "${ARGS[@]}" "$URL/health" || true
echo
