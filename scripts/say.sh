#!/usr/bin/env bash
# Quick smoke test from the operator laptop.
#   bash scripts/say.sh "Hello from the lab"
#   URL=https://your-domain.ngrok-free.dev bash scripts/say.sh "Hi"
set -u
URL="${URL:-http://127.0.0.1:8766}"
KEY="${WEBHOOK_API_KEY:-}"
TEXT="${1:-Hello from g1-core.}"

ARGS=(-sS -X POST "$URL/say" -H "Content-Type: application/json" -d "{\"text\": \"$TEXT\"}")
[[ -n "$KEY" ]] && ARGS+=(-H "X-API-Key: $KEY")

curl "${ARGS[@]}"
echo
