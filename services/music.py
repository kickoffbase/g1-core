"""
Music Service
=============
Plays an audio URL through the active PulseAudio sink (typically the
JBL). Designed to be the simplest possible "play this thing on the
robot's speaker" surface for Robohire — no codec assumptions on the
client side, no music synthesis on the robot side.

Why ffmpeg + paplay
-------------------
We let `ffmpeg` do all the format heavy-lifting (mp3 / m4a / opus /
ogg / wav / direct streams) and pipe **raw s16le 44.1 kHz stereo PCM**
into `paplay --device=<jbl_sink>`. This:

  1. Works for any URL the operator can copy-paste (S3 link, podcast
     feed, public mp3, whatever).
  2. Bypasses the system default sink so it always lands on the JBL,
     regardless of pavucontrol routing.
  3. Lets us stop the playback by sending SIGTERM to the process tree
     — `subprocess.Popen` + `start_new_session=True` makes that one-call.

Endpoints
---------
    POST /music         {url, gain_db?}     — start (replaces current track)
    POST /music/stop                         — stop everything
    GET  /music                              — status snapshot

If the active audio sink is unknown (no PulseAudio, no JBL paired) we
play through the operator's `SYSTEM_AUDIO_CMD` instead — same fallback
ladder as `speaker.py`.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from pydantic import BaseModel

from app import audio_sink
from app.config import settings
from services.base import Service

log = logging.getLogger(__name__)

_router_import_error: Optional[Exception] = None
try:
    from fastapi import APIRouter, Body, Header, HTTPException
except ImportError as e:  # pragma: no cover
    APIRouter = Body = Header = HTTPException = None  # type: ignore[assignment]
    _router_import_error = e


# Hard ceiling on track length. Playing a 6-hour podcast by accident is
# the kind of foot-gun that makes operators hate you.
_MAX_TRACK_SECONDS = 60 * 30


class _PlayPayload(BaseModel):
    url: str
    # `gain_db` is applied via ffmpeg's volume filter; +6 = ~2x, -6 = ~0.5x.
    gain_db: float = 0.0


def _check_auth(x_api_key: Optional[str]) -> None:
    if not settings.webhook_api_key:
        return
    if x_api_key != settings.webhook_api_key:
        raise HTTPException(status_code=401, detail="invalid api key")


class MusicService(Service):
    name = "music"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._url: Optional[str] = None
        self._sink: Optional[str] = None
        self._started_at: float = 0.0
        self._last_error: Optional[str] = None
        self._reaper: Optional[threading.Thread] = None
        self._timeout_thread: Optional[threading.Thread] = None
        self._stop_timeout = threading.Event()

    # ── Service API ────────────────────────────────────────────────────

    def start(self) -> None:
        if not shutil.which("ffmpeg"):
            log.warning("MusicService: 'ffmpeg' not in PATH — /music will return 501")

    def stop(self) -> None:
        self._kill_locked(reason="service shutdown")

    def health(self) -> Dict[str, Any]:
        with self._lock:
            playing = self._proc is not None and self._proc.poll() is None
            return {
                "available": shutil.which("ffmpeg") is not None,
                "playing": playing,
                "url": self._url if playing else None,
                "sink": self._sink if playing else None,
                "uptime_s": round(time.monotonic() - self._started_at, 1) if playing else 0.0,
                "last_error": self._last_error,
            }

    # ── HTTP API ───────────────────────────────────────────────────────

    def http_router(self):
        if _router_import_error is not None:
            log.warning("MusicService: fastapi unavailable (%s)", _router_import_error)
            return None

        router = APIRouter(prefix="/music", tags=["music"])

        @router.post("", status_code=202)
        def play(
            payload: _PlayPayload = Body(...),
            x_api_key: Optional[str] = Header(default=None),
        ):
            _check_auth(x_api_key)
            url = (payload.url or "").strip()
            if not url:
                raise HTTPException(status_code=400, detail="url is required")
            if not _looks_like_url(url):
                raise HTTPException(status_code=400, detail="url must start with http(s):// or be a local file")
            if not shutil.which("ffmpeg"):
                raise HTTPException(status_code=501, detail="ffmpeg is not installed on the robot")
            ok, msg = self._start_playback(url, payload.gain_db)
            if not ok:
                raise HTTPException(status_code=502, detail=msg or "could not start playback")
            return {"ok": True, "playing": True, "url": url, "sink": self._sink}

        @router.post("/stop", status_code=200)
        def stop(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            stopped = self._kill_locked(reason="api /music/stop")
            return {"ok": True, "stopped": stopped}

        @router.get("")
        def status(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            return self.health()

        return router

    # ── Internals ──────────────────────────────────────────────────────

    def _start_playback(self, url: str, gain_db: float) -> tuple[bool, Optional[str]]:
        with self._lock:
            self._kill_unlocked(reason="replaced by new track")

            sink = audio_sink.resolved_sink()
            cmd = self._build_pipeline(url, gain_db, sink)
            log.info("Music: starting (sink=%s url=%s)", sink or "<system-cmd>", url)
            try:
                # `start_new_session=True` puts the whole pipeline (sh+ffmpeg
                # +paplay) into its own process group so a single os.killpg
                # tears the lot down.
                proc = subprocess.Popen(
                    cmd,
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except Exception as e:
                msg = f"spawn failed: {e}"
                log.error("Music: %s", msg)
                self._last_error = msg
                return False, msg

            self._proc = proc
            self._url = url
            self._sink = sink
            self._started_at = time.monotonic()
            self._last_error = None

            self._stop_timeout.clear()
            self._reaper = threading.Thread(target=self._reap, name="music-reap", daemon=True)
            self._reaper.start()

            self._timeout_thread = threading.Thread(
                target=self._enforce_timeout, name="music-timeout", daemon=True,
            )
            self._timeout_thread.start()
            return True, None

    def _build_pipeline(self, url: str, gain_db: float, sink: Optional[str]) -> str:
        # ffmpeg → raw PCM (s16le 44.1k stereo) on stdout.
        ffmpeg = (
            f"ffmpeg -nostdin -loglevel error -re -i {shlex.quote(url)} "
            f"-vn -ac 2 -ar 44100 -sample_fmt s16 -f s16le"
        )
        if abs(gain_db) > 0.01:
            ffmpeg += f" -af volume={gain_db}dB"
        ffmpeg += " -"

        if sink:
            player = (
                f"paplay --device={shlex.quote(sink)} --client-name=g1-core-music "
                f"--raw --rate=44100 --format=s16le --channels=2"
            )
        else:
            # Mirror SystemAudioPlayer's fallback: aplay direct to ALSA.
            player = "aplay -q -t raw -f S16_LE -r 44100 -c 2"

        return f"{ffmpeg} | {player}"

    def _reap(self) -> None:
        proc = self._proc
        if proc is None:
            return
        rc = proc.wait()
        with self._lock:
            stderr = b""
            if proc.stderr is not None:
                try:
                    stderr = proc.stderr.read() or b""
                except Exception:
                    pass
            if proc is self._proc:  # not replaced by another track
                self._proc = None
                self._url = None
                self._sink = None
                self._stop_timeout.set()
                if rc != 0 and rc != -signal.SIGTERM:
                    msg = stderr.decode(errors="replace").strip()[:300]
                    self._last_error = f"exit {rc}: {msg}" if msg else f"exit {rc}"
                    log.warning("Music: pipeline ended with %s", self._last_error)
                else:
                    log.info("Music: finished cleanly (rc=%s)", rc)

    def _enforce_timeout(self) -> None:
        # Hard kill anything that's been playing longer than _MAX_TRACK_SECONDS,
        # even if reap is still waiting on a stuck ffmpeg.
        triggered = self._stop_timeout.wait(timeout=float(_MAX_TRACK_SECONDS))
        if triggered:
            return
        log.warning("Music: hit %ds ceiling — stopping", _MAX_TRACK_SECONDS)
        self._kill_locked(reason="max duration ceiling")

    def _kill_locked(self, reason: str) -> bool:
        with self._lock:
            return self._kill_unlocked(reason=reason)

    def _kill_unlocked(self, reason: str) -> bool:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        log.info("Music: stopping (%s)", reason)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as e:
            log.debug("Music: killpg failed (%s) — falling back to terminate", e)
            try:
                proc.terminate()
            except Exception:
                pass
        return True


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https"):
        return bool(parsed.netloc)
    if value.startswith("/") and "://" not in value:
        return True
    return False
