"""
In-memory log ring buffer
=========================
Why this exists:

  `journalctl --user -u g1-core.service` is the "right" way to read our
  logs, but on many Ubuntu installs the **persistent user journal** is
  not enabled (`/var/log/journal/<id>/user-1000.journal` does not exist
  → `journalctl --user` returns "-- No entries --"). The records are
  fine, they live in the *system* journal under `_SYSTEMD_USER_UNIT=`,
  but `--user` does not read those.

  Rather than depend on a particular journald layout, we capture every
  record produced by the in-process logger into a thread-safe deque and
  expose it through `/logs`. This *always* works — even when journald is
  broken, even when running outside systemd, even when the operator
  hasn't installed the user-linger.

  We keep this independent of the rich console handler in `main.py`:
  RichHandler renders ANSI/colour for the terminal, RingHandler stores
  plain text for the API.
"""

from __future__ import annotations

import logging
from collections import deque
from threading import Lock
from typing import Deque, List, Optional


_DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"


class RingHandler(logging.Handler):
    """A `logging.Handler` that keeps the last N records in memory."""

    def __init__(self, capacity: int = 4000) -> None:
        super().__init__(level=logging.DEBUG)
        self._buf: Deque[str] = deque(maxlen=capacity)
        self._lock = Lock()
        self.setFormatter(logging.Formatter(_DEFAULT_FORMAT, datefmt=_DEFAULT_DATEFMT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = f"{record.levelname} {record.name} | <log format error>"
        with self._lock:
            self._buf.append(msg)

    def lines(self, limit: Optional[int] = None) -> List[str]:
        with self._lock:
            data = list(self._buf)
        if limit and limit > 0 and len(data) > limit:
            data = data[-limit:]
        return data

    def render(self, limit: Optional[int] = None) -> str:
        return "\n".join(self.lines(limit))

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


_handler: Optional[RingHandler] = None


def install(capacity: int = 4000) -> RingHandler:
    """Idempotently attach the ring buffer to the root logger."""
    global _handler
    if _handler is not None:
        return _handler
    _handler = RingHandler(capacity=capacity)
    root = logging.getLogger()
    root.addHandler(_handler)
    return _handler


def get() -> Optional[RingHandler]:
    return _handler
