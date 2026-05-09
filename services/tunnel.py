"""
ngrok Tunnel Service
====================
Why we run our own supervisor instead of just `Restart=always` on a
second systemd unit:

  - We must wait until the webhook is actually bound before opening the
    tunnel — otherwise ngrok hits ERR_NGROK_8012 on cold boot.
  - Free-tier ngrok allows exactly one agent per account; before starting
    we kill any leftover `ngrok` (e.g. left over from g1-brain) and free
    the inspector port. The preflight script handles the same on service
    boot, but the supervisor re-checks at every respawn for resilience.
  - On any exit (clean or crash) we wait 3s and respawn.

Set `NGROK_DOMAIN=...` (a reserved subdomain) for a stable public URL.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from app.config import settings
from services.base import Service

log = logging.getLogger(__name__)


class TunnelService(Service):
    name = "tunnel"

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._last_public_url: Optional[str] = None
        self._last_started_at: Optional[float] = None
        self._restart_count = 0
        self._enabled = settings.ngrok_enabled

    def start(self) -> None:
        if not self._enabled:
            log.info("Tunnel disabled (NGROK_ENABLED=false)")
            return
        if shutil.which("ngrok") is None:
            log.warning("Tunnel: 'ngrok' binary not found in PATH — tunnel disabled this run")
            self._enabled = False
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name="tunnel", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    def health(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "running": bool(self._proc and self._proc.poll() is None),
            "domain": settings.ngrok_domain or None,
            "public_url": self._last_public_url,
            "started_at": self._last_started_at,
            "restart_count": self._restart_count,
        }

    # ── Internals ──────────────────────────────────────────────────────

    def _supervise(self) -> None:
        # Wait for the webhook to bind before opening the tunnel.
        for _ in range(60):
            if self._stop.is_set() or settings.shutdown:
                return
            if _port_open("127.0.0.1", settings.webhook_port):
                break
            time.sleep(1.0)
        else:
            log.warning("Tunnel: webhook never bound :%d in time — starting tunnel anyway",
                        settings.webhook_port)

        while not self._stop.is_set() and not settings.shutdown:
            self._kill_stale_ngrok()
            cmd = self._build_command()
            log.info("Tunnel: starting ngrok %s", " ".join(cmd[1:]))
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
            except Exception as e:
                log.error("Tunnel: failed to spawn ngrok: %s — retrying in 5s", e)
                time.sleep(5.0)
                continue

            self._last_started_at = time.time()
            self._restart_count += 1
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                log.info("[ngrok] %s", line)
                # ngrok logfmt prints url=https://… on tunnel start.
                if "url=https://" in line:
                    pos = line.find("url=https://")
                    rest = line[pos + len("url="):].split()[0]
                    self._last_public_url = rest

            rc = self._proc.wait()
            self._proc = None
            log.warning("Tunnel: ngrok exited (code=%s) — respawning in 3s", rc)
            for _ in range(30):
                if self._stop.is_set() or settings.shutdown:
                    return
                time.sleep(0.1)

    def _build_command(self) -> List[str]:
        cmd: List[str] = [
            "ngrok", "http", str(settings.webhook_port),
            "--log", "stdout", "--log-format", "logfmt",
        ]
        if settings.ngrok_domain:
            cmd.extend(["--url", settings.ngrok_domain])
        # Run the inspector on a non-default port so we never collide with
        # a leftover g1-brain ngrok bound to 4040.
        cmd.extend(["--inspect-db-size", "0"])
        return cmd

    @staticmethod
    def _kill_stale_ngrok() -> None:
        """Best-effort cleanup of any ngrok process we don't own. Free-tier
        only allows one agent per account, so a stray ngrok = no tunnel."""
        if shutil.which("pkill"):
            subprocess.run(["pkill", "-x", "ngrok"], check=False)
            time.sleep(0.5)


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False
