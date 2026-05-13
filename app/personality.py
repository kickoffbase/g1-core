"""
Personalities
=============
Each personality is a JSON file in `personalities/<slug>.json`.

Schema (only `slug` is required):
    {
      "slug":         "comedian",
      "display_name": "Bart the Comedian",
      "description":  "...",
      "intro_line":   "Optional opening line.",
      "outro_line":   "Optional closing line.",
      "voice": {
        "voice_id":   "elevenlabs voice id",
        "model":      "eleven_flash_v2_5",
        "stability":  0.0–1.0,
        "similarity": 0.0–1.0,
        "style":      0.0–1.0,
        "speed":      0.7–1.2
      }
    }

Active personality is persisted to `state/active_personality` so a runtime
switch survives a service restart.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import REPO_ROOT, settings

log = logging.getLogger(__name__)

_DIR = REPO_ROOT / "personalities"
_STATE_FILE = REPO_ROOT / "state" / "active_personality"
_lock = threading.RLock()


@dataclass
class VoiceOverrides:
    voice_id: str = ""
    model: str = ""
    stability: Optional[float] = None
    similarity: Optional[float] = None
    style: Optional[float] = None
    speed: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "VoiceOverrides":
        if not data:
            return cls()
        return cls(
            voice_id=str(data.get("voice_id") or ""),
            model=str(data.get("model") or ""),
            stability=_opt_float(data.get("stability")),
            similarity=_opt_float(data.get("similarity")),
            style=_opt_float(data.get("style")),
            speed=_opt_float(data.get("speed")),
        )


@dataclass
class GestureOverrides:
    """Per-personality gesture preferences. None = inherit settings.* defaults."""
    enabled: Optional[bool] = None
    intensity: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "GestureOverrides":
        if not data:
            return cls()
        en = data.get("enabled")
        intensity = data.get("intensity")
        return cls(
            enabled=bool(en) if isinstance(en, bool) else None,
            intensity=str(intensity).strip().lower() if isinstance(intensity, str) and intensity.strip() else None,
        )


@dataclass
class Personality:
    slug: str
    display_name: str = ""
    description: str = ""
    intro_line: str = ""
    outro_line: str = ""
    voice: VoiceOverrides = field(default_factory=VoiceOverrides)
    gestures: GestureOverrides = field(default_factory=GestureOverrides)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Personality":
        return cls(
            slug=str(data.get("slug") or "").strip(),
            display_name=str(data.get("display_name") or "").strip(),
            description=str(data.get("description") or "").strip(),
            intro_line=str(data.get("intro_line") or "").strip(),
            outro_line=str(data.get("outro_line") or "").strip(),
            voice=VoiceOverrides.from_dict(data.get("voice")),
            gestures=GestureOverrides.from_dict(data.get("gestures")),
        )


_BUILTIN_FALLBACK = Personality(
    slug="comedian",
    display_name="Bart",
    description="Built-in fallback personality.",
    intro_line="Mic check, humans.",
    outro_line="Powering down.",
)


_active: Optional[Personality] = None
_change_listeners: List["callable[[Personality], None]"] = []


def list_available() -> List[str]:
    """Sorted slugs of personalities present on disk."""
    if not _DIR.is_dir():
        return [_BUILTIN_FALLBACK.slug]
    found = {p.stem for p in _DIR.glob("*.json")}
    found.add(_BUILTIN_FALLBACK.slug)
    return sorted(found)


def get() -> Personality:
    with _lock:
        if _active is None:
            return set_active(_load_persisted_slug() or settings.personality, persist=False)
        return _active


def set_active(slug: str, persist: bool = True) -> Personality:
    """Switch to <slug>. Falls back to built-in if file is broken/missing."""
    global _active
    with _lock:
        persona = _load(slug)
        _active = persona
        if persist:
            _persist_slug(persona.slug)
        log.info("Personality active: %s (%s)", persona.display_name or persona.slug, persona.slug)
    for cb in list(_change_listeners):
        try:
            cb(persona)
        except Exception as e:
            log.warning("Personality change listener failed: %s", e)
    return persona


def on_change(callback) -> None:
    """Register a callback fired after every successful set_active()."""
    _change_listeners.append(callback)


def _load(slug: str) -> Personality:
    path = _DIR / f"{slug}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("slug"):
            data["slug"] = slug
        return Personality.from_dict(data)
    except FileNotFoundError:
        log.warning("Personality '%s' not found at %s — using fallback", slug, path)
    except Exception as e:
        log.error("Personality '%s' broken (%s) — using fallback", slug, e)
    return _BUILTIN_FALLBACK


def _persist_slug(slug: str) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(slug.strip() + "\n", encoding="utf-8")
    except Exception as e:
        log.warning("Could not persist active personality (%s): %s", slug, e)


def _load_persisted_slug() -> Optional[str]:
    try:
        if _STATE_FILE.is_file():
            slug = _STATE_FILE.read_text(encoding="utf-8").strip()
            if slug and (Path(_DIR / f"{slug}.json").is_file() or slug == _BUILTIN_FALLBACK.slug):
                return slug
    except Exception as e:
        log.warning("Could not read persisted personality: %s", e)
    return None


def _opt_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
