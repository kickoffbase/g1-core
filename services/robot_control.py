"""
Safe Robot Control Service
==========================
Small admin-facing SDK surface for useful high-level controls that do not
move the robot's base: capabilities, LED, volume, and safe runtime config.

No locomotion endpoints live here. LocoClient may be initialized by
app.robot, but this service intentionally never exposes Move/Turn/raw SDK.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from app import gestures
from app.config import settings
from app.robot import robot
from services.base import Service

log = logging.getLogger(__name__)

_router_import_error: Optional[Exception] = None
try:
    from fastapi import APIRouter, Body, Header, HTTPException
except ImportError as e:  # pragma: no cover
    APIRouter = Body = Header = HTTPException = None  # type: ignore[assignment]
    _router_import_error = e


LED_PRESETS = {
    "off": (0, 0, 0),
    "white": (255, 255, 255),
    "soft_white": (80, 70, 55),
    "blue": (0, 80, 255),
    "green": (0, 180, 60),
    "amber": (255, 120, 0),
    "red": (255, 0, 0),
}


class _LedPayload(BaseModel):
    preset: Optional[str] = None
    r: Optional[int] = Field(default=None, ge=0, le=255)
    g: Optional[int] = Field(default=None, ge=0, le=255)
    b: Optional[int] = Field(default=None, ge=0, le=255)


class _VolumePayload(BaseModel):
    volume: int = Field(ge=0, le=100)


class _ConfigPayload(BaseModel):
    speech_enabled: Optional[bool] = None
    manual_restricted_enabled: Optional[bool] = None


def _check_auth(x_api_key: Optional[str]) -> None:
    if not settings.webhook_api_key:
        return
    if x_api_key != settings.webhook_api_key:
        raise HTTPException(status_code=401, detail="invalid api key")


def _capabilities() -> Dict[str, Any]:
    health = robot.health()
    gesture_status = gestures.status_snapshot()
    return {
        "robot": health,
        "connected": robot.connected,
        "audio_available": robot.connected,
        "arm_available": robot.arm_available,
        "locomotion_exposed": False,
        "sdk_actions": gestures.action_catalog()["actions"],
        "gesture_policy": gestures.policy_snapshot(),
        "gesture_status": gesture_status,
        "led_presets": sorted(LED_PRESETS),
        "volume": {
            "configured_default": settings.robot_volume,
            "min": 0,
            "max": 100,
        },
    }


class RobotControlService(Service):
    name = "robot_control"

    def health(self) -> Dict[str, Any]:
        return {
            "connected": robot.connected,
            "arm_available": robot.arm_available,
            "locomotion_exposed": False,
        }

    def http_router(self):
        if _router_import_error is not None:
            log.warning("RobotControlService: fastapi unavailable (%s)", _router_import_error)
            return None

        router = APIRouter(prefix="/robot", tags=["robot"])

        @router.get("/capabilities")
        def capabilities(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            return _capabilities()

        @router.get("/config")
        def get_config(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            return {
                "speech_enabled": gestures.is_speech_enabled(),
                "manual_restricted_enabled": gestures.is_manual_restricted_enabled(),
                "gesture_policy": gestures.policy_snapshot(),
                "led_presets": sorted(LED_PRESETS),
                "volume": {
                    "configured_default": settings.robot_volume,
                    "min": 0,
                    "max": 100,
                },
            }

        @router.post("/config")
        def set_config(
            payload: _ConfigPayload = Body(...),
            x_api_key: Optional[str] = Header(default=None),
        ):
            _check_auth(x_api_key)
            if payload.speech_enabled is not None:
                gestures.set_speech_enabled(payload.speech_enabled)
            if payload.manual_restricted_enabled is not None:
                gestures.set_manual_restricted_enabled(payload.manual_restricted_enabled)
            return {
                "speech_enabled": gestures.is_speech_enabled(),
                "manual_restricted_enabled": gestures.is_manual_restricted_enabled(),
                "gesture_policy": gestures.policy_snapshot(),
            }

        @router.post("/led")
        def led(
            payload: _LedPayload = Body(...),
            x_api_key: Optional[str] = Header(default=None),
        ):
            _check_auth(x_api_key)
            if payload.preset:
                preset = payload.preset.strip().lower()
                if preset not in LED_PRESETS:
                    raise HTTPException(status_code=400, detail="unknown LED preset")
                r, g, b = LED_PRESETS[preset]
            else:
                if payload.r is None or payload.g is None or payload.b is None:
                    raise HTTPException(status_code=400, detail="preset or r/g/b is required")
                r, g, b = payload.r, payload.g, payload.b
                preset = None
            ok = robot.set_led(int(r), int(g), int(b))
            return {
                "ok": ok,
                "skipped": None if ok else "audio_client_unavailable",
                "preset": preset,
                "rgb": [r, g, b],
            }

        @router.post("/volume")
        def volume(
            payload: _VolumePayload = Body(...),
            x_api_key: Optional[str] = Header(default=None),
        ):
            _check_auth(x_api_key)
            ok = robot.set_volume(int(payload.volume))
            return {
                "ok": ok,
                "skipped": None if ok else "audio_client_unavailable",
                "volume": int(payload.volume),
            }

        return router
