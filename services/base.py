"""
Service Protocol
================
The contract every plugin under `services/` must satisfy. Keep it tiny so
new services have nothing to argue with:

    name           — short identifier shown in /health and the journal.
    start()        — non-blocking. Spawn threads if you need them.
    stop()         — best-effort, must not raise.
    health()       — quick dict, must not block.
    http_router()  — optional FastAPI APIRouter; webhook auto-mounts it.

The `http_router()` hook is what makes "one feature = one file under
services/" work for HTTP features too: a new service exposes its own
endpoints without ever touching webhook.py.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # avoid hard fastapi import for non-HTTP services
    from fastapi import APIRouter


class Service:
    name: str = "service"

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def health(self) -> Dict[str, Any]:
        return {"ok": True}

    def http_router(self) -> Optional["APIRouter"]:
        """Return a FastAPI APIRouter to expose endpoints under the
        webhook server. None (default) = the service has no HTTP surface."""
        return None
