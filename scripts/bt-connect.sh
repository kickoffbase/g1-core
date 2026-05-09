#!/usr/bin/env bash
# Pair, trust, and connect a Bluetooth speaker — call once per speaker
# per machine. Subsequent boots auto-reconnect (the watchdog also nudges
# the link if it drops).
#
# Usage:
#   bash scripts/bt-connect.sh AA:BB:CC:DD:EE:FF
#   bash scripts/bt-connect.sh                  # interactive scan + pick
#
# After connecting, set BLUETOOTH_MAC=<mac> in .env so the watchdog can
# auto-reconnect on link loss.

set -euo pipefail

if ! command -v bluetoothctl >/dev/null 2>&1; then
  echo "ERROR: bluetoothctl not found. Install: sudo apt install -y bluez bluez-tools" >&2
  exit 1
fi

MAC="${1:-}"

if [[ -z "$MAC" ]]; then
  echo "Scanning for 10s — power on the speaker now and put it in pairing mode..."
  bluetoothctl --timeout 10 scan on || true
  bluetoothctl devices
  read -rp "Paste the MAC of the speaker: " MAC
fi

if [[ -z "$MAC" ]]; then
  echo "No MAC given. Aborting." >&2
  exit 1
fi

echo "==> power on / agent"
bluetoothctl power on >/dev/null
bluetoothctl agent on >/dev/null
bluetoothctl default-agent >/dev/null || true

echo "==> pair $MAC"
bluetoothctl pair "$MAC" || true
echo "==> trust $MAC"
bluetoothctl trust "$MAC" >/dev/null || true
echo "==> connect $MAC"
bluetoothctl connect "$MAC"

if command -v pactl >/dev/null 2>&1; then
  echo "==> setting Pulse default sink"
  SINK="$(pactl list short sinks | awk -v m="${MAC//:/_}" '$0 ~ m {print $2; exit}')"
  if [[ -n "$SINK" ]]; then
    pactl set-default-sink "$SINK"
    echo "Default sink: $SINK"
  else
    echo "WARN: BT sink not found in PulseAudio yet. Run: pactl list short sinks"
  fi
fi

echo
echo "Add this line to ~/g1-core/.env so the watchdog auto-reconnects:"
echo "  BLUETOOTH_MAC=$MAC"
