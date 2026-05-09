"""
System Audio Sink
=================
Pipes raw PCM (s16le mono 16 kHz — what ElevenLabs gives us) to whatever
the host is configured to play through. With PulseAudio/PipeWire, the
default sink is whatever's currently routed in `pavucontrol` / `pactl`,
so a Bluetooth speaker "just works" once paired and selected as default.

The wrapper around the subprocess is deliberately self-healing:
  - First write spawns the player.
  - BrokenPipe → close, swap to fallback command, respawn, retry the write.
  - close()/abort() never raise.

Helper script `scripts/bt-connect.sh` handles the BT pairing / setting the
sink to default — call it from the watchdog or manually.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from typing import Iterable, List, Optional

log = logging.getLogger(__name__)


class SystemAudioPlayer:
    def __init__(self, command: str, fallback_command: str = ""):
        self._commands = self._resolve_commands(command, fallback_command)
        self._command_index = 0
        self._proc: Optional[subprocess.Popen] = None

    @staticmethod
    def _resolve_commands(command: str, fallback_command: str) -> List[str]:
        commands: List[str] = []
        for cmd in (command, fallback_command):
            cmd = (cmd or "").strip()
            if not cmd or cmd in commands:
                continue
            # Under systemd we typically have no PulseAudio user session, so
            # `paplay` will fail with "Connection refused". Skip it unless the
            # operator wired up PULSE_SERVER explicitly.
            if cmd.startswith("paplay ") and not os.environ.get("PULSE_SERVER"):
                log.warning("Skipping paplay (PULSE_SERVER unset); will try fallback")
                continue
            commands.append(cmd)
        if not commands:
            commands.append("aplay -q -t raw -f S16_LE -r 16000 -c 1")
        return commands

    def __enter__(self) -> "SystemAudioPlayer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        if self._proc is not None:
            return
        cmd = self._current_command()
        args = shlex.split(cmd)
        if not args:
            raise RuntimeError("system audio command is empty")
        log.debug("Spawning system audio player: %s", cmd)
        self._proc = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, pcm: bytes) -> bool:
        if not pcm:
            return True
        # At most len(commands) attempts: one per fallback command.
        for _ in range(len(self._commands) or 1):
            if self._proc is None:
                self.start()
            if self._proc is None or self._proc.stdin is None:
                return False
            try:
                self._proc.stdin.write(pcm)
                return True
            except BrokenPipeError:
                if not self._rotate_command():
                    return False
        return False

    def write_many(self, chunks: Iterable[bytes]) -> int:
        written = 0
        for pcm in chunks:
            if not self.write(pcm):
                break
            written += len(pcm)
        return written

    def close(self) -> None:
        proc = self._take_proc()
        if proc is None:
            return
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.terminate()

    def abort(self) -> None:
        proc = self._take_proc()
        if proc is None:
            return
        proc.terminate()

    # ── Internals ──────────────────────────────────────────────────────

    def _current_command(self) -> str:
        if not self._commands:
            return ""
        return self._commands[min(self._command_index, len(self._commands) - 1)]

    def _take_proc(self) -> Optional[subprocess.Popen]:
        proc = self._proc
        self._proc = None
        if proc and proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass
        return proc

    def _rotate_command(self) -> bool:
        proc = self._take_proc()
        stderr = self._read_stderr(proc) if proc else ""
        if self._command_index + 1 >= len(self._commands):
            log.error("System audio command failed: %s%s",
                      self._current_command(), f" ({stderr})" if stderr else "")
            return False
        log.warning("System audio falling back to next command (%s)", stderr or "broken pipe")
        self._command_index += 1
        self.start()
        return True

    @staticmethod
    def _read_stderr(proc: Optional[subprocess.Popen]) -> str:
        if not proc or not proc.stderr:
            return ""
        try:
            return (proc.stderr.read() or b"").decode(errors="replace").strip()
        except Exception:
            return ""
