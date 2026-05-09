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
import threading
import time
from typing import Any, Dict, List

from rich.console import Console
from rich.logging import RichHandler

from app import __version__, git_full_sha, git_repo_url, git_sha, version_string
from app import audio_sink
from app import bluetooth as bt
from app import command_bus, log_ring, personality
from app.command_bus import bus
from app.config import settings
from app.robot import robot
from app.speaker import say, speak_personality_intro
from services.base import Service
from services.bluetooth import BluetoothService
from services.music import MusicService
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
# Capture every record into an in-process ring so /logs works even when
# the user-journal is empty (very common — Ubuntu defaults persist only
# the *system* journal). RichHandler stays for the terminal, RingHandler
# powers the operator's web log panel.
log_ring.install(capacity=4000)
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
        el_err = tts.get_session_error()
        pending_n = bus.pending()
        services_health = {svc.name: svc.health() for svc in self._services}
        music_h = services_health.get("music") or {}
        music_playing = bool(music_h.get("playing"))
        music_url = music_h.get("url") if music_playing else None
        sha_short = git_sha()
        sha_full = git_full_sha()
        repo_url = git_repo_url()
        commit_url = (
            f"{repo_url}/commit/{sha_full}" if repo_url and sha_full else None
        )
        snap = {
            "ok": True,
            "version": __version__,
            "version_full": version_string(),
            # Operator-panel deep-link to the exact commit running on the
            # robot. Empty fields if this isn't a git checkout.
            "git": {
                "sha": sha_short,
                "sha_full": sha_full,
                "repo_url": repo_url,
                "commit_url": commit_url,
            },
            # g1-core native shape (nested)
            "personality_detail": {
                "active": per.slug,
                "display_name": per.display_name,
            },
            "robot": robot.health(),
            "tts": {"session_error": el_err},
            "audio_output": settings.audio_output,
            "services": services_health,
            "queue": {"pending": pending_n, "history": len(bus.recent(limit=settings.command_history_size))},
            "uptime_s": round(time.monotonic() - _started_at, 1),
            # ── Robohire / g1-brain-compatible flat fields ──────────────────
            "brain": "g1-core",
            "personality": per.slug,
            "pending": pending_n,
            "commands_paused": False,
            "voice_loop_paused": False,
            "elevenlabs_session_error": el_err,
            "music_playing": music_playing,
            "music_prompt": music_url,
        }
        return snap


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


def _submit_boot_greet(max_wait_s: float = 6.0) -> None:
    """Wait for the BT speaker to actually be connected (so the greet
    plays through JBL, not the chest), then submit the greet command.
    Falls back gracefully — we never block the main loop and never skip
    the greet entirely. If BT isn't configured we just speak immediately."""
    target = (settings.bluetooth_mac or "").strip()
    if not target or not bt.is_available():
        bus.submit(command_bus.KIND_GREET, source="boot")
        return
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        try:
            if bt.is_connected(target):
                break
        except Exception:
            pass
        time.sleep(0.5)
    bus.submit(command_bus.KIND_GREET, source="boot")


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
    ver = version_string()
    console.rule(f"[bold]g1-core[/] [dim]v{ver}[/]")
    # Same line in plain log so journalctl tail makes the version obvious.
    log.info("g1-core version %s", ver)
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
    service_registry.register(MusicService())
    # WebhookService gets the registry explicitly: when started via
    # `python3 -m main` the running module is `__main__`, while `from main
    # import service_registry` would re-import a fresh, empty copy under
    # the name `main` — leaving the webhook with zero routers to mount.
    service_registry.register(WebhookService(registry=service_registry))
    service_registry.register(TunnelService())
    service_registry.register(WatchdogService())
    service_registry.start_all()

    # Keep the active PulseAudio sink awake so the JBL never drops to
    # standby between utterances (PA suspends idle sinks at 5 s; we poke
    # every 3 s). Watchdog also calls keepalive on its tick — this thread
    # makes sure we don't depend on its 15 s cadence for the JBL link.
    audio_sink.start_keepalive()

    _print_banner()

    # Defer the boot greet until BT is online (or after a short timeout).
    # Without this the very first utterance frequently lands on the chest
    # speaker because the bluez sink isn't registered with PulseAudio yet
    # — annoying, even if technically harmless.
    threading.Thread(target=_submit_boot_greet, name="boot-greet", daemon=True).start()

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
        audio_sink.stop_keepalive()
        service_registry.stop_all()
        robot.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
