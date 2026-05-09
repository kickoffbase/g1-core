#!/usr/bin/env python3
"""
Service entrypoint — Python instead of bash.

Why Python and not preflight.sh + run.sh
-----------------------------------------
Python reads source files in **universal-newline mode** (PEP 278) — it
treats LF / CRLF / CR identically. So even if the operator dumps the
whole repo from a Windows editor with CRLF endings, Python still loads
this file and runs. Bash does NOT do that: a single `\r` at the end of
a line turns `set -euo pipefail` into `set: pipefail: invalid option`
and the ExecStart fails with `status=2` before any cleanup can happen.

What this script does, in order:

  1. Sanitize line endings on every text file in the repo (CRLF → LF).
     This heals all the OTHER scripts (helper shell tools, .env, etc.)
     even if they arrived with Windows line endings.
  2. chmod +x on the few scripts that need it.
  3. Stop the predecessor `g1-brain` user/system service if it's
     squatting on the webhook port or the ngrok agent.
  4. Kill stale main.py / ngrok processes from a previous run.
  5. Free the webhook + ngrok inspector ports.
  6. exec into `python3 -m main` — replaces the current process so
     systemd's MainPID points at the actual service.

This file is the single ExecStart for `g1-core.service`. There is no
`ExecStartPre`, no `run.sh`, no `preflight.sh` in the critical path.
"""
from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Sequence

REPO = Path(__file__).resolve().parent.parent

# Only sweep file extensions the operator is likely to edit. Keep the
# universe small so a 'find' over node_modules-style trees can never
# slow boot.
TEXT_EXTS = {
    ".sh", ".service", ".py",
    ".json", ".md", ".toml",
    ".yml", ".yaml", ".env",
    ".cfg", ".conf", ".ini",
}
TEXT_NAMES = {".env", ".env.example", ".editorconfig", ".gitattributes"}
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", "state"}

EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


def log(msg: str) -> None:
    # Plain print: systemd captures stdout into the journal verbatim.
    print(f"[preflight] {msg}", flush=True)


# ── 1. CRLF sweep ──────────────────────────────────────────────────────


def _looks_textual(path: Path) -> bool:
    if path.name in TEXT_NAMES:
        return True
    return path.suffix.lower() in TEXT_EXTS


def _strip_crlf_inplace(path: Path) -> bool:
    """Return True iff the file actually contained CR characters."""
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if b"\r" not in raw:
        return False
    cleaned = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if cleaned == raw:
        return False
    try:
        path.write_bytes(cleaned)
    except OSError as e:
        log(f"WARN: could not rewrite {path}: {e}")
        return False
    return True


def sanitize_repo() -> int:
    fixed = 0
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            p = Path(root) / name
            if not _looks_textual(p):
                continue
            if _strip_crlf_inplace(p):
                fixed += 1
    return fixed


# ── 2. chmod +x ────────────────────────────────────────────────────────


def ensure_executable(paths: Iterable[Path]) -> None:
    for p in paths:
        if not p.exists():
            continue
        try:
            mode = p.stat().st_mode
            p.chmod(mode | EXEC_BITS)
        except OSError as e:
            log(f"WARN: chmod +x {p}: {e}")


# ── 3. Stop predecessor g1-brain ───────────────────────────────────────


def stop_g1_brain() -> None:
    """g1-brain holds the same ngrok account + neighbouring port. If left
    running it will silently break g1-core's tunnel."""
    quietly_run(["systemctl", "--user", "stop", "g1-brain.service"])
    quietly_run(["sudo", "-n", "systemctl", "stop", "g1-brain.service"])


# ── 4. Stale process cleanup ───────────────────────────────────────────


def kill_stale_processes() -> None:
    quietly_run(["pkill", "-9", "-f", r"python3?[^\n]*g1-core/main\.py"])
    quietly_run(["pkill", "-9", "-f", r"python3?[^\n]*g1-brain/main\.py"])
    quietly_run(["pkill", "-9", "-x", "ngrok"])


# ── 5. Port freeing ────────────────────────────────────────────────────


def read_webhook_port(default: int = 8766) -> int:
    env_path = REPO / ".env"
    if not env_path.exists():
        return default
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("WEBHOOK_PORT="):
                value = line.split("=", 1)[1].split("#", 1)[0].strip().strip('"').strip("'")
                if value.isdigit():
                    return int(value)
    except OSError:
        pass
    return default


def free_port(port: int) -> None:
    if shutil.which("fuser"):
        quietly_run(["fuser", "-k", f"{port}/tcp"])
        return
    if shutil.which("lsof"):
        try:
            r = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True, text=True, timeout=4,
            )
        except subprocess.SubprocessError:
            return
        for pid in (r.stdout or "").split():
            quietly_run(["kill", "-9", pid])


def wait_port_free(port: int, attempts: int = 20) -> None:
    for _ in range(attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return
            except OSError:
                time.sleep(0.25)


# ── 5b. Internet readiness ─────────────────────────────────────────────


# Hosts we ping to consider the network "ready". Order matters: the first
# host that succeeds wins. We need at least one of these reachable, or
# the service races against the network at boot — ElevenLabs rejects with
# `OSError: [Errno 113] No route to host`, ngrok handshake flaps, etc.
_NET_PROBES: tuple[tuple[str, int], ...] = (
    ("api.elevenlabs.io", 443),
    ("1.1.1.1", 443),
    ("8.8.8.8", 53),
)


def wait_for_internet(max_seconds: float = 30.0) -> bool:
    """Block up to `max_seconds` until at least one probe host accepts a
    TCP connection. Returns True on success, False on timeout (we still
    let the service start so a degraded boot doesn't loop forever)."""
    deadline = time.monotonic() + max_seconds
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        for host, port in _NET_PROBES:
            try:
                with socket.create_connection((host, port), timeout=2.0):
                    log(f"network ready (probe {host}:{port} ok, attempt {attempt})")
                    return True
            except OSError:
                continue
        time.sleep(1.0)
    log(f"WARN: network not ready after {max_seconds:.0f}s — starting anyway")
    return False


# ── 6. Helpers ─────────────────────────────────────────────────────────


def quietly_run(args: Sequence[str], timeout: float = 6.0) -> int:
    try:
        return subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout,
        ).returncode
    except FileNotFoundError:
        return 127
    except subprocess.SubprocessError:
        return 1


# ── Main ───────────────────────────────────────────────────────────────


def preflight() -> int:
    log(f"repo={REPO}")
    fixed = sanitize_repo()
    log(f"normalized line endings ({fixed} file(s) had CRLF)")
    ensure_executable([
        REPO / "systemd" / "preflight.py",
        REPO / "systemd" / "bootstrap.py",
        *(REPO / "systemd").glob("*.sh"),
        *(REPO / "scripts").glob("*.sh"),
    ])
    stop_g1_brain()
    kill_stale_processes()

    port = read_webhook_port()
    log(f"freeing ports {port}, 4040, 4041")
    for p in (port, 4040, 4041):
        free_port(p)
    wait_port_free(port)

    # Wait until the network actually has a route. Without this, the boot
    # greet, ngrok handshake, and the BT speaker's DNS query all race the
    # interface coming up, and every one of them logs a scary traceback
    # before things settle. 30s ceiling is enough for both Ethernet and
    # WiFi-with-DHCP on the Orin.
    wait_for_internet(max_seconds=30.0)

    if not shutil.which("ngrok"):
        log("WARN: 'ngrok' not in PATH — tunnel will be skipped")

    return port


def run_main() -> int:
    """Replace the current process with `python3 -m main`."""
    python = sys.executable or shutil.which("python3") or "/usr/bin/python3"
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    log(f"exec {python} -m main")
    os.chdir(REPO)
    os.execvpe(python, [python, "-m", "main"], env)
    return 1  # unreachable


def main() -> int:
    args = sys.argv[1:]
    preflight()
    if "--fix-only" in args or "--no-exec" in args:
        log("done (--fix-only)")
        return 0
    return run_main()


if __name__ == "__main__":
    sys.exit(main())
