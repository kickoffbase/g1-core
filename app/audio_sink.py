"""
PulseAudio Sink Switcher
========================
Flips the system default sink between the G1 chest speaker and a paired
external speaker (typically JBL over Bluetooth).

Why a switcher and not just `pactl set-default-sink` from a script:

  - Idempotent + thread-safe: multiple callers can `apply("jbl")` per
    second without stamping on each other or spamming pactl.
  - Graceful degradation: if PulseAudio isn't running on this host (the
    G1 Orin uses raw ALSA out of the box), the call is a *successful*
    no-op so the operator UI doesn't show a fake error. Routing then
    relies on whatever `SYSTEM_AUDIO_CMD` is configured to do.
  - Auto-fallback: if `jbl` is requested but the sink isn't currently
    registered (BT speaker off / out of range / unpaired), we silently
    fall back to `builtin` so the robot keeps talking through the chest
    speaker instead of going silent.

Mapping is configured in `.env`:

    AUDIO_SINK_BUILTIN=alsa_output.platform-snd_aloop.0.analog-stereo
    AUDIO_SINK_JBL=bluez_sink.XX_XX_XX_XX_XX_XX.a2dp_sink

Find sink names with `pactl list sinks short`.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from threading import Lock
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)

# Last successfully-applied mode. Lets the watchdog skip pactl on every
# tick and only re-apply when the operator actually flips the toggle.
_applied: Optional[str] = None
_lock = Lock()
_pulse_unavailable_logged = False


def current() -> Optional[str]:
    """The mode last applied successfully (None until first apply)."""
    return _applied


def apply(mode: str) -> bool:
    """Switch the system default sink. Idempotent + thread-safe.

    Returns True on success OR when there's nothing to do (pulse not
    running). Returns False only when sinks are misconfigured."""
    global _applied
    if mode not in ("builtin", "jbl"):
        log.warning("audio_sink.apply: unknown mode %r — ignoring", mode)
        return False

    with _lock:
        if not _pulse_alive():
            # Nothing to switch on this host — record intent and let the
            # SYSTEM_AUDIO_CMD path do its thing.
            _applied = mode
            return True

        available = _available_sinks()
        target_mode = mode
        sink = _sink_for(target_mode)
        if target_mode == "jbl" and (not sink or sink not in available):
            log.info("JBL sink %r unavailable — falling back to builtin", sink or "<unset>")
            target_mode = "builtin"
            sink = _sink_for("builtin")

        if not sink:
            log.warning(
                "audio_sink.apply: no sink configured for mode=%s "
                "(set AUDIO_SINK_%s in .env)",
                target_mode, target_mode.upper(),
            )
            return False

        if sink not in available:
            log.warning("audio_sink.apply: sink %r not registered with pactl — skipping", sink)
            return False

        if target_mode == _applied:
            return True

        try:
            subprocess.run(
                ["pactl", "set-default-sink", sink],
                check=True, capture_output=True, text=True, timeout=5,
            )
        except subprocess.CalledProcessError as e:
            log.error("audio_sink.apply: pactl failed (sink=%s): %s",
                      sink, (e.stderr or e.stdout or "").strip()[:200])
            return False
        except subprocess.TimeoutExpired:
            log.error("audio_sink.apply: pactl timed out (sink=%s)", sink)
            return False

        _applied = target_mode
        log.info("Audio sink → %s (sink=%s%s)", target_mode, sink,
                 " [fallback from jbl]" if target_mode != mode else "")
        return True


# ── Internals ──────────────────────────────────────────────────────────


def _sink_for(mode: str) -> Optional[str]:
    if mode == "jbl":
        return (settings.audio_sink_jbl or "").strip() or None
    if mode == "builtin":
        return (settings.audio_sink_builtin or "").strip() or None
    return None


def _pulse_alive() -> bool:
    global _pulse_unavailable_logged
    if shutil.which("pactl") is None:
        if not _pulse_unavailable_logged:
            log.info("audio_sink: pactl not installed — sink switching disabled")
            _pulse_unavailable_logged = True
        return False
    try:
        r = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=3)
    except Exception:
        return False
    if r.returncode != 0:
        if not _pulse_unavailable_logged:
            log.info("audio_sink: PulseAudio/PipeWire not running — sink switching disabled")
            _pulse_unavailable_logged = True
        return False
    return True


def _available_sinks() -> set:
    if not _pulse_alive():
        return set()
    try:
        out = subprocess.run(
            ["pactl", "list", "sinks", "short"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        log.debug("pactl list sinks failed: %s", e)
        return set()
    names = set()
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1]:
            names.add(parts[1])
    return names
