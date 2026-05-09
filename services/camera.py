"""
Camera Service
==============
Streams a single V4L2 camera (/dev/video* — built-in G1 head RGB, USB
webcam, anything `cv2.VideoCapture` can open) over plain HTTP MJPEG so
the operator panel can show a live preview with nothing more exotic
than `<img src="…/camera/stream.mjpg" />`.

Why MJPEG and not WebRTC
------------------------
WebRTC is the "right" answer for sub-100ms two-way video, but it drags
in signaling, STUN/TURN, peer connections and a *lot* of moving parts
that don't survive a misconfigured firewall. MJPEG over HTTP:

  * is a single GET request, so it tunnels through ngrok unchanged;
  * works in every browser via `<img>` with no JS at all;
  * lets us reuse the same `X-API-Key` auth as everything else;
  * is good for ~5–15 fps of debug-grade preview, which is what an
    operator panel actually needs.

Endpoints
---------
    GET /camera/snapshot.jpg            single JPEG (one-shot capture)
    GET /camera/stream.mjpg             multipart/x-mixed-replace MJPEG
    GET /camera                          status snapshot

Lazy lifecycle
--------------
The capture device is opened on the **first** subscriber and closed
`CAMERA_IDLE_CLOSE_S` seconds after the **last** subscriber leaves —
so when nobody is watching we don't burn CPU/USB bandwidth or block
other consumers (e.g. SDK calls that want the head camera).
"""

from __future__ import annotations

import io
import logging
import os
import threading
import time
from typing import Any, Dict, Iterator, Optional

from app.config import settings
from services.base import Service

log = logging.getLogger(__name__)

_router_import_error: Optional[Exception] = None
try:
    from fastapi import APIRouter, Header, HTTPException, Response
    from fastapi.responses import StreamingResponse
except ImportError as e:  # pragma: no cover
    APIRouter = Header = HTTPException = Response = StreamingResponse = None  # type: ignore[assignment]
    _router_import_error = e

# OpenCV is the only hard dependency. We don't crash the whole service
# at import time when it's missing — `start()` warns and the endpoints
# return 501, exactly like MusicService does for ffmpeg.
_cv2_import_error: Optional[Exception] = None
try:
    import cv2  # type: ignore
except Exception as e:  # pragma: no cover - covers ImportError + native loader errors
    cv2 = None  # type: ignore[assignment]
    _cv2_import_error = e


# Multipart boundary used for the MJPEG stream. The exact string doesn't
# matter as long as it never appears in the JPEG payload.
_BOUNDARY = "g1corecam"


def _check_auth(x_api_key: Optional[str]) -> None:
    if not settings.webhook_api_key:
        return
    if x_api_key != settings.webhook_api_key:
        raise HTTPException(status_code=401, detail="invalid api key")


def _device_from_env() -> Any:
    """Return the value to pass to `cv2.VideoCapture`. Accepts either an
    integer index ("0", "2") or a /dev path ("/dev/video2")."""
    raw = os.getenv("CAMERA_DEVICE", "0").strip() or "0"
    try:
        return int(raw)
    except ValueError:
        return raw


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(lo, min(hi, v))


class CameraService(Service):
    name = "camera"

    def __init__(self) -> None:
        self._enabled = os.getenv("CAMERA_ENABLED", "true").lower() in ("1", "true", "yes")
        self._device = _device_from_env()
        self._width = _env_int("CAMERA_WIDTH", 640, 64, 4096)
        self._height = _env_int("CAMERA_HEIGHT", 480, 64, 4096)
        self._fps = _env_int("CAMERA_FPS", 10, 1, 60)
        self._jpeg_quality = _env_int("CAMERA_JPEG_QUALITY", 70, 1, 100)
        self._idle_close_s = _env_int("CAMERA_IDLE_CLOSE_S", 10, 0, 600)

        self._lock = threading.Lock()
        self._cap = None  # cv2.VideoCapture | None
        self._capture_thread: Optional[threading.Thread] = None
        self._stop_capture = threading.Event()

        # Latest encoded JPEG + a "new frame ready" event for subscribers.
        # `frame_event` is a Condition so multiple stream consumers can
        # wait on the same notification cheaply.
        self._latest_jpeg: Optional[bytes] = None
        self._latest_ts: float = 0.0
        self._frame_cond = threading.Condition()

        self._subscribers = 0
        self._idle_timer: Optional[threading.Timer] = None

        self._open_error: Optional[str] = None

    # ── Service API ────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._enabled:
            log.info("CameraService disabled (CAMERA_ENABLED=false)")
            return
        if _cv2_import_error is not None:
            log.warning(
                "CameraService: opencv-python not importable (%s) — /camera/* will return 501",
                _cv2_import_error,
            )
            return
        log.info(
            "CameraService ready (device=%s %dx%d@%dfps q=%d, lazy)",
            self._device, self._width, self._height, self._fps, self._jpeg_quality,
        )

    def stop(self) -> None:
        self._stop_capture.set()
        with self._lock:
            self._close_capture_unlocked(reason="service shutdown")

    def health(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "available": _cv2_import_error is None,
                "open": self._cap is not None,
                "device": str(self._device),
                "size": [self._width, self._height],
                "fps": self._fps,
                "subscribers": self._subscribers,
                "last_frame_age_s": (
                    round(time.monotonic() - self._latest_ts, 2)
                    if self._latest_ts else None
                ),
                "open_error": self._open_error,
            }

    # ── HTTP API ───────────────────────────────────────────────────────

    def http_router(self):
        if _router_import_error is not None:
            log.warning("CameraService: fastapi unavailable (%s)", _router_import_error)
            return None

        router = APIRouter(prefix="/camera", tags=["camera"])

        @router.get("")
        def status(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            return self.health()

        @router.get("/snapshot.jpg")
        def snapshot(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            self._require_available()
            jpg = self._capture_one_shot()
            if jpg is None:
                raise HTTPException(status_code=503, detail=self._open_error or "camera unavailable")
            return Response(
                content=jpg,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store"},
            )

        @router.get("/stream.mjpg")
        def stream(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            self._require_available()
            return StreamingResponse(
                self._mjpeg_generator(),
                media_type=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                    "Connection": "close",
                    # Defeat ngrok's HTML interstitial — same trick we use elsewhere.
                    "X-Accel-Buffering": "no",
                },
            )

        return router

    # ── Internals ──────────────────────────────────────────────────────

    def _require_available(self) -> None:
        if not self._enabled:
            raise HTTPException(status_code=503, detail="camera disabled (set CAMERA_ENABLED=true)")
        if _cv2_import_error is not None:
            raise HTTPException(
                status_code=501,
                detail=f"opencv not installed: {_cv2_import_error}",
            )

    def _capture_one_shot(self) -> Optional[bytes]:
        """Best-effort single-frame capture without bumping the subscriber
        count. If the streaming loop is already running we just hand back
        whatever frame it produced last."""
        with self._lock:
            if self._cap is not None and self._latest_jpeg is not None:
                return self._latest_jpeg

        # Nothing cached → open briefly, grab one frame, encode, close.
        cap = self._open_locked_or_none()
        if cap is None:
            return None
        try:
            for _ in range(3):  # warm-up reads — first frame from V4L2 is often green
                ok, _ = cap.read()
                if not ok:
                    continue
            ok, frame = cap.read()
            if not ok or frame is None:
                self._open_error = "read() returned no frame"
                return None
            return self._encode_jpeg(frame)
        finally:
            try:
                cap.release()
            except Exception:
                pass

    def _open_locked_or_none(self):
        """Open a capture device standalone (used only by snapshot path)."""
        if cv2 is None:
            return None
        try:
            cap = cv2.VideoCapture(self._device)
            if not cap.isOpened():
                self._open_error = f"VideoCapture({self._device}) failed to open"
                cap.release()
                return None
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self._width))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self._height))
            cap.set(cv2.CAP_PROP_FPS, float(self._fps))
            self._open_error = None
            return cap
        except Exception as e:
            self._open_error = f"open: {e}"
            return None

    def _encode_jpeg(self, frame) -> Optional[bytes]:
        if cv2 is None:
            return None
        try:
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality])
            if not ok:
                return None
            return buf.tobytes()
        except Exception as e:
            log.warning("camera: jpeg encode failed: %s", e)
            return None

    # ── Subscriber bookkeeping (lazy capture loop) ─────────────────────

    def _add_subscriber(self) -> None:
        with self._lock:
            self._subscribers += 1
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            if self._cap is None:
                self._open_capture_unlocked()

    def _drop_subscriber(self) -> None:
        with self._lock:
            self._subscribers = max(0, self._subscribers - 1)
            if self._subscribers == 0 and self._cap is not None:
                # Schedule a deferred close so quick reconnects (page
                # refresh, navigation) don't spin the camera up/down.
                if self._idle_timer is not None:
                    self._idle_timer.cancel()
                self._idle_timer = threading.Timer(
                    float(self._idle_close_s), self._idle_close,
                )
                self._idle_timer.daemon = True
                self._idle_timer.start()

    def _idle_close(self) -> None:
        with self._lock:
            if self._subscribers == 0:
                self._close_capture_unlocked(reason=f"idle for {self._idle_close_s}s")
            self._idle_timer = None

    def _open_capture_unlocked(self) -> None:
        if cv2 is None:
            return
        cap = self._open_locked_or_none()
        if cap is None:
            return
        self._cap = cap
        self._stop_capture.clear()
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="camera-capture", daemon=True,
        )
        self._capture_thread.start()
        log.info("camera: capture started (device=%s)", self._device)

    def _close_capture_unlocked(self, reason: str) -> None:
        if self._cap is None:
            return
        log.info("camera: closing (%s)", reason)
        self._stop_capture.set()
        try:
            self._cap.release()
        except Exception:
            pass
        self._cap = None
        # Wake any sleeping subscribers so they can exit cleanly.
        with self._frame_cond:
            self._frame_cond.notify_all()

    def _capture_loop(self) -> None:
        period = 1.0 / float(self._fps)
        while not self._stop_capture.is_set():
            t0 = time.monotonic()
            cap = self._cap
            if cap is None:
                break
            try:
                ok, frame = cap.read()
            except Exception as e:
                log.warning("camera: read crashed: %s", e)
                break
            if not ok or frame is None:
                # Some V4L2 drivers blip — short sleep and retry a few
                # times before bailing out.
                time.sleep(0.05)
                continue
            jpg = self._encode_jpeg(frame)
            if jpg is None:
                continue
            with self._frame_cond:
                self._latest_jpeg = jpg
                self._latest_ts = time.monotonic()
                self._frame_cond.notify_all()
            elapsed = time.monotonic() - t0
            if elapsed < period:
                time.sleep(period - elapsed)
        log.info("camera: capture loop exited")

    def _mjpeg_generator(self) -> Iterator[bytes]:
        """Generator that the StreamingResponse drains. Each iteration
        either yields the next encoded frame as a multipart part, or
        exits cleanly when the client disconnects / the camera closes."""
        self._add_subscriber()
        try:
            last_ts = 0.0
            # Send an initial frame immediately if we have one cached so
            # the <img> doesn't sit blank waiting for the first capture.
            with self._frame_cond:
                if self._latest_jpeg is not None:
                    yield _multipart_chunk(self._latest_jpeg)
                    last_ts = self._latest_ts

            while True:
                with self._frame_cond:
                    # Wake on any new frame OR every 1s to notice client
                    # disconnects via the StreamingResponse send error.
                    self._frame_cond.wait(timeout=1.0)
                    if self._latest_jpeg is None or self._cap is None:
                        if self._cap is None:
                            break
                        continue
                    if self._latest_ts <= last_ts:
                        continue
                    frame = self._latest_jpeg
                    last_ts = self._latest_ts
                yield _multipart_chunk(frame)
        finally:
            self._drop_subscriber()


def _multipart_chunk(jpeg: bytes) -> bytes:
    head = (
        f"--{_BOUNDARY}\r\n"
        f"Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(jpeg)}\r\n\r\n"
    ).encode("ascii")
    return head + jpeg + b"\r\n"


# Keep this last so the module import never fails because of it.
_ = io  # silence unused-import linters in environments without cv2
