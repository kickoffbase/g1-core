"""
Personalities
=============
Each personality is a JSON file in `personalities/<slug>.json`.

When Supabase is configured, enabled rows in `robot_personalities` are loaded
first; local JSON files supplement any slug not in the DB (and serve as
fallback when Supabase is unreachable). On slug conflict, the DB row wins.

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
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import REPO_ROOT, settings

log = logging.getLogger(__name__)

_DIR = REPO_ROOT / "personalities"
_STATE_FILE = REPO_ROOT / "state" / "active_personality"
_FALLBACK_STATE_FILE = Path.home() / ".local" / "state" / "g1-core" / "active_personality"
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "description": self.description,
            "intro_line": self.intro_line,
            "outro_line": self.outro_line,
            "voice": {
                "voice_id": self.voice.voice_id,
                "model": self.voice.model,
                "stability": self.voice.stability,
                "similarity": self.voice.similarity,
                "style": self.voice.style,
                "speed": self.voice.speed,
            },
            "gestures": {
                "enabled": self.gestures.enabled,
                "intensity": self.gestures.intensity,
            },
        }


_BUILTIN_FALLBACK = Personality(
    slug="comedian",
    display_name="Bart",
    description="Built-in fallback personality.",
    intro_line="Mic check, humans.",
    outro_line="Powering down.",
)


_active: Optional[Personality] = None
_change_listeners: List["callable[[Personality], None]"] = []
_db_config_warning_logged = False


def _merge_slugs(db_rows: Optional[List[Dict[str, Any]]]) -> List[str]:
    """Union of enabled DB slugs and local JSON slugs. DB wins on slug conflict at load time."""
    disk = set(_list_disk_available())
    if db_rows is None:
        return sorted(disk)
    db = {str(row.get("slug") or "").strip() for row in db_rows if row.get("slug")}
    return sorted(db | disk)


def list_available() -> List[str]:
    """Sorted slugs from Supabase (when reachable) merged with local JSON files."""
    db_rows = _fetch_db_personalities(fields="slug")
    slugs = _merge_slugs(db_rows)
    if db_rows == [] and not slugs:
        log.warning("No enabled DB personalities for robot %s — using local fallback", settings.supabase_robot_id)
    return slugs or _list_disk_available()


def list_details() -> List[Dict[str, Any]]:
    """Personality metadata for control UIs. DB rows merged with disk-only JSON."""
    db_rows = _fetch_db_personalities()
    if db_rows is not None:
        by_slug: Dict[str, Dict[str, Any]] = {}
        for row in db_rows:
            try:
                persona = Personality.from_dict(row)
                by_slug[persona.slug] = persona.to_dict()
            except Exception as e:
                log.warning("Skipping broken DB personality row %s: %s", row.get("slug"), e)
        for slug in _list_disk_available():
            if slug not in by_slug:
                by_slug[slug] = _load_disk(slug).to_dict()
        if by_slug:
            return [by_slug[s] for s in sorted(by_slug)]
        if db_rows == []:
            log.warning("No enabled DB personality details for robot %s — using local fallback", settings.supabase_robot_id)
    return [_load_disk(slug).to_dict() for slug in _list_disk_available()]


def _list_disk_available() -> List[str]:
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
    db_persona = _load_db(slug)
    if db_persona is not None:
        return db_persona
    return _load_disk(slug)


def _load_disk(slug: str) -> Personality:
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
    for path in (_STATE_FILE, _FALLBACK_STATE_FILE):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(slug.strip() + "\n", encoding="utf-8")
            if path != _STATE_FILE:
                log.warning("Persisted active personality to fallback state file: %s", path)
            return
        except Exception as e:
            log.warning("Could not persist active personality to %s (%s): %s", path, slug, e)


def _load_persisted_slug() -> Optional[str]:
    for path in (_STATE_FILE, _FALLBACK_STATE_FILE):
        try:
            if path.is_file():
                slug = path.read_text(encoding="utf-8").strip()
                if slug:
                    return slug
        except Exception as e:
            log.warning("Could not read persisted personality from %s: %s", path, e)
    return None


def _load_db(slug: str) -> Optional[Personality]:
    rows = _fetch_db_personalities(slug=slug)
    if not rows:
        if rows == []:
            log.warning("Personality '%s' not found in DB — using local fallback", slug)
        return None
    try:
        return Personality.from_dict(rows[0])
    except Exception as e:
        log.error("Personality '%s' DB row broken (%s) — using local fallback", slug, e)
        return None


def _fetch_db_personalities(
    slug: Optional[str] = None,
    fields: str = "slug,display_name,description,intro_line,outro_line,voice,gestures",
) -> Optional[List[Dict[str, Any]]]:
    global _db_config_warning_logged

    base = settings.supabase_url.rstrip("/")
    key = settings.supabase_service_role_key
    robot_id = settings.supabase_robot_id.strip()
    if not base or not key or not robot_id:
        if not _db_config_warning_logged:
            log.warning("Supabase personality config incomplete — using local JSON fallback")
            _db_config_warning_logged = True
        return None

    params = {
        "select": fields,
        "robot_id": f"eq.{robot_id}",
        "is_enabled": "eq.true",
        "order": "slug.asc",
    }
    if slug is not None:
        params["slug"] = f"eq.{slug.strip()}"
        params["limit"] = "1"
    url = f"{base}/rest/v1/robot_personalities?{urlencode(params)}"
    req = Request(
        url,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=settings.supabase_timeout_s) as res:
            raw = res.read().decode("utf-8")
        data = json.loads(raw or "[]")
        if not isinstance(data, list):
            raise ValueError("expected list response")
        return data
    except (OSError, URLError, json.JSONDecodeError, ValueError) as e:
        log.warning("Supabase personalities unavailable (%s) — using local JSON fallback", e)
        return None


def _opt_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
