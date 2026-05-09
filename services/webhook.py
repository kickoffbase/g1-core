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

import json
import logging
import subprocess
import threading
from typing import Any, Dict, Optional

from pydantic import BaseModel

from app import command_bus, log_ring, personality
from app import version_string
from app.command_bus import bus
from app.config import settings
from services.base import Service

log = logging.getLogger(__name__)

_fastapi_import_error: Optional[Exception] = None
try:
    from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
    from fastapi.responses import PlainTextResponse
    import uvicorn
except ImportError as e:  # pragma: no cover — only happens on a misconfigured host
    Body = FastAPI = Header = HTTPException = Query = Request = None  # type: ignore[assignment]
    PlainTextResponse = None  # type: ignore[assignment]
    uvicorn = None  # type: ignore[assignment]
    _fastapi_import_error = e


class SayPayload(BaseModel):
    text: str
    command_id: Optional[str] = None


class PersonalityPayload(BaseModel):
    slug: str


def _client_source(forwarded_for: Optional[str]) -> str:
    if not forwarded_for or not forwarded_for.strip():
        return "webhook:direct"
    return f"webhook:{forwarded_for.split(',')[0].strip()}"


# Robohire forwards /logs with unit=g1-brain; g1-core runs as a user service
# with ngrok in-process — everything lands in the same journal.
_LOG_UNITS = frozenset(
    {
        "g1-core",
        "g1-core.service",
        "g1-brain",
        "g1-brain.service",
        "ngrok",
        "ngrok.service",
    }
)


def _render_ring(lines: int) -> str:
    handler = log_ring.get()
    if handler is None:
        return ""
    return handler.render(limit=lines)


def _debug_snapshot() -> Dict[str, Any]:
    """Build the rich debug JSON. Every probe is best-effort: a missing
    binary or non-zero exit becomes a string error in its slot, the
    rest of the dict is unaffected."""
    import os
    import platform
    import sys

    from main import service_registry  # late import to avoid cycles

    health = service_registry.health_snapshot()

    sinks = _run_text(["pactl", "list", "sinks", "short"], timeout=4)
    sources = _run_text(["pactl", "list", "sources", "short"], timeout=4)
    pa_info = _run_text(["pactl", "info"], timeout=4)
    bt_devices = _run_text(["bluetoothctl", "devices", "Connected"], timeout=4)
    bt_paired = _run_text(["bluetoothctl", "paired-devices"], timeout=4)
    listening = _run_text(
        ["ss", "-tlnp"], timeout=4,
        fallback=["netstat", "-tlnp"],
    )
    procs = _run_text(["pgrep", "-af", "g1-core|ngrok|paplay|aplay|ffmpeg"], timeout=4)

    return {
        "version": version_string(),
        "health": health,
        "host": {
            "uname": platform.platform(),
            "python": sys.version.split()[0],
            "pid": os.getpid(),
            "cwd": os.getcwd(),
            "env_personality": os.environ.get("PERSONALITY"),
            "env_audio_output": os.environ.get("AUDIO_OUTPUT"),
        },
        "pulseaudio": {
            "info": pa_info,
            "sinks": sinks,
            "sources": sources,
        },
        "bluetooth": {
            "connected": bt_devices,
            "paired": bt_paired,
        },
        "ports": {
            "listening": listening,
            "webhook": settings.webhook_port,
        },
        "processes": procs,
    }


def _run_text(args: list, timeout: float = 4.0, fallback: Optional[list] = None) -> str:
    """Run a probe and return stdout. On error, return a one-line marker
    like '<unavailable: ...>' so the operator UI can still render the
    field instead of erroring out the whole /debug call."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        if fallback is not None:
            return _run_text(fallback, timeout=timeout)
        return f"<unavailable: {args[0]} not installed>"
    except subprocess.TimeoutExpired:
        return f"<timeout: {' '.join(args[:2])}>"
    except Exception as e:
        return f"<error: {e}>"
    if r.returncode != 0 and not r.stdout:
        return f"<exit {r.returncode}: {(r.stderr or '').strip()[:200]}>"
    return r.stdout or r.stderr or ""


def _read_journal(lines: int) -> tuple[str, Optional[str]]:
    """Try every reasonable journalctl invocation and return the first
    one that produces output. Returns (text, error_for_operator)."""
    candidates: list[list[str]] = [
        # Persistent system journal filtered by user-unit metadata —
        # works even when the user-journal directory does not exist.
        ["journalctl", "_SYSTEMD_USER_UNIT=g1-core.service",
         "-n", str(lines), "--no-pager", "--output=short-iso"],
        # User-instance journal, if it exists.
        ["journalctl", "--user", "-u", "g1-core.service",
         "-n", str(lines), "--no-pager", "--output=short-iso"],
        # Last resort — the system unit (in case the user installed it
        # the legacy way).
        ["journalctl", "-u", "g1-core.service",
         "-n", str(lines), "--no-pager", "--output=short-iso"],
    ]
    last_err: Optional[str] = None
    for cmd in candidates:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        except FileNotFoundError:
            return "", "journalctl not available"
        except subprocess.TimeoutExpired:
            last_err = f"journalctl timed out: {' '.join(cmd[:3])}"
            continue
        out = (result.stdout or "").strip()
        if result.returncode == 0 and out and "-- No entries --" not in out:
            return result.stdout, None
        if result.returncode != 0:
            last_err = (result.stderr or result.stdout or "journalctl failed").strip()[:300]
    return "", last_err


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
            # `info` so server lifecycle lines reach the operator log;
            # access_log stays off — every /health probe would spam it.
            log_level="info",
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
        services = service_registry.services()
        log.info("Webhook: scanning %d service(s) for HTTP routers", len(services))
        for svc in services:
            if svc is self:
                continue
            try:
                router = svc.http_router()
            except Exception as e:
                log.warning("Service %s: http_router() raised: %s", svc.name, e, exc_info=True)
                continue
            if router is None:
                log.info("Service %s: no HTTP router", svc.name)
                continue
            try:
                app.include_router(router)
                log.info("Mounted HTTP router from service: %s", svc.name)
            except Exception as e:
                log.warning(
                    "Service %s: include_router failed: %s", svc.name, e,
                    exc_info=True,
                )

        @app.get("/health")
        def health():
            from main import service_registry  # late import: avoid cycle at module load
            return service_registry.health_snapshot()

        @app.get("/debug")
        def debug(x_api_key: Optional[str] = Header(default=None)):
            """Heavy diagnostic snapshot for the operator panel.

            Strictly read-only — runs `pactl`, `ss`, `bluetoothctl`,
            `pgrep` and stitches their output together with the in-process
            `/health` data. Auth-protected because `pactl list` leaks
            sink names and process titles."""
            _check_auth(x_api_key)
            return _debug_snapshot()

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
            out = cmd.to_dict()
            out.setdefault("queued", "SAY")
            out.setdefault("text", text)
            return out

        @app.post("/greet", status_code=202)
        async def greet(
            request: "Request",
            x_api_key: Optional[str] = Header(default=None),
            x_forwarded_for: Optional[str] = Header(default=None, alias="X-Forwarded-For"),
        ):
            """Must accept POST with **no JSON body** — Robohire's panel sends
            a bare POST; g1-brain never used a Body() here."""
            _check_auth(x_api_key)
            command_id: Optional[str] = None
            try:
                raw = await request.body()
                if raw and raw.strip():
                    data = json.loads(raw.decode("utf-8"))
                    if isinstance(data, dict):
                        cid = data.get("command_id")
                        if cid is not None:
                            command_id = str(cid).strip() or None
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                command_id = None

            cmd = bus.submit(
                kind=command_bus.KIND_GREET,
                source=_client_source(x_forwarded_for),
                command_id=command_id,
            )
            # Blend g1-brain shape with g1-core's rich command tracking.
            base = cmd.to_dict()
            base.setdefault("queued", "GREET")
            return base

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
            return {
                "ok": True,
                "active": persona.slug,
                "display_name": persona.display_name,
            }

        @app.get("/logs", response_class=PlainTextResponse)
        def logs(
            unit: str = Query("g1-core"),
            lines: int = Query(300, ge=1, le=2000),
            source: str = Query("auto", pattern="^(auto|ring|journal)$"),
            x_api_key: Optional[str] = Header(default=None),
        ):
            """Return the last N log lines.

            Sources, in order of reliability:
              - `ring`    : in-process ring buffer (always works).
              - `journal` : `journalctl` (system + user). May be empty if
                            the host has no persistent user journal.
              - `auto`    : journal first; fall back to ring if journal is
                            unreachable / empty. We append a marker so the
                            operator can tell which source served them."""
            _check_auth(x_api_key)
            u = (unit or "").strip()
            if u not in _LOG_UNITS:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown unit (allowed: {sorted(_LOG_UNITS)})",
                )

            if source == "ring":
                return PlainTextResponse(_render_ring(lines))

            journal_text, journal_err = _read_journal(lines)

            if source == "journal":
                if journal_err:
                    raise HTTPException(status_code=500, detail=journal_err)
                return PlainTextResponse(journal_text or "(no output)")

            # source == "auto"
            ring_text = _render_ring(lines)
            if journal_text and journal_text.strip():
                # Both available — prefer journal (richer timestamps),
                # show the in-process tail too if journal is short.
                if journal_text.count("\n") >= max(20, lines // 5):
                    return PlainTextResponse(journal_text)
                merged = (
                    f"{journal_text.rstrip()}\n"
                    f"--- in-process ring tail (journal short) ---\n"
                    f"{ring_text}"
                )
                return PlainTextResponse(merged)

            header = "(journal empty or unavailable, falling back to in-process ring)"
            if journal_err:
                header = f"(journal: {journal_err[:200]})"
            body = ring_text or "(no log records yet — service just started?)"
            return PlainTextResponse(f"{header}\n{body}")

        return app
