"""
g1-core — entrypoint
====================
This module's only job is to wire things together:

    1. Boot subsystems (robot connection, personality state).
    2. Register services (webhook, tunnel, watchdog) — adding a new
       feature is one `register(...)` call away, no surgery elsewhere.
    3. Drain the command bus on the main thread (only one mouth).
    4. Shut everything down cleanly on SIGINT / SIGTERM / `settings.shutdown`.

Why a single consumer thread (= the main thread itself)?
  - Audio playback is inherently serial. Two utterances at once is gibberish.
  - DDS / WebSocket clients aren't always thread-safe.
  - All concurrency lives in services (HTTP, supervisor loops). They feed
    the bus; main drains it. Easy to reason about, easy to debug.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from typing import Any, Dict, List

from rich.console import Console
from rich.logging import RichHandler

from app import command_bus, personality
from app.command_bus import bus
from app.config import settings
from app.robot import robot
from app.speaker import say, speak_personality_intro
from services.base import Service
from services.bluetooth import BluetoothService
from services.tunnel import TunnelService
from services.watchdog import WatchdogService
from services.webhook import WebhookService


console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
)
log = logging.getLogger("g1-core")


# ── Service registry ───────────────────────────────────────────────────


class ServiceRegistry:
    def __init__(self) -> None:
        self._services: List[Service] = []

    def register(self, service: Service) -> None:
        self._services.append(service)

    def services(self) -> List[Service]:
        return list(self._services)

    def start_all(self) -> None:
        for svc in self._services:
            try:
                svc.start()
                log.info("Service started: %s", svc.name)
            except Exception:
                log.exception("Service %s failed to start", svc.name)

    def stop_all(self) -> None:
        for svc in reversed(self._services):
            try:
                svc.stop()
            except Exception:
                log.exception("Service %s failed to stop", svc.name)

    def health_snapshot(self) -> Dict[str, Any]:
        from app import tts
        per = personality.get()
        return {
            "ok": True,
            "personality": {"active": per.slug, "display_name": per.display_name},
            "robot": robot.health(),
            "tts": {"session_error": tts.get_session_error()},
            "audio_output": settings.audio_output,
            "services": {svc.name: svc.health() for svc in self._services},
            "queue": {"pending": bus.pending(), "history": len(bus.recent(limit=settings.command_history_size))},
            "uptime_s": round(time.monotonic() - _started_at, 1),
        }


service_registry = ServiceRegistry()
_started_at = time.monotonic()


# ── Command dispatch ───────────────────────────────────────────────────


def _execute(cmd: command_bus.Command) -> None:
    bus.mark_running(cmd)
    try:
        if cmd.kind == command_bus.KIND_SAY:
            text = (cmd.payload.get("text") or "").strip()
            result = say(text)
        elif cmd.kind == command_bus.KIND_GREET:
            text = personality.get().intro_line or "Hello."
            result = say(text)
        elif cmd.kind == command_bus.KIND_OUTRO:
            text = personality.get().outro_line or "Goodbye."
            result = say(text)
        else:
            bus.mark_failed(cmd, f"unknown kind: {cmd.kind}")
            return

        if result.get("error"):
            bus.mark_failed(cmd, str(result["error"]))
        else:
            bus.mark_done(cmd, result=result)
    except Exception as e:
        log.exception("Command %s failed", cmd.id)
        bus.mark_failed(cmd, f"{type(e).__name__}: {e}")


# ── Lifecycle ──────────────────────────────────────────────────────────


def _install_signals() -> None:
    def handler(sig, frame):
        if settings.shutdown:
            log.warning("Force exit")
            sys.exit(1)
        log.info("Signal %s — shutting down", sig)
        settings.shutdown = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _print_banner() -> None:
    per = personality.get()
    console.rule("[bold]g1-core[/]")
    console.print(f"  Personality: [cyan]{per.slug}[/] ({per.display_name})")
    console.print(f"  Audio:       [cyan]{settings.audio_output}[/]")
    console.print(f"  Robot:       [cyan]{'enabled' if settings.robot_enabled else 'disabled'}[/] (iface={settings.network_interface})")
    console.print(f"  Webhook:     [cyan]http://{settings.webhook_host}:{settings.webhook_port}[/]")
    if settings.ngrok_enabled:
        domain = settings.ngrok_domain or "<dynamic>"
        console.print(f"  Tunnel:      [cyan]ngrok[/] domain={domain}")
    else:
        console.print("  Tunnel:      [dim]disabled[/]")
    console.rule()


def main() -> int:
    _install_signals()

    # Boot personality + robot before services so /health reflects reality
    # from the very first request.
    personality.get()
    robot.start()

    # Order matters only loosely: webhook auto-mounts http_router() of
    # every other service at start_all time, so anything providing routes
    # just needs to be registered before start_all() runs.
    service_registry.register(BluetoothService())
    service_registry.register(WebhookService())
    service_registry.register(TunnelService())
    service_registry.register(WatchdogService())
    service_registry.start_all()

    _print_banner()

    # Speak the personality intro once at boot — confirms the audio path
    # from the very first second the service is alive.
    bus.submit(command_bus.KIND_GREET, source="boot")

    try:
        while not settings.shutdown:
            cmd = bus.take(timeout=0.5)
            if cmd is None:
                continue
            log.info("Run [%s] %s from %s", cmd.kind, cmd.id, cmd.source or "?")
            _execute(cmd)
    finally:
        log.info("Shutting down services")
        # Speak outro best-effort — never block shutdown on it.
        try:
            outro = personality.get().outro_line
            if outro:
                say(outro)
        except Exception:
            pass
        service_registry.stop_all()
        robot.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
