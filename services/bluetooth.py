"""
Bluetooth Service
=================
Operator HTTP surface for managing the BT speaker without SSH:

    GET  /bluetooth/devices                  — paired + visible devices
    POST /bluetooth/scan {timeout_s?}        — active scan, then list
    POST /bluetooth/connect {mac, pair?, trust?}
    POST /bluetooth/disconnect {mac}
    POST /bluetooth/sink {mode}              — pactl set-default-sink builtin|jbl
    GET  /bluetooth/sink                     — current applied mode

This service has no background thread of its own — everything runs in
the FastAPI request handlers. The watchdog calls `app.bluetooth` directly
for periodic auto-reconnect.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel

from app import audio_sink, bluetooth
from app.config import settings
from services.base import Service

log = logging.getLogger(__name__)

_router_import_error: Optional[Exception] = None
try:
    from fastapi import APIRouter, Body, Header, HTTPException
except ImportError as e:  # pragma: no cover
    APIRouter = Body = Header = HTTPException = None  # type: ignore[assignment]
    _router_import_error = e


class _ConnectPayload(BaseModel):
    mac: str
    pair: bool = True
    trust: bool = True


class _DisconnectPayload(BaseModel):
    mac: str


class _ScanPayload(BaseModel):
    timeout_s: int = 8


class _SinkPayload(BaseModel):
    mode: str  # "builtin" | "jbl"


def _check_auth(x_api_key: Optional[str]) -> None:
    if not settings.webhook_api_key:
        return
    if x_api_key != settings.webhook_api_key:
        raise HTTPException(status_code=401, detail="invalid api key")


def _wrap(call):
    """Translate app.bluetooth exceptions into HTTP errors."""
    try:
        return call()
    except bluetooth.BluetoothUnavailable as e:
        raise HTTPException(status_code=501, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


class BluetoothService(Service):
    name = "bluetooth"

    def health(self) -> Dict[str, Any]:
        target = settings.bluetooth_mac.strip()
        return {
            "available": bluetooth.is_available(),
            "target_mac": target or None,
            "target_connected": bluetooth.is_connected(target) if target else None,
            "sink_mode": audio_sink.current(),
        }

    def http_router(self):
        if _router_import_error is not None:
            log.warning("BluetoothService: fastapi not available (%s) — endpoints disabled",
                        _router_import_error)
            return None

        router = APIRouter(prefix="/bluetooth", tags=["bluetooth"])

        @router.get("/devices")
        def list_devices(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            return {"ok": True, "devices": _wrap(bluetooth.devices)}

        @router.post("/scan")
        def scan_devices(
            payload: _ScanPayload = Body(default=_ScanPayload()),
            x_api_key: Optional[str] = Header(default=None),
        ):
            _check_auth(x_api_key)
            devices = _wrap(lambda: bluetooth.scan(payload.timeout_s))
            return {"ok": True, "timeout_s": payload.timeout_s, "devices": devices}

        @router.post("/connect")
        def connect_device(
            payload: _ConnectPayload = Body(...),
            x_api_key: Optional[str] = Header(default=None),
        ):
            _check_auth(x_api_key)
            device = _wrap(lambda: bluetooth.connect(payload.mac, payload.pair, payload.trust))
            if not device.get("connected"):
                raise HTTPException(status_code=502, detail=f"failed to connect {payload.mac}")
            return {"ok": True, "device": device}

        @router.post("/disconnect")
        def disconnect_device(
            payload: _DisconnectPayload = Body(...),
            x_api_key: Optional[str] = Header(default=None),
        ):
            _check_auth(x_api_key)
            device = _wrap(lambda: bluetooth.disconnect(payload.mac))
            return {"ok": True, "device": device}

        @router.get("/sink")
        def get_sink(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            return {
                "ok": True,
                "applied": audio_sink.current(),
                "configured": {
                    "builtin": settings.audio_sink_builtin or None,
                    "jbl": settings.audio_sink_jbl or None,
                    "default": settings.audio_sink_default,
                },
            }

        @router.post("/sink")
        def set_sink(
            payload: _SinkPayload = Body(...),
            x_api_key: Optional[str] = Header(default=None),
        ):
            _check_auth(x_api_key)
            mode = (payload.mode or "").strip().lower()
            if mode not in ("builtin", "jbl"):
                raise HTTPException(status_code=400, detail="mode must be 'builtin' or 'jbl'")
            ok = audio_sink.apply(mode)
            return {"ok": ok, "applied": audio_sink.current()}

        return router
