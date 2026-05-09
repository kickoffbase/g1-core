"""
Speaker
=======
"`speaker.say(text)`" is the only function the rest of the service needs
to know about. It owns:

  - audio sink construction (Unitree / system / both)
  - TTS streaming
  - LED feedback (best-effort, never blocks playback)
  - per-utterance serialization (one mouth, one voice)

Stability notes:
  - System audio is opened lazily per utterance and torn down at the end —
    a Bluetooth disconnect mid-utterance hurts that one line, not the
    next one (the next call respawns `aplay` cleanly).
  - TTS errors mark the command failed but never raise out of `say()`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from app import audio_sink, tts
from app.audio import SystemAudioPlayer
from app.config import settings
from app.robot import robot

log = logging.getLogger(__name__)

_PCM_BPS = 32000  # s16le mono 16 kHz = 2 bytes * 16000 Hz
_lock = threading.Lock()


def _audio_mode() -> str:
    raw = (settings.audio_output or "system").strip().lower()
    if raw not in {"unitree", "system", "both"}:
        log.warning("Unknown AUDIO_OUTPUT=%r, defaulting to system", raw)
        return "system"
    return raw


def _uses_unitree() -> bool:
    return _audio_mode() in {"unitree", "both"}


def _uses_system() -> bool:
    return _audio_mode() in {"system", "both"}


def say(text: str) -> dict:
    """Speak `text` to whichever sinks are configured.

    Returns a small result dict describing how much audio was played and
    where. Never raises — failures are reported in the dict (`error`)."""
    text = (text or "").strip()
    if not text:
        return {"played_bytes": 0, "skipped": "empty"}

    if not settings.elevenlabs_api_key:
        return {"played_bytes": 0, "error": "ELEVENLABS_API_KEY not set"}

    with _lock:
        return _say_locked(text)


def speak_personality_intro() -> Optional[dict]:
    from app import personality
    line = personality.get().intro_line
    return say(line) if line else None


def speak_personality_outro() -> Optional[dict]:
    from app import personality
    line = personality.get().outro_line
    return say(line) if line else None


def _say_locked(text: str) -> dict:
    system_player: Optional[SystemAudioPlayer] = None
    played_bytes = 0
    started = time.monotonic()
    error: Optional[str] = None
    sink_mode: Optional[str] = None
    # When the system sink is requested but unavailable (or dies mid-stream)
    # we route audio through the built-in robot speaker even if AUDIO_OUTPUT
    # was 'system' only. Better to talk through the wrong speaker than to
    # play silence at the operator.
    force_unitree = False

    if _uses_system():
        sink_mode = (settings.audio_sink_default or "jbl").strip().lower()
        if sink_mode in ("builtin", "jbl"):
            audio_sink.apply(sink_mode)
        try:
            system_player = SystemAudioPlayer(
                settings.system_audio_cmd,
                settings.system_audio_fallback_cmd,
            )
            system_player.start()
        except Exception as e:
            log.error("System audio unavailable: %s — falling back to G1 built-in speaker", e)
            system_player = None
            force_unitree = True

    _set_led(255, 255, 255)
    try:
        for pcm in tts.stream(_one_chunk(text)):
            if _uses_unitree() or force_unitree:
                robot.play_pcm(pcm)
            if system_player is not None:
                if not system_player.write(pcm):
                    if not force_unitree:
                        log.warning("System audio dropped mid-utterance — switching to G1 built-in speaker")
                    system_player = None
                    force_unitree = True
            played_bytes += len(pcm)
    except tts.TTSUnavailable as e:
        error = f"tts_unavailable: {e}"
        log.error("TTS unavailable: %s", e)
    except Exception as e:
        error = f"tts_error: {e}"
        log.exception("TTS streaming failed")
    finally:
        if system_player is not None:
            system_player.close()
        _set_led(0, 0, 0)

    return {
        "played_bytes": played_bytes,
        "duration_s": round(played_bytes / _PCM_BPS, 2) if played_bytes else 0.0,
        "wall_s": round(time.monotonic() - started, 2),
        "audio_output": _audio_mode(),
        "sink_mode": sink_mode,
        "fallback_to_builtin": force_unitree,
        "error": error,
    }


def _one_chunk(text: str):
    """ElevenLabs accepts an iterator — for static text we just yield once."""
    yield text


def _set_led(r: int, g: int, b: int) -> None:
    try:
        robot.set_led(r, g, b)
    except Exception:
        pass
