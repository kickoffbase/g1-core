"""
Teach Service
=============
Thin HTTP wrapper around ``app/teach.py`` — the low-level arm record/replay
engine. Mirrors the shape of ``services/gestures.py``.

Endpoints
---------
    GET  /teach                      — status snapshot (mode, available, count)
    GET  /teach/list                 — recorded motions (name, duration, frames)
    POST /teach/record/start {name}  — arms go compliant, start capturing
    POST /teach/record/stop          — save recordings/<name>.json
    POST /teach/play {name}          — replay a recording (queued on the bus)
    POST /teach/stop                 — EMERGENCY: hand arms back to locomotion
    DELETE /teach/{name}             — delete a recording

Recording control runs synchronously (it just flips the engine's state and
spawns its own loop). Replay goes through the command bus so it serialises
against say()/gesture() — only one thing ever drives the robot at a time.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel

from app import command_bus, teach
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


class _NamePayload(BaseModel):
    name: str
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


class TeachService(Service):
    name = "teach"

    def health(self) -> Dict[str, Any]:
        return teach.teach.status()

    def http_router(self):
        if _router_import_error is not None:
            log.warning("TeachService: fastapi unavailable (%s)", _router_import_error)
            return None

        router = APIRouter(prefix="/teach", tags=["teach"])

        @router.get("")
        def status(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            return teach.teach.status()

        @router.get("/list")
        def list_recordings(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            return {
                "recordings": teach.list_recordings(),
                "status": teach.teach.status(),
            }

        @router.post("/record/start", status_code=200)
        def record_start(
            payload: _NamePayload = Body(...),
            x_api_key: Optional[str] = Header(default=None),
        ):
            _check_auth(x_api_key)
            return teach.teach.start_recording(payload.name)

        @router.post("/record/stop", status_code=200)
        def record_stop(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            return teach.teach.stop_recording()

        @router.post("/play", status_code=202)
        def play(
            payload: _NamePayload = Body(...),
            x_api_key: Optional[str] = Header(default=None),
            x_forwarded_for: Optional[str] = Header(default=None, alias="X-Forwarded-For"),
        ):
            _check_auth(x_api_key)
            if not settings.teach_enabled:
                raise HTTPException(status_code=409, detail="teach disabled")
            if teach.get_recording(payload.name) is None:
                raise HTTPException(status_code=404, detail=f"unknown recording '{payload.name}'")
            cmd = bus.submit(
                kind=command_bus.KIND_TEACH_PLAY,
                payload={"name": payload.name},
                source=_client_source(x_forwarded_for),
                command_id=payload.command_id,
            )
            base = cmd.to_dict()
            base.setdefault("queued", "TEACH_PLAY")
            base.setdefault("name", payload.name)
            return base

        @router.post("/stop", status_code=200)
        def emergency_stop(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            # Runs synchronously — it's a safety op; queueing would delay it.
            return teach.teach.stop()

        @router.delete("/{name}", status_code=200)
        def delete(name: str, x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            ok = teach.delete_recording(name)
            if not ok:
                raise HTTPException(status_code=404, detail=f"unknown recording '{name}'")
            return {"ok": True, "deleted": name}

        return router
