"""
Gestures
========
Subtle, randomized arm motions during speech, plus an explicit one-shot
API. Single source of truth for everything that ever moves G1's arms
through the high-level Arm Action service.

Design constraints
------------------
* **SDK presets only** — `SAFE_ACTIONS` is the closed set of action ids
  we ever issue, and it mirrors Unitree's built-in SDK action_map.
  Anything outside that map is still rejected; firmware-only discoveries
  from `GetActionList()` are shown to operators but not executed by slug.
* **No low-level joint streaming.** Everything goes through
  `G1ArmActionClient.ExecuteAction` (api id 7106). Worst case: the
  firmware responds with a non-zero code and we treat the gesture as
  failed. We never publish raw motor commands.
* **Multi-layered gating** before any send:
    1. master switch (`settings.gestures_enabled`)
    2. personality switch (`personality.gestures.enabled`)
    3. robot connected
    4. arm client available (older firmware ships without the service)
    5. music not playing (audio-only quiet content stays quiet)
    6. movement lockout (no gestures within N seconds of a `Move`)
    7. rate limit (`gestures_min_gap_s_floor` + intensity profile gap)
    8. per-utterance cap (the Conductor enforces this for in-speech)
* **Auto-release.** After every gesture sequence we send `release_arm`
  (id 99) so the robot returns to a neutral posture. Failure to release
  is logged but never raised.
* **Graceful degrade.** Any failure path returns a structured
  `{ok: False, skipped: <reason>}` instead of raising, so `say()` stays
  bullet-proof and the webhook keeps responding.

The Conductor
-------------
`Conductor` is the per-utterance scheduler used by `app/speaker.py`:
given an estimated utterance duration it picks 0..N random gesture
times within `[min_gap, max_gap]`, fires them on a daemon timer, and
guarantees an `release_arm` in `stop()`. Stops the moment speech ends.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from app import personality
from app.config import settings
from app.robot import robot

log = logging.getLogger(__name__)


# ── SDK Arm Actions ────────────────────────────────────────────────────
# Complete preset map from unitree_sdk2py.g1.arm.g1_arm_action_client
# `action_map`. Manual `/gesture` calls may use any of these IDs/slugs;
# automatic in-speech gestures still use the conservative profile pools below.
SDK_ACTIONS: Dict[str, int] = {
    "two_hand_kiss": 11,
    "left_kiss": 12,
    "right_kiss": 13,
    "hands_up": 15,
    "clap": 17,
    "high_five": 18,
    "hug": 19,
    "heart": 20,
    "right_heart": 21,
    "reject": 22,
    "right_hand_up": 23,
    "x_ray": 24,
    "face_wave": 25,
    "high_wave": 26,
    "shake_hand": 27,
    "release_arm": 99,
}

ACTION_DESCRIPTIONS: Dict[str, str] = {
    "two_hand_kiss": "Two-hand air kiss",
    "left_kiss": "Left-hand air kiss",
    "right_kiss": "Right-hand air kiss",
    "hands_up": "Both hands up",
    "clap": "Clap",
    "high_five": "High five",
    "hug": "Hug / arms forward",
    "heart": "Two-hand heart",
    "right_heart": "Right-hand heart",
    "reject": "Crossed-arms reject",
    "right_hand_up": "Right hand up",
    "x_ray": "X-ray / Ultraman pose",
    "face_wave": "Wave near face",
    "high_wave": "High wave",
    "shake_hand": "Handshake",
    "release_arm": "Release arms to neutral",
}

# Backwards-compatible name used by the service layer.
SAFE_ACTIONS = SDK_ACTIONS
_ACTION_ORDER = list(SDK_ACTIONS.keys())

# Reverse lookup for `/gesture` when callers pass an integer id.
_ID_TO_NAME: Dict[int, str] = {v: k for k, v in SAFE_ACTIONS.items()}

RELEASE_ARM_ID = 99
AUTO_RELEASE_DELAY_S = 3.0


@dataclass
class IntensityProfile:
    name: str
    pool: List[str]
    min_gap_s: float
    max_gap_s: float
    max_per_utterance: int


# Subtle is the default. Keep `release_arm` out of the random pool —
# it's an internal cleanup, not a "gesture". `max_per_utterance` is the
# upper bound; the actual number is also capped by `_dynamic_cap()` based
# on text length so a 4-second hello never gets a 3-gesture salvo.
INTENSITY_PROFILES: Dict[str, IntensityProfile] = {
    "subtle": IntensityProfile(
        name="subtle",
        pool=["face_wave", "right_hand_up"],
        min_gap_s=8.0,
        max_gap_s=14.0,
        max_per_utterance=2,
    ),
    "balanced": IntensityProfile(
        name="balanced",
        pool=["face_wave", "right_hand_up"],
        min_gap_s=6.5,
        max_gap_s=11.0,
        max_per_utterance=3,
    ),
    "expressive": IntensityProfile(
        name="expressive",
        pool=["face_wave", "right_hand_up"],
        min_gap_s=6.0,
        max_gap_s=9.0,
        max_per_utterance=4,
    ),
}

# Approximate ElevenLabs flash output rate (chars/s) — keep in sync with
# `_AVG_CHARS_PER_SEC` in `app/speaker.py`. Used by `preview_schedule()`
# so the UI can show the same prediction the conductor would make.
_AVG_CHARS_PER_SEC = 14.0
# Tail window — see `Conductor.start()` for the rationale.
_TAIL_S = 2.5


def resolve_profile() -> IntensityProfile:
    """Personality > settings default > built-in subtle. The personality
    file may say `intensity: "expressive"`, but we still respect the
    global gap floor — see `_gap_bounds()`."""
    raw = settings.gestures_default_intensity
    try:
        per = personality.get()
        if per.gestures and per.gestures.intensity:
            raw = per.gestures.intensity
    except Exception:
        pass
    profile = INTENSITY_PROFILES.get((raw or "").strip().lower())
    if profile is None:
        profile = INTENSITY_PROFILES["subtle"]
    return profile


def is_enabled() -> bool:
    """Both the master switch AND the personality must agree."""
    if not settings.gestures_enabled:
        return False
    try:
        per = personality.get()
        if per.gestures and per.gestures.enabled is False:
            return False
    except Exception:
        pass
    return True


# Runtime toggle for *in-speech* gestures only. Manual `/gesture` POSTs and
# the explicit greeting opener still work even when this is off — that's the
# whole point: an operator can mute talking-hands during a quiet moment
# without losing the manual buttons. State is in-memory (resets on reboot to
# `gestures_enabled` so the .env is the durable source of truth).
_speech_runtime_enabled: bool = True


def is_speech_enabled() -> bool:
    """Whether the conductor should fire gestures during the next utterance.

    The master kill-switch (`is_enabled()`) wins — if the personality or
    the .env says no, the runtime toggle is irrelevant."""
    if not is_enabled():
        return False
    return _speech_runtime_enabled


def set_speech_enabled(enabled: bool) -> bool:
    """Flip the in-speech toggle. Returns the resolved value (after
    coercion)."""
    global _speech_runtime_enabled
    _speech_runtime_enabled = bool(enabled)
    log.info("Gesture: speech_enabled=%s", _speech_runtime_enabled)
    return _speech_runtime_enabled


def _dynamic_cap(eta_s: float, profile: IntensityProfile) -> int:
    """Cap gestures-per-utterance based on how long speech will run.

    The personality's `max_per_utterance` is the ceiling; this function
    only ever returns a *lower* number. The bands are rough but match
    how a human talks with their hands:
        <  4s : silent              (no gesture — too short to land)
        4-10s: 1 gesture            ("hi, how are you?")
        10-25s: up to 2 gestures    (a couple of sentences)
        25s+ : up to 4 gestures     (paragraph-length lines)
    """
    if eta_s < settings.gestures_min_utterance_s:
        return 0
    cap = profile.max_per_utterance
    if eta_s < 10.0:
        return min(1, cap)
    if eta_s < 25.0:
        return min(2, cap)
    if eta_s < 45.0:
        return min(3, cap)
    return cap


def _gap_bounds(profile: IntensityProfile) -> Tuple[float, float]:
    """Apply the global floor on top of profile bounds."""
    floor = max(0.0, float(settings.gestures_min_gap_s_floor))
    lo = max(profile.min_gap_s, floor)
    hi = max(profile.max_gap_s, lo + 0.5)
    return lo, hi


# ── State (single rail for /gesture + Conductor) ──────────────────────


_lock = threading.Lock()
_last_at: float = 0.0
_in_flight: bool = False
_last_skip_reason: Optional[str] = None
_last_action: Optional[str] = None
_release_timer: Optional[threading.Timer] = None


def _resolve_action(name_or_id: Union[str, int]) -> Tuple[Optional[str], Optional[int]]:
    """Map either 'face_wave' / 25 → ('face_wave', 25) or (None, None) if
    the input is not in the whitelist."""
    if isinstance(name_or_id, int):
        if name_or_id in _ID_TO_NAME:
            return _ID_TO_NAME[name_or_id], name_or_id
        return None, None
    if isinstance(name_or_id, str):
        key = name_or_id.strip().lower().replace("-", "_").replace(" ", "_")
        if key in SAFE_ACTIONS:
            return key, SAFE_ACTIONS[key]
    return None, None


def _music_playing() -> bool:
    """Best-effort music gate. We don't import MusicService directly to
    avoid a circular dep; instead we look it up via __main__ at call
    time (same trick webhook.py uses for the registry)."""
    try:
        import sys
        main_mod = sys.modules.get("__main__") or sys.modules.get("main")
        registry = getattr(main_mod, "service_registry", None) if main_mod else None
        if registry is None:
            return False
        for svc in registry.services():
            if getattr(svc, "name", "") == "music":
                health = svc.health() or {}
                return bool(health.get("playing"))
    except Exception:
        return False
    return False


def _movement_locked_out() -> bool:
    if settings.gestures_post_move_lockout_s <= 0:
        return False
    return (time.time() - robot.last_move_at) < settings.gestures_post_move_lockout_s


def _rate_limited(profile: IntensityProfile) -> bool:
    floor = float(settings.gestures_min_gap_s_floor)
    gap = max(profile.min_gap_s, floor)
    return (time.time() - _last_at) < gap


def _set_skip(reason: str) -> Dict[str, Any]:
    global _last_skip_reason
    _last_skip_reason = reason
    return {"ok": False, "skipped": reason}


def execute(
    name_or_id: Union[str, int],
    *,
    source: str = "api",
    bypass_rate_limit: bool = False,
) -> Dict[str, Any]:
    """Send one whitelisted gesture if every safety gate passes.

    Returns `{ok: True, action, id, code}` on success or
            `{ok: False, skipped: <reason>}` otherwise.
    Never raises. `source` is only used for logs."""
    global _last_at, _in_flight, _last_action

    name, action_id = _resolve_action(name_or_id)
    if name is None or action_id is None:
        return _set_skip("not_in_whitelist")

    profile = resolve_profile()

    with _lock:
        if not is_enabled():
            return _set_skip("disabled")
        if not robot.connected:
            return _set_skip("robot_disconnected")
        if not robot.arm_available:
            return _set_skip("arm_unavailable")
        if _in_flight:
            return _set_skip("gesture_in_flight")
        # release_arm bypasses rate limit & music/movement gates — it's a
        # safety op, not a "talking" gesture.
        if action_id != RELEASE_ARM_ID:
            if _music_playing():
                return _set_skip("music_playing")
            if _movement_locked_out():
                return _set_skip("post_move_lockout")
            if not bypass_rate_limit and _rate_limited(profile):
                return _set_skip("rate_limited")
        _in_flight = True

    log.info("Gesture: %s (%s) source=%s", name, action_id, source)
    try:
        sent, code = robot.arm_execute(action_id)
    except Exception as e:
        with _lock:
            _in_flight = False
        log.exception("Gesture: arm_execute raised")
        return _set_skip(f"sdk_exception: {type(e).__name__}")

    with _lock:
        _in_flight = False
        if sent and action_id != RELEASE_ARM_ID:
            _last_at = time.time()
            _last_action = name
        elif sent:
            _last_action = name

    if not sent:
        return _set_skip("sdk_send_failed")

    if isinstance(code, int) and code != 0:
        # Firmware-level rejection — most often FSM mismatch (the
        # operator left the robot in damp / sit). Don't keep retrying.
        log.warning("Gesture: firmware rejected %s with code=%s", name, code)
        return {"ok": False, "skipped": "firmware_rejected", "action": name, "id": action_id, "code": code}

    return {"ok": True, "action": name, "id": action_id, "code": code if isinstance(code, int) else None}


def release() -> Dict[str, Any]:
    """Best-effort return to neutral. Always safe to call."""
    return execute("release_arm", source="release")


def schedule_release(delay_s: float = AUTO_RELEASE_DELAY_S) -> None:
    """Return arms to neutral after a short hold.

    Manual `/gesture` calls should feel like a single button: run the
    gesture, hold the pose briefly, then release. We keep this server-side
    so Robohire doesn't need to coordinate timers over ngrok.
    """
    global _release_timer
    if delay_s <= 0:
        release()
        return
    with _lock:
        if _release_timer is not None:
            try:
                _release_timer.cancel()
            except Exception:
                pass
        timer = threading.Timer(delay_s, release)
        timer.daemon = True
        timer.name = "gesture-auto-release"
        _release_timer = timer
        timer.start()


def list_safe_actions() -> Dict[str, int]:
    return dict(SAFE_ACTIONS)


def _jsonish(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [_jsonish(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonish(v) for k, v in value.items()}
    return repr(value)


def _extract_action_ids(value: Any) -> List[int]:
    found: set[int] = set()

    def walk(item: Any) -> None:
        if isinstance(item, bool) or item is None:
            return
        if isinstance(item, int):
            if 0 <= item <= 999:
                found.add(item)
            return
        if isinstance(item, str):
            for match in re.findall(r"\b\d{1,3}\b", item):
                found.add(int(match))
            return
        if isinstance(item, dict):
            for key, child in item.items():
                walk(key)
                walk(child)
            return
        if isinstance(item, (list, tuple, set)):
            for child in item:
                walk(child)

    walk(value)
    return sorted(found)


def action_catalog() -> Dict[str, Any]:
    """SDK catalog plus best-effort firmware availability from GetActionList."""
    sent, code, raw = robot.arm_action_list()
    firmware_ids = _extract_action_ids(raw) if sent and raw is not None else []
    firmware_set = set(firmware_ids)

    actions: List[Dict[str, Any]] = []
    for name in _ACTION_ORDER:
        action_id = SAFE_ACTIONS[name]
        actions.append({
            "name": name,
            "id": action_id,
            "description": ACTION_DESCRIPTIONS.get(name, name.replace("_", " ")),
            "source": "sdk",
            "available": action_id in firmware_set if firmware_ids else None,
        })

    known_ids = set(SAFE_ACTIONS.values())
    for action_id in firmware_ids:
        if action_id in known_ids:
            continue
        actions.append({
            "name": f"firmware_action_{action_id}",
            "id": action_id,
            "description": "Advertised by robot firmware, not present in SDK action_map",
            "source": "firmware",
            "available": True,
        })

    return {
        "actions": actions,
        "firmware": {
            "ok": bool(sent),
            "code": code,
            "action_ids": firmware_ids,
            "raw": _jsonish(raw),
        },
    }


def status_snapshot() -> Dict[str, Any]:
    profile = resolve_profile()
    lo, hi = _gap_bounds(profile)
    return {
        "enabled": is_enabled(),
        "speech_enabled": is_speech_enabled(),
        "arm_available": robot.arm_available,
        "intensity": profile.name,
        "pool": list(profile.pool),
        "min_gap_s": lo,
        "max_gap_s": hi,
        "max_per_utterance": profile.max_per_utterance,
        "min_utterance_s": settings.gestures_min_utterance_s,
        "avg_chars_per_sec": _AVG_CHARS_PER_SEC,
        "last_at": _last_at,
        "last_action": _last_action,
        "last_skip_reason": _last_skip_reason,
        "in_flight": _in_flight,
    }


def preview_schedule(text_chars: int) -> Dict[str, Any]:
    """Predict how many gestures the conductor would schedule for a given
    text length. Pure function — never touches the SDK or state.

    Used by the operator UI to show "your text → ~Xs → Y gestures" before
    the operator hits Speak. Mirrors the math in `Conductor.start()`."""
    chars = max(0, int(text_chars))
    eta_s = chars / _AVG_CHARS_PER_SEC
    profile = resolve_profile()
    lo, hi = _gap_bounds(profile)
    cap = _dynamic_cap(eta_s, profile)

    expected = 0
    skipped: Optional[str] = None
    if not is_enabled():
        skipped = "disabled"
    elif not _speech_runtime_enabled:
        skipped = "speech_muted"
    elif eta_s < settings.gestures_min_utterance_s:
        skipped = "utterance_too_short"
    else:
        usable = max(0.0, eta_s - _TAIL_S)
        if usable <= lo:
            skipped = "utterance_too_short"
        else:
            avg_gap = (lo + hi) / 2.0
            # Mean number of slots that fit before `usable`, capped.
            est = int(((usable - lo) // avg_gap) + 1) if usable >= lo else 0
            expected = max(0, min(cap, est))

    return {
        "estimated_duration_s": round(eta_s, 1),
        "expected_gestures": expected,
        "cap": cap,
        "intensity": profile.name,
        "min_gap_s": lo,
        "max_gap_s": hi,
        "min_utterance_s": settings.gestures_min_utterance_s,
        "speech_enabled": is_speech_enabled(),
        "enabled": is_enabled(),
        "skipped": skipped,
    }


# ── Conductor (per-utterance scheduler) ────────────────────────────────


class Conductor:
    """Schedules a small number of random gestures inside one utterance.

    Lifecycle:
        c = Conductor()
        c.start(estimated_duration_s=8.0)      # schedules timers
        ... PCM streams to the speaker ...
        c.stop()                               # cancels + release_arm

    The conductor is single-shot: build a fresh one per utterance.
    Multiple conductors must not run in parallel — `say()` already
    holds a lock around the whole utterance, so this is enforced
    upstream."""

    def __init__(self) -> None:
        self._timers: List[threading.Timer] = []
        self._stopped = threading.Event()
        self._fired = 0
        self._max = 0

    def start(self, estimated_duration_s: float) -> Dict[str, Any]:
        if not is_enabled():
            return {"scheduled": 0, "skipped": "disabled"}
        if not _speech_runtime_enabled:
            return {"scheduled": 0, "skipped": "speech_muted"}
        if not robot.connected or not robot.arm_available:
            return {"scheduled": 0, "skipped": "no_arm"}
        if estimated_duration_s < settings.gestures_min_utterance_s:
            return {"scheduled": 0, "skipped": "utterance_too_short"}

        profile = resolve_profile()
        cap = _dynamic_cap(estimated_duration_s, profile)
        if cap <= 0:
            return {"scheduled": 0, "skipped": "utterance_too_short"}

        lo, hi = _gap_bounds(profile)
        # Reserve a tail window so we don't fire a gesture that finishes
        # *after* speech ends — looks awkward and defeats the lip-sync
        # illusion. Each Arm Action takes ~2-3s to play out.
        usable = max(0.0, estimated_duration_s - _TAIL_S)
        if usable <= lo:
            return {"scheduled": 0, "skipped": "utterance_too_short"}

        # Deterministic-ish schedule: lay timers at random points inside
        # [lo, usable], spaced by at least lo. The dynamic cap (above)
        # caps how many gestures fire in a single utterance.
        slots: List[float] = []
        cursor = random.uniform(lo, min(hi, usable))
        while cursor <= usable and len(slots) < cap:
            slots.append(cursor)
            cursor += random.uniform(lo, hi)

        self._max = len(slots)
        for t in slots:
            timer = threading.Timer(t, self._fire, args=(profile,))
            timer.daemon = True
            timer.name = "gesture-conductor"
            timer.start()
            self._timers.append(timer)

        return {"scheduled": len(slots), "intensity": profile.name, "cap": cap}

    def _fire(self, profile: IntensityProfile) -> None:
        if self._stopped.is_set():
            return
        if self._fired >= self._max:
            return
        try:
            choice = random.choice(profile.pool)
        except IndexError:
            return
        result = execute(choice, source="conductor")
        if result.get("ok"):
            self._fired += 1

    def stop(self) -> None:
        self._stopped.set()
        for t in self._timers:
            try:
                t.cancel()
            except Exception:
                pass
        self._timers.clear()
        # Always best-effort return arms to neutral after a sequence.
        try:
            if self._fired > 0:
                release()
        except Exception:
            log.debug("Gesture: release() in Conductor.stop raised", exc_info=True)


__all__ = [
    "SAFE_ACTIONS",
    "SDK_ACTIONS",
    "ACTION_DESCRIPTIONS",
    "RELEASE_ARM_ID",
    "INTENSITY_PROFILES",
    "Conductor",
    "execute",
    "release",
    "schedule_release",
    "is_enabled",
    "is_speech_enabled",
    "set_speech_enabled",
    "resolve_profile",
    "list_safe_actions",
    "action_catalog",
    "status_snapshot",
    "preview_schedule",
]
