"""
ElevenLabs Streaming TTS
========================
One WebSocket per utterance. We open it, push the text once, drain audio
chunks until `isFinal`, close. No long-lived connection to keep alive →
no "the WS died at 3am" failure mode. If the WS fails mid-stream, the
caller (`speaker.say`) marks the command failed but the service stays up.

Voice settings come from the active personality (with env defaults as
fallback) — switching personalities therefore changes the voice on the
very next utterance, no restart required.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from typing import Iterable, Iterator, Optional

from app import personality
from app.config import settings

log = logging.getLogger(__name__)

_WS_URL = (
    "wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input"
    "?model_id={model}&output_format=pcm_16000"
)
_CHUNK_SCHEDULE = [50, 120, 200, 290]

_session_error: Optional[str] = None
_session_error_logged = False


class TTSUnavailable(RuntimeError):
    """Raised when keys / voice id / dependencies are missing."""


def get_session_error() -> Optional[str]:
    """Non-None means we permanently disabled the API for this run
    (quota / auth) — surfaced via /health for the operator UI."""
    return _session_error


def _maybe_record_session_error(msg: str) -> None:
    global _session_error
    if _session_error is not None:
        return
    low = (msg or "").lower()
    if "quota" in low:
        _session_error = "quota_exceeded"
    elif "invalid_api_key" in low or "missing_permissions" in low:
        _session_error = "auth_error"


def stream(text_iter: Iterable[str]) -> Iterator[bytes]:
    """Yield PCM s16le mono 16kHz chunks for the given text stream.

    Iterates one fresh WebSocket per call. Raises `TTSUnavailable` if
    config is missing; otherwise yields whatever audio we got and logs
    transient failures (so a flaky network hurts one utterance, not the
    whole service)."""
    global _session_error_logged

    if _session_error is not None:
        if not _session_error_logged:
            _session_error_logged = True
            log.warning("ElevenLabs disabled this run (%s) — utterances will be silent.", _session_error)
        return

    try:
        from websocket import create_connection
    except ImportError as e:
        raise TTSUnavailable("websocket-client is not installed") from e

    voice = personality.get().voice
    api_key = settings.elevenlabs_api_key
    voice_id = voice.voice_id or settings.elevenlabs_voice_id
    if not api_key:
        raise TTSUnavailable("ELEVENLABS_API_KEY is not set")
    if not voice_id:
        raise TTSUnavailable("ELEVENLABS_VOICE_ID is not set (and no personality voice override)")

    model = voice.model or settings.elevenlabs_model
    voice_settings = {
        "stability": voice.stability if voice.stability is not None else settings.elevenlabs_stability,
        "similarity_boost": voice.similarity if voice.similarity is not None else settings.elevenlabs_similarity,
        "style": voice.style if voice.style is not None else settings.elevenlabs_style,
        "use_speaker_boost": True,
        "speed": voice.speed if voice.speed is not None else settings.elevenlabs_speed,
    }

    ws = create_connection(
        _WS_URL.format(voice_id=voice_id, model=model),
        header=[f"xi-api-key: {api_key}"],
        timeout=15,
    )
    try:
        ws.send(json.dumps({
            "text": " ",
            "voice_settings": voice_settings,
            "generation_config": {"chunk_length_schedule": _CHUNK_SCHEDULE},
        }))

        send_error: list = [None]
        send_done = threading.Event()

        def _pump_text():
            try:
                for chunk in text_iter:
                    if not chunk:
                        continue
                    ws.send(json.dumps({"text": chunk + " "}))
                ws.send(json.dumps({"text": ""}))
            except Exception as e:
                send_error[0] = e
                log.error("TTS text-send failed: %s", e)
            finally:
                send_done.set()

        threading.Thread(target=_pump_text, name="tts-send", daemon=True).start()

        while True:
            try:
                msg = ws.recv()
            except Exception as e:
                log.debug("TTS recv closed: %s", e)
                break
            if not msg:
                break
            try:
                data = json.loads(msg)
            except (json.JSONDecodeError, TypeError):
                continue

            err = data.get("error")
            if err:
                err_s = err if isinstance(err, str) else json.dumps(err)
                log.error("TTS server error: %s", err_s)
                _maybe_record_session_error(err_s)
                break

            audio_b64 = data.get("audio")
            if audio_b64:
                try:
                    yield base64.b64decode(audio_b64)
                except Exception as e:
                    log.warning("TTS audio decode failed: %s", e)

            if data.get("isFinal"):
                break

        send_done.wait(timeout=1.0)
        if send_error[0] is not None:
            raise send_error[0]
    finally:
        try:
            ws.close()
        except Exception:
            pass
