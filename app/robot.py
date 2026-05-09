"""
Robot Connection
================
Thin wrapper around `unitree_sdk2py` with three properties the original
g1-brain didn't have, and which matter for stability:

  1. **Optional**. If `ROBOT_ENABLED=false` or the SDK can't be imported,
     all robot ops become no-ops and the service still boots — handy for
     dev machines and for the case where DDS itself is sick. The webhook
     is unaffected; system audio still plays.

  2. **Self-healing**. Every public method goes through `_call`, which on
     SDK exception flips the state to ERROR and triggers a backoff
     reconnect in a background thread. Subsequent calls return `False`
     fast until the link is back; once it's back, they "just work" again.

  3. **Inspectable**. `health()` returns a small dict the watchdog and
     the webhook can both read.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from app.config import settings

log = logging.getLogger(__name__)


class State(str, Enum):
    DISABLED = "disabled"      # ROBOT_ENABLED=false
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class RobotHealth:
    state: State
    last_error: Optional[str] = None
    last_connect_at: Optional[float] = None
    failed_calls: int = 0


class Robot:
    def __init__(self) -> None:
        self._state = State.DISABLED if not settings.robot_enabled else State.DISCONNECTED
        self._lock = threading.RLock()
        self._reconnect_thread: Optional[threading.Thread] = None
        self._reconnect_stop = threading.Event()
        self._audio = None
        self._loco = None
        self._failed_calls = 0
        self._last_error: Optional[str] = None
        self._last_connect_at: Optional[float] = None

    # ── Public surface ─────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._state == State.CONNECTED

    def health(self) -> dict:
        return {
            "state": self._state.value,
            "last_error": self._last_error,
            "last_connect_at": self._last_connect_at,
            "failed_calls": self._failed_calls,
        }

    def start(self) -> None:
        """Try to connect once, then keep retrying forever in the background
        until the service shuts down. Safe to call multiple times."""
        if self._state == State.DISABLED:
            log.info("Robot disabled (ROBOT_ENABLED=false) — skipping DDS init")
            return
        with self._lock:
            self._connect_once()
            if not self.connected:
                self._spawn_reconnect()

    def stop(self) -> None:
        self._reconnect_stop.set()
        with self._lock:
            self._audio = None
            self._loco = None
            self._state = State.DISCONNECTED if settings.robot_enabled else State.DISABLED

    # Robot ops — every one is a no-op when disconnected.

    def set_volume(self, volume: int) -> bool:
        return self._call("SetVolume", lambda c: c.SetVolume(volume), use="audio")

    def set_led(self, r: int, g: int, b: int) -> bool:
        return self._call("LedControl", lambda c: c.LedControl(r, g, b), use="audio")

    def play_pcm(self, pcm: bytes, stream_id: str = "g1_core") -> bool:
        return self._call(
            "PlayStream",
            lambda c: c.PlayStream("g1_core", stream_id, pcm),
            use="audio",
        )

    def stop_playback(self) -> bool:
        return self._call("PlayStop", lambda c: c.PlayStop("g1_core"), use="audio")

    # ── Internals ──────────────────────────────────────────────────────

    def _call(self, op: str, fn: Callable[[Any], Any], use: str) -> bool:
        if self._state != State.CONNECTED:
            return False
        client = self._audio if use == "audio" else self._loco
        if client is None:
            return False
        try:
            fn(client)
            return True
        except Exception as e:
            self._on_call_error(op, e)
            return False

    def _on_call_error(self, op: str, error: Exception) -> None:
        with self._lock:
            self._failed_calls += 1
            self._last_error = f"{op}: {error}"
            self._state = State.ERROR
            self._audio = None
            self._loco = None
        log.error("Robot op %s failed: %s — scheduling reconnect", op, error)
        self._spawn_reconnect()

    def _spawn_reconnect(self) -> None:
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return
        self._reconnect_stop.clear()
        t = threading.Thread(target=self._reconnect_loop, name="robot-reconnect", daemon=True)
        self._reconnect_thread = t
        t.start()

    def _reconnect_loop(self) -> None:
        backoff = settings.robot_reconnect_min_s
        while not self._reconnect_stop.is_set() and not settings.shutdown:
            time.sleep(backoff)
            with self._lock:
                self._connect_once()
                if self.connected:
                    return
            backoff = min(backoff * 2, settings.robot_reconnect_max_s)

    def _connect_once(self) -> None:
        if self._state == State.CONNECTING:
            return
        self._state = State.CONNECTING
        log.info("Robot: connecting via interface '%s'...", settings.network_interface)
        try:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            ChannelFactoryInitialize(0, settings.network_interface)
        except Exception as e:
            self._last_error = f"DDS init failed: {e}"
            self._state = State.ERROR
            log.warning("Robot: DDS init failed (%s)", e)
            return

        if not self._init_audio() or not self._init_loco():
            return

        try:
            if self._audio is not None:
                self._audio.SetVolume(settings.robot_volume)
        except Exception as e:
            log.warning("Robot: SetVolume on connect failed: %s", e)

        self._state = State.CONNECTED
        self._last_connect_at = time.time()
        self._last_error = None
        log.info("Robot: connected")

    def _init_audio(self) -> bool:
        try:
            from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
            client = AudioClient()
            client.SetTimeout(settings.robot_init_timeout_s)
            client.Init()
            self._audio = client
            return True
        except Exception as e:
            self._last_error = f"AudioClient init failed: {e}"
            self._state = State.ERROR
            log.warning("Robot: AudioClient init failed (%s)", e)
            return False

    def _init_loco(self) -> bool:
        # Loco is optional for g1-core (we don't move the robot from the
        # webhook yet) — failing to init it should not block audio.
        try:
            from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
            client = LocoClient()
            client.SetTimeout(settings.robot_init_timeout_s)
            client.Init()
            self._loco = client
        except Exception as e:
            log.info("Robot: LocoClient unavailable (%s) — audio still works", e)
        return True


robot = Robot()
