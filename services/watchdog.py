"""
Watchdog
========
Cheap periodic health probe. Logs a one-line heartbeat with subsystem
status and, if enabled, gently nudges the Bluetooth speaker back online
when the link drops.

The watchdog explicitly does NOT restart the whole service — that's
systemd's job. It only repairs things it knows how to repair without a
process restart (BT reconnect, robot reconnect retry, periodic /health
log).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict

from app import audio_sink, bluetooth
from app.config import settings
from app.robot import robot
from services.base import Service

log = logging.getLogger(__name__)


class WatchdogService(Service):
    name = "watchdog"

    def __init__(self) -> None:
        self._thread = None
        self._stop = threading.Event()
        self._last_tick: float = 0.0
        self._bt_failures = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> Dict[str, Any]:
        return {
            "last_tick": self._last_tick,
            "interval_s": settings.watchdog_interval_s,
            "bt_failures": self._bt_failures,
        }

    # ── Internals ──────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set() and not settings.shutdown:
            try:
                self._tick()
            except Exception:
                log.exception("Watchdog tick failed")
            self._last_tick = time.time()
            self._stop.wait(timeout=settings.watchdog_interval_s)

    def _tick(self) -> None:
        # Robot reconnect: if the SDK is enabled but we're not connected,
        # robot.start() restarts the reconnect loop (it's a no-op if one
        # is already running).
        if settings.robot_enabled and not robot.connected:
            robot.start()

        if settings.bluetooth_autoreconnect and settings.bluetooth_mac:
            self._maybe_reconnect_bt()

        # Keep the active PulseAudio sink awake — JBL / any bluez sink
        # otherwise sleeps after ~5s of silence and the next /say takes
        # an extra second of A2DP re-handshake. No-op when pactl absent
        # or no mode applied yet.
        try:
            audio_sink.keepalive()
        except Exception:
            log.debug("audio_sink.keepalive() failed", exc_info=True)

    def _maybe_reconnect_bt(self) -> None:
        mac = settings.bluetooth_mac.strip()
        if not mac:
            return
        result = bluetooth.reconnect_if_dropped(mac)
        if result is None:
            return  # already connected, or bluetoothctl missing
        if result:
            log.info("Watchdog: BT speaker %s reconnected", mac)
        else:
            self._bt_failures += 1
            log.warning("Watchdog: BT speaker %s reconnect failed (failures=%d)",
                        mac, self._bt_failures)
