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

from app import audio_sink, gestures, tts
from app.audio import SystemAudioPlayer
from app.config import settings
from app.robot import robot

log = logging.getLogger(__name__)

_PCM_BPS = 32000  # s16le mono 16 kHz = 2 bytes * 16000 Hz
_lock = threading.Lock()
# ElevenLabs flash voice averages ~14 chars/s on conversational copy at
# speed=1.0. Used to estimate utterance duration so the gesture
# Conductor can place gestures before the line ends instead of trailing
# into silence. Slightly conservative — the worst-case error is "we
# don't gesture", never "the gesture lands after speech ends".
_AVG_CHARS_PER_SEC = 14.0


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
        primary_cmd, fallback_cmd = _build_audio_commands(sink_mode)
        try:
            system_player = SystemAudioPlayer(primary_cmd, fallback_cmd)
            system_player.start()
        except Exception as e:
            log.error("System audio unavailable: %s — falling back to G1 built-in speaker", e)
            system_player = None
            force_unitree = True

    log.info("speak: '%s' (mode=%s sink=%s preroll=%dms)",
             text if len(text) <= 80 else text[:77] + "…",
             _audio_mode(), sink_mode or "-", settings.audio_preroll_ms)
    _set_led(255, 255, 255)
    # Schedule subtle in-speech gestures. Built fresh per utterance so
    # there's no shared state to corrupt. `start()` is a fast no-op when
    # gestures are disabled, the arm is unavailable, or the line is too
    # short — we still always call `stop()` in `finally` to release.
    conductor = gestures.Conductor()
    eta_s = max(0.0, len(text) / _AVG_CHARS_PER_SEC + (settings.audio_preroll_ms / 1000.0))
    try:
        sched = conductor.start(eta_s)
        log.info("speak: gesture conductor %s", sched)
    except Exception:
        log.debug("Gesture conductor start raised", exc_info=True)
    try:
        for pcm in _with_preroll(tts.stream(_one_chunk(text)), settings.audio_preroll_ms):
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
        try:
            conductor.stop()
        except Exception:
            log.debug("Gesture conductor stop raised", exc_info=True)
        _set_led(0, 0, 0)

    result = {
        "played_bytes": played_bytes,
        "duration_s": round(played_bytes / _PCM_BPS, 2) if played_bytes else 0.0,
        "wall_s": round(time.monotonic() - started, 2),
        "audio_output": _audio_mode(),
        "sink_mode": sink_mode,
        "fallback_to_builtin": force_unitree,
        "error": error,
    }
    if error:
        log.warning("speak: done with error=%s after %.2fs", error, result["wall_s"])
    else:
        log.info("speak: done %.2fs %db played sink=%s%s",
                 result["wall_s"], played_bytes, sink_mode or "-",
                 " (fallback→builtin)" if force_unitree else "")
    return result


def _one_chunk(text: str):
    """ElevenLabs accepts an iterator — for static text we just yield once."""
    yield text


def _with_preroll(pcm_iter, preroll_ms: int):
    """Prepend `preroll_ms` of digital silence (s16le mono 16 kHz).

    Bluetooth (and even some USB) sinks need ~150-300 ms to leave standby
    after the first PCM byte — without preroll the first phoneme is eaten.
    Yielding zeros is equivalent to silent audio: PA / aplay open the
    stream + the bluez sink reaches RUNNING before the real phoneme
    arrives, so the utterance starts with the very first sound."""
    if preroll_ms > 0:
        n_bytes = (_PCM_BPS * preroll_ms) // 1000
        # Round to an even number — s16le samples are 2 bytes.
        if n_bytes % 2:
            n_bytes += 1
        if n_bytes:
            yield b"\x00" * n_bytes
    for chunk in pcm_iter:
        yield chunk


def _build_audio_commands(sink_mode: str) -> tuple[str, str]:
    """Pick the player command(s) for this utterance.

    When pactl is alive AND we know the actual sink name, we go through
    `paplay --device=<sink>` — this guarantees audio reaches the JBL
    instead of the chest speaker (raw `aplay` ignores PulseAudio's
    default-sink). For any other case we fall back to the operator's
    SYSTEM_AUDIO_CMD (typically `aplay`)."""
    primary = settings.system_audio_cmd
    fallback = settings.system_audio_fallback_cmd
    sink = audio_sink.resolved_sink(sink_mode) if sink_mode else None
    if sink:
        # `--client-name` makes the stream identifiable in pavucontrol;
        # `--latency-msec=200` keeps the bluez sink primed without
        # introducing a noticeable delay. The raw format matches what
        # ElevenLabs streams (s16le, mono, 16 kHz).
        paplay = (
            f"paplay --device={sink} --client-name=g1-core "
            "--raw --rate=16000 --format=s16le --channels=1 --latency-msec=200"
        )
        primary = paplay
        # Fallback to the operator-configured command (usually aplay) so
        # we still play *something* if PA dies mid-utterance.
        fallback = settings.system_audio_cmd
    return primary, fallback


def _set_led(r: int, g: int, b: int) -> None:
    try:
        robot.set_led(r, g, b)
    except Exception:
        pass
