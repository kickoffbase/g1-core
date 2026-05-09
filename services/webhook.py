"""
Webhook Service
===============
FastAPI server with the smallest possible external command surface:

    GET  /health                           service + subsystem status
    POST /say          {text, command_id?} queue a SAY (idempotent)
    POST /greet        {command_id?}       queue the personality intro line
    GET  /commands/{id}                    poll status of a previously queued cmd
    GET  /commands?limit=N                 last N commands (queued + history)
    GET  /control/personalities            list available + active slug
    POST /control/personality {slug}       switch personality (persisted)

Everything runs in a daemon thread; a fault in uvicorn never takes the
robot loop down. Auth: if `WEBHOOK_API_KEY` is set, every request must
carry it as `X-API-Key`.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from pydantic import BaseModel

from app import command_bus, personality
from app.command_bus import bus
from app.config import settings
from services.base import Service

log = logging.getLogger(__name__)

_fastapi_import_error: Optional[Exception] = None
try:
    from fastapi import Body, FastAPI, Header, HTTPException, Query
    import uvicorn
except ImportError as e:  # pragma: no cover — only happens on a misconfigured host
    Body = FastAPI = Header = HTTPException = Query = None  # type: ignore[assignment]
    uvicorn = None  # type: ignore[assignment]
    _fastapi_import_error = e


class SayPayload(BaseModel):
    text: str
    command_id: Optional[str] = None


class GreetPayload(BaseModel):
    command_id: Optional[str] = None


class PersonalityPayload(BaseModel):
    slug: str


def _client_source(forwarded_for: Optional[str]) -> str:
    if not forwarded_for or not forwarded_for.strip():
        return "webhook:direct"
    return f"webhook:{forwarded_for.split(',')[0].strip()}"


class WebhookService(Service):
    name = "webhook"

    def __init__(self) -> None:
        self._server: Optional["uvicorn.Server"] = None
        self._thread: Optional[threading.Thread] = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        if _fastapi_import_error is not None:
            log.error("Webhook unavailable — install: pip install fastapi uvicorn (%s)",
                      _fastapi_import_error)
            return

        app = self._build_app()
        config = uvicorn.Config(
            app,
            host=settings.webhook_host,
            port=settings.webhook_port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        def _run():
            try:
                self._server.run()
            except Exception as e:
                log.error("Webhook server crashed: %s", e, exc_info=True)

        self._thread = threading.Thread(target=_run, name="webhook", daemon=True)
        self._thread.start()
        self._started = True
        auth_note = "with API key" if settings.webhook_api_key else "OPEN — bind to 127.0.0.1 if no LAN trust"
        log.info("Webhook listening on http://%s:%d (%s)",
                 settings.webhook_host, settings.webhook_port, auth_note)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True

    def health(self) -> Dict[str, Any]:
        return {
            "running": self._started,
            "host": settings.webhook_host,
            "port": settings.webhook_port,
            "auth": bool(settings.webhook_api_key),
            "pending": bus.pending(),
        }

    # ── Internals ──────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(title="g1-core", docs_url=None, redoc_url=None)

        def _check_auth(x_api_key: Optional[str]) -> None:
            if not settings.webhook_api_key:
                return
            if x_api_key != settings.webhook_api_key:
                raise HTTPException(status_code=401, detail="invalid api key")

        # Auto-mount routers from every other registered service. This is
        # what lets `services/bluetooth.py` ship its own `/bluetooth/*`
        # endpoints without webhook.py ever importing it.
        from main import service_registry
        for svc in service_registry.services():
            if svc is self:
                continue
            try:
                router = svc.http_router()
            except Exception as e:
                log.warning("Service %s: http_router() raised: %s", svc.name, e)
                continue
            if router is None:
                continue
            app.include_router(router)
            log.info("Mounted HTTP router from service: %s", svc.name)

        @app.get("/health")
        def health():
            from main import service_registry  # late import: avoid cycle at module load
            return service_registry.health_snapshot()

        @app.post("/say", status_code=202)
        def say(
            payload: SayPayload = Body(...),
            x_api_key: Optional[str] = Header(default=None),
            x_forwarded_for: Optional[str] = Header(default=None, alias="X-Forwarded-For"),
        ):
            _check_auth(x_api_key)
            text = (payload.text or "").strip()
            if not text:
                raise HTTPException(status_code=400, detail="text is required")
            if len(text) > 600:
                raise HTTPException(status_code=400, detail="text too long (max 600 chars)")
            cmd = bus.submit(
                kind=command_bus.KIND_SAY,
                payload={"text": text},
                source=_client_source(x_forwarded_for),
                command_id=payload.command_id,
            )
            return cmd.to_dict()

        @app.post("/greet", status_code=202)
        def greet(
            payload: GreetPayload = Body(default=None),
            x_api_key: Optional[str] = Header(default=None),
            x_forwarded_for: Optional[str] = Header(default=None, alias="X-Forwarded-For"),
        ):
            _check_auth(x_api_key)
            cmd = bus.submit(
                kind=command_bus.KIND_GREET,
                source=_client_source(x_forwarded_for),
                command_id=(payload.command_id if payload else None),
            )
            return cmd.to_dict()

        @app.get("/commands/{cid}")
        def get_command(cid: str, x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            cmd = bus.get(cid)
            if cmd is None:
                raise HTTPException(status_code=404, detail="unknown command id")
            return cmd.to_dict()

        @app.get("/commands")
        def list_commands(
            limit: int = Query(50, ge=1, le=200),
            x_api_key: Optional[str] = Header(default=None),
        ):
            _check_auth(x_api_key)
            return {"items": bus.recent(limit=limit), "pending": bus.pending()}

        @app.get("/control/personalities")
        def list_personalities(x_api_key: Optional[str] = Header(default=None)):
            _check_auth(x_api_key)
            return {"active": personality.get().slug, "available": personality.list_available()}

        @app.post("/control/personality")
        def set_personality(
            payload: PersonalityPayload = Body(...),
            x_api_key: Optional[str] = Header(default=None),
        ):
            _check_auth(x_api_key)
            slug = (payload.slug or "").strip()
            if not slug:
                raise HTTPException(status_code=400, detail="slug is required")
            if slug not in personality.list_available():
                raise HTTPException(status_code=404, detail=f"unknown personality '{slug}'")
            persona = personality.set_active(slug)
            return {"active": persona.slug, "display_name": persona.display_name}

        return app
