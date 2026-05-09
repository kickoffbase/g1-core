"""
Bluetooth Helpers
=================
Pure `bluetoothctl` wrappers — no HTTP, no FastAPI, no service plumbing.
Imported by both the watchdog (for auto-reconnect) and the bluetooth
service (for the operator HTTP API). Splitting logic from transport keeps
each surface independently testable.

Every function returns dict-friendly values and never raises on missing
`bluetoothctl` — instead it surfaces the failure so the caller decides
how to react (HTTP 501 for the API, "skip this tick" for the watchdog).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


class BluetoothUnavailable(RuntimeError):
    """Raised when `bluetoothctl` is missing on this host."""


def is_available() -> bool:
    return shutil.which("bluetoothctl") is not None


def _run(args: List[str], timeout: float = 8.0) -> subprocess.CompletedProcess:
    if not is_available():
        raise BluetoothUnavailable("bluetoothctl not installed")
    return subprocess.run(
        ["bluetoothctl", *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _parse_devices(stdout: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith("Device "):
            continue
        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        rows.append({"mac": parts[1], "name": parts[2].strip()})
    return rows


def _str_from_info(text: str, key: str) -> str:
    m = re.search(rf"^\s*{re.escape(key)}:\s*(.+?)\s*$", text, flags=re.I | re.M)
    return m.group(1).strip() if m else ""


def _bool_from_info(text: str, key: str) -> bool:
    m = re.search(rf"^\s*{re.escape(key)}:\s*(yes|no)\s*$", text, flags=re.I | re.M)
    return bool(m and m.group(1).lower() == "yes")


def info(mac: str) -> Dict[str, object]:
    """Return paired/trusted/connected flags + display name for one device."""
    res = _run(["info", mac], timeout=6)
    text = f"{res.stdout or ''}\n{res.stderr or ''}"
    return {
        "mac": mac,
        "display_name": _str_from_info(text, "Alias") or _str_from_info(text, "Name") or mac,
        "paired": _bool_from_info(text, "Paired"),
        "trusted": _bool_from_info(text, "Trusted"),
        "connected": _bool_from_info(text, "Connected"),
    }


def is_connected(mac: str) -> bool:
    try:
        return bool(info(mac).get("connected"))
    except Exception:
        return False


def devices() -> List[Dict[str, object]]:
    """Visible (`devices`) ∪ paired devices, enriched with status flags."""
    base = _run(["devices"], timeout=6)
    if base.returncode != 0:
        detail = (base.stderr or base.stdout or "").strip()[:300]
        raise RuntimeError(f"bluetoothctl devices exit {base.returncode}: {detail}")

    rows = _parse_devices(base.stdout or "")
    paired_out = _run(["paired-devices"], timeout=6)
    paired_macs = {d["mac"] for d in _parse_devices(paired_out.stdout or "")}

    enriched: List[Dict[str, object]] = []
    for d in rows:
        try:
            i = info(d["mac"])
        except Exception as e:
            log.debug("bluetoothctl info %s failed: %s", d["mac"], e)
            i = {"mac": d["mac"], "display_name": d["name"] or d["mac"],
                 "paired": d["mac"] in paired_macs, "trusted": False, "connected": False}
        enriched.append({**i, "name": d["name"]})
    return enriched


def scan(timeout_s: int = 8) -> List[Dict[str, object]]:
    timeout_s = max(3, min(int(timeout_s), 20))
    _run(["scan", "off"], timeout=3)
    _run(["--timeout", str(timeout_s), "scan", "on"], timeout=timeout_s + 3)
    return devices()


def connect(mac: str, pair: bool = True, trust: bool = True) -> Dict[str, object]:
    """Idempotent: pair + trust + connect. Returns the final device record."""
    mac = (mac or "").strip().upper()
    if not MAC_RE.match(mac):
        raise ValueError("invalid MAC address")
    if pair:
        _run(["pair", mac], timeout=20)
    if trust:
        _run(["trust", mac], timeout=10)
    _run(["connect", mac], timeout=15)
    return info(mac)


def disconnect(mac: str) -> Dict[str, object]:
    mac = (mac or "").strip().upper()
    if not MAC_RE.match(mac):
        raise ValueError("invalid MAC address")
    _run(["disconnect", mac], timeout=10)
    return info(mac)


def reconnect_if_dropped(mac: str) -> Optional[bool]:
    """Watchdog hook: if `mac` is configured but not connected, try once.
    Returns True on (re)connect, False on attempted-but-failed, None when
    bluetoothctl is missing or the device is already connected."""
    mac = (mac or "").strip()
    if not mac or not is_available():
        return None
    if is_connected(mac):
        return None
    try:
        _run(["connect", mac], timeout=8)
    except Exception as e:
        log.warning("Bluetooth reconnect %s failed: %s", mac, e)
        return False
    return is_connected(mac)
