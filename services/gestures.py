"""
Gesture Service
===============
Thin HTTP wrapper around `app/gestures.py`. Mirrors the shape of
`services/music.py` so the operator panel only ever has to know one
service contract.

Endpoints
---------
    GET  /gesture/list                — whitelist + active intensity profile
    POST /gesture {action,command_id?} — queue one whitelisted gesture
    POST /gesture/release             — return arms to neutral (no-op safe)
    GET  /gesture                     — status snapshot (last action, skip
                                        reason, arm availability)

The POST routes go through the command bus so multiple operators don't
race the SDK; the bus' per-command idempotency means a button mash-tap
only fires once.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Union

from pydantic import BaseModel

from app import command_bus, gestures
from app.command_bus import bus
from app.config import settings
from services.base import Service

log = logging.getLogger(__name__)

_router_import_error: Optional[Exception] = None
try:
    from fastapi import APIRouter, Body, Header, HTTPException
except ImportError as e:  # pragma: no cover
    APIRouter = Body = Header = HTTPException = None  # type: ignore[assignment]
    _router_import_error = e


class _GesturePayload(BaseModel):
    # Accept either the whitelisted name ("face_wave") or the numeric id
    # (25). Anything outside the whitelist is rejected at the gestures
    # module level — we just normalize here for the bus payload.
    action: Union[str, int]
    command_id: Optional[str] = None


def _check_auth(x_api_key: Optional[str]) -> None:
    if not settings.webhook_api_key:
        return
    if x_api_key != settings.webhook_api_key:
        raise HTTPException(status_code=401, detail="invalid api key")


def _client_source(forwarded_for: Optional[str]) -> str:
    if not forwarded_for or not forwarded_for.strip():
        return "webhook:direct"
    return f"webhook:{forwarded_for.split(',')[0].strip()}"


class GestureService(Service):
    name = "gestures"

    def health(self) -> Dict[str, Any]:
        snap = gestures.status_snapshot()
        # Surface the master settings flag separately so the panel can
        # tell the difference between "personality muted" and "killed
        # globally in .env".
        snap["master_enabled"] = bool(settings.gestures_enabled)
        return snap

    def http_router(self):
        if _router_import_error is not None:
            log.warning("GestureService: fastapi unavailable (%s)", _router_import_error)
            return None

        router = APIRouter(prefix="/gesture", tags=["gesture"])

        @router.get("/list")
        def list_safe(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            return {
                "actions": gestures.list_safe_actions(),
                "profile": gestures.status_snapshot(),
            }

        @router.get("")
        def status(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            return self.health()

        @router.post("", status_code=202)
        def fire(
            payload: _GesturePayload = Body(...),
            x_api_key: Optional[str] = Header(default=None),
            x_forwarded_for: Optional[str] = Header(default=None, alias="X-Forwarded-For"),
        ):
            _check_auth(x_api_key)
            action = payload.action
            # Accept "25" coming from a form input as a numeric id.
            if isinstance(action, str):
                stripped = action.strip()
                if stripped.isdigit():
                    action = int(stripped)
                else:
                    action = stripped.lower().replace("-", "_").replace(" ", "_")
            if isinstance(action, int):
                if action not in {v for v in gestures.SAFE_ACTIONS.values()}:
                    raise HTTPException(status_code=400, detail="action id not in whitelist")
            elif isinstance(action, str):
                if action not in gestures.SAFE_ACTIONS:
                    raise HTTPException(status_code=400, detail=f"unknown action '{action}'")
            else:
                raise HTTPException(status_code=400, detail="action must be string or int")

            cmd = bus.submit(
                kind=command_bus.KIND_GESTURE,
                payload={"action": action},
                source=_client_source(x_forwarded_for),
                command_id=payload.command_id,
            )
            base = cmd.to_dict()
            base.setdefault("queued", "GESTURE")
            base.setdefault("action", action)
            return base

        @router.post("/release", status_code=200)
        def release(
            x_api_key: Optional[str] = Header(default=None),
        ):
            _check_auth(x_api_key)
            # Release runs synchronously — it's a 1-call safety op and
            # the operator will mash-tap it; queueing would just delay.
            result = gestures.release()
            return result

        return router
