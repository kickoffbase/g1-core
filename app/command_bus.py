"""
Command Bus
===========
Tiny in-process message bus for "do this on the robot" requests coming from
any producer (currently only the webhook, tomorrow MQTT/WebSocket/CLI).

Why an explicit bus instead of just calling speaker.say() from the request
handler:
  - Guarantees serial execution (only one utterance at a time → no audio
    overlap, no DDS contention).
  - Lets producers fire-and-forget and poll for status (`GET /commands/<id>`).
  - Idempotency: a producer that retries with the same `id` gets the cached
    result instead of speaking the line twice.
  - Decouples the HTTP layer from the robot — if the robot is mid-reconnect
    we just queue the command and play it once it recovers.

Status lifecycle:
    QUEUED → RUNNING → DONE
                    ↘ FAILED
                    ↘ DROPPED      (queue full)
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


# Type names live as plain strings so it's trivial to add new kinds without
# importing an enum at every callsite.
KIND_SAY = "SAY"
KIND_GREET = "GREET"
KIND_OUTRO = "OUTRO"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_DROPPED = "dropped"


@dataclass
class Command:
    id: str
    kind: str
    payload: Dict[str, Any] = field(default_factory=dict)
    source: str = ""
    status: str = STATUS_QUEUED
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CommandBus:
    """Single producer-many consumers? No — single consumer, many producers."""

    def __init__(self, max_queue: int = 32, history_size: int = 200):
        self._queue: "queue.Queue[Command]" = queue.Queue(maxsize=max_queue)
        # OrderedDict acts as both an index (for status lookups + dedup) and
        # an LRU bound (we trim to `history_size`). Cheap and lock-protected.
        self._history: "OrderedDict[str, Command]" = OrderedDict()
        self._history_size = history_size
        self._lock = threading.Lock()

    # ── Producer side ──────────────────────────────────────────────────

    def submit(
        self,
        kind: str,
        payload: Optional[Dict[str, Any]] = None,
        source: str = "",
        command_id: Optional[str] = None,
    ) -> Command:
        """Queue a command. If `command_id` was seen before, returns the
        existing record without re-queueing (idempotent retries)."""
        cid = (command_id or "").strip() or _new_id()
        with self._lock:
            existing = self._history.get(cid)
            if existing is not None:
                return existing
            cmd = Command(id=cid, kind=kind, payload=payload or {}, source=source)
            self._remember(cmd)
        try:
            self._queue.put_nowait(cmd)
        except queue.Full:
            with self._lock:
                cmd.status = STATUS_DROPPED
                cmd.error = "queue full"
                cmd.finished_at = time.time()
            log.warning("CommandBus: dropped %s (%s) — queue full", cmd.kind, cmd.id)
        return cmd

    def get(self, cid: str) -> Optional[Command]:
        with self._lock:
            return self._history.get(cid)

    def recent(self, limit: int = 50) -> list:
        with self._lock:
            items = list(self._history.values())
        return [c.to_dict() for c in items[-limit:]]

    def pending(self) -> int:
        return self._queue.qsize()

    # ── Consumer side ──────────────────────────────────────────────────

    def take(self, timeout: float = 1.0) -> Optional[Command]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def mark_running(self, cmd: Command) -> None:
        with self._lock:
            cmd.status = STATUS_RUNNING
            cmd.started_at = time.time()

    def mark_done(self, cmd: Command, result: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            cmd.status = STATUS_DONE
            cmd.result = result or {}
            cmd.finished_at = time.time()

    def mark_failed(self, cmd: Command, error: str) -> None:
        with self._lock:
            cmd.status = STATUS_FAILED
            cmd.error = error
            cmd.finished_at = time.time()

    # ── Internals ──────────────────────────────────────────────────────

    def _remember(self, cmd: Command) -> None:
        self._history[cmd.id] = cmd
        while len(self._history) > self._history_size:
            self._history.popitem(last=False)


def _new_id() -> str:
    return uuid.uuid4().hex


# A single bus instance is fine — the whole service is one process and one
# robot. Tests instantiate their own CommandBus directly.
bus = CommandBus()
