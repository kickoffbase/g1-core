"""
Teach Mode — low-level arm record / replay
===========================================
The low-level counterpart to ``app/gestures.py``:

  * ``gestures.py`` → high-level ``G1ArmActionClient`` presets (face_wave,
    clap, …). RPC, firmware-owned, no joint streaming. Always safe.
  * ``teach.py``    → record a *custom* arm motion by physically moving the
    robot's arms, then replay it on demand. Streams joint targets over the
    ``rt/arm_sdk`` topic. Powerful, and therefore gated.

How it works
------------
Recording
    1. We take control of the arms via ``rt/arm_sdk`` with the blend
       *weight* ramped 0 → 1, ``kp = 0`` and a small ``kd`` (damping). The
       legs stay under the locomotion controller the whole time, so the
       robot keeps standing while the arms go limp-but-not-floppy.
    2. The operator moves the arms by hand. We sample ``rt/lowstate`` joint
       positions at ``TEACH_RECORD_HZ`` into a list of frames.
    3. On stop we ramp the weight 1 → 0 (hand the arms back) and persist the
       frames to ``recordings/<slug>.json``.

Replay
    1. Ramp weight 0 → 1 while smoothly interpolating from the *current*
       measured pose to the recording's first frame (``TEACH_APPROACH_S``) —
       this prevents a violent yank if the arms start somewhere else.
    2. Stream the recorded frames at their captured cadence with a
       conservative ``kp``/``kd``.
    3. Hold the final frame briefly, ramp weight 1 → 0, release.

Safety model
------------
* **Off by default.** ``TEACH_ENABLED=false`` makes every entry point a
  no-op that returns ``{ok: False, skipped: "disabled"}``.
* **Single operation.** A module lock guarantees only one of
  {record, play} runs at a time, and never concurrently with itself.
* **Arms only by default.** We touch joints 15-28 (both arms). The 3 waist
  joints (12-14) are opt-in via ``TEACH_INCLUDE_WAIST``. Legs are *never*
  touched — locomotion keeps them.
* **Weight ramps** on every entry/exit so control transfer is smooth.
* **Approach ramp** to frame 0 so replay never jumps.
* **Bounded.** ``TEACH_MAX_DURATION_S`` auto-stops a forgotten recording.
* **Emergency release.** ``stop()`` zeroes the weight and hands the arms
  straight back to the locomotion controller; always safe to call.
* **Graceful degrade.** Like gestures, nothing here raises into the caller;
  every path returns a structured dict.

This module imports ``unitree_sdk2py`` lazily (same as ``app/robot.py``) so
g1-core still boots on a dev box without the SDK — teach simply reports
``available: False`` there.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.config import REPO_ROOT, settings
from app.robot import robot

log = logging.getLogger(__name__)

_DIR = REPO_ROOT / "recordings"


# ── G1 joint layout (29-DoF, unitree_hg) ───────────────────────────────
# Index of the "weight" virtual joint used by rt/arm_sdk to blend our
# command with the locomotion controller (0 = loco owns arms, 1 = we do).
WEIGHT_JOINT = 29

# Waist (opt-in) and both arms (default capture set).
WAIST_JOINTS = (12, 13, 14)        # yaw, roll, pitch
ARM_JOINTS = tuple(range(15, 29))  # 15..28 — L shoulder/elbow/wrist, then R

# Human-readable names purely for the recording metadata / UI.
JOINT_NAMES: Dict[int, str] = {
    12: "waist_yaw",
    13: "waist_roll",
    14: "waist_pitch",
    15: "l_shoulder_pitch",
    16: "l_shoulder_roll",
    17: "l_shoulder_yaw",
    18: "l_elbow",
    19: "l_wrist_roll",
    20: "l_wrist_pitch",
    21: "l_wrist_yaw",
    22: "r_shoulder_pitch",
    23: "r_shoulder_roll",
    24: "r_shoulder_yaw",
    25: "r_elbow",
    26: "r_wrist_roll",
    27: "r_wrist_pitch",
    28: "r_wrist_yaw",
}

_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _slugify(name: str) -> str:
    s = _SLUG_RE.sub("_", (name or "").strip().lower()).strip("_")
    return s


def _joint_set() -> List[int]:
    joints = list(ARM_JOINTS)
    if settings.teach_include_waist:
        joints = list(WAIST_JOINTS) + joints
    return joints


# ── State ───────────────────────────────────────────────────────────────
class Mode(str):
    IDLE = "idle"
    RECORDING = "recording"
    PLAYING = "playing"


@dataclass
class _Session:
    name: str = ""
    started_at: float = 0.0
    frames: List[List[float]] = field(default_factory=list)
    joints: List[int] = field(default_factory=list)


class TeachEngine:
    """Owns the low-level arm channel and the record/replay state machine.

    A single instance (`teach`) is shared process-wide. All public methods
    are safe to call from any thread; heavy work (the record / replay loops)
    runs on dedicated daemon threads, but only one at a time."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._mode = Mode.IDLE
        self._pub = None              # ChannelPublisher(rt/arm_sdk)
        self._sub = None              # ChannelSubscriber(rt/lowstate)
        self._crc = None
        self._latest_state = None     # last LowState_ we saw
        self._state_lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._session = _Session()
        self._last_error: Optional[str] = None
        self._last_played: Optional[str] = None

    # ── Capability / status ─────────────────────────────────────────────

    def is_enabled(self) -> bool:
        return bool(settings.teach_enabled)

    @property
    def available(self) -> bool:
        """True when we *could* drive the arms: feature on, robot linked,
        and the low-level channel initialised at least once."""
        return self.is_enabled() and robot.connected and self._ensure_channels()

    @property
    def mode(self) -> str:
        return self._mode

    def status(self) -> Dict[str, Any]:
        with self._lock:
            sess = self._session
            recording = self._mode == Mode.RECORDING
            return {
                "enabled": self.is_enabled(),
                "available": self.available,
                "robot_connected": robot.connected,
                "mode": self._mode,
                "include_waist": bool(settings.teach_include_waist),
                "recording": (
                    {
                        "name": sess.name,
                        "frames": len(sess.frames),
                        "elapsed_s": round(time.time() - sess.started_at, 2),
                        "max_s": settings.teach_max_duration_s,
                    }
                    if recording
                    else None
                ),
                "last_played": self._last_played,
                "last_error": self._last_error,
                "count": len(list_recordings()),
            }

    # ── Recording ───────────────────────────────────────────────────────

    def start_recording(self, name: str) -> Dict[str, Any]:
        slug = _slugify(name)
        if not self.is_enabled():
            return _skip("disabled")
        if not slug:
            return _skip("invalid_name")
        if not robot.connected:
            return _skip("robot_disconnected")
        with self._lock:
            if self._mode != Mode.IDLE:
                return _skip(f"busy_{self._mode}")
            if not self._ensure_channels():
                return _skip("channel_unavailable")
            # Need a fresh state sample so we know where the arms are.
            if self._read_state() is None:
                return _skip("no_lowstate")
            self._session = _Session(
                name=slug,
                started_at=time.time(),
                joints=_joint_set(),
            )
            self._mode = Mode.RECORDING
            self._stop_evt.clear()
            self._worker = threading.Thread(
                target=self._record_loop, name="teach-record", daemon=True
            )
            self._worker.start()
        log.info("Teach: recording '%s' started (%d joints)", slug, len(self._session.joints))
        return {"ok": True, "name": slug, "mode": self._mode}

    def stop_recording(self) -> Dict[str, Any]:
        with self._lock:
            if self._mode != Mode.RECORDING:
                return _skip("not_recording")
            self._stop_evt.set()
            worker = self._worker
        if worker is not None:
            worker.join(timeout=settings.teach_ramp_s + 2.0)
        # _record_loop persists + resets mode on the way out.
        with self._lock:
            saved = self._session.name
            frames = len(self._session.frames)
        rec = get_recording(saved) if saved else None
        if rec is None:
            return _skip("save_failed")
        log.info("Teach: recording '%s' saved (%d frames)", saved, frames)
        return {"ok": True, "name": saved, "frames": frames, "duration_s": rec.get("duration_s")}

    def _record_loop(self) -> None:
        joints = self._session.joints
        kd = float(settings.teach_record_kd)
        dt = 1.0 / float(settings.teach_record_hz)
        ramp_s = float(settings.teach_ramp_s)
        max_s = float(settings.teach_max_duration_s)
        try:
            # Take soft control of the arms: kp=0 so the operator can move
            # them freely, kd small so they don't flop. Weight 0→1.
            self._ramp_weight(0.0, 1.0, ramp_s, hold_compliant=True, kd=kd)
            t0 = time.time()
            next_t = t0
            while not self._stop_evt.is_set() and not settings.shutdown:
                st = self._read_state()
                if st is not None:
                    frame = [float(st.motor_state[j].q) for j in joints]
                    self._session.frames.append(frame)
                # Keep publishing the compliant hold so the arms stay soft.
                self._publish_compliant(joints, kd)
                if (time.time() - t0) >= max_s:
                    log.warning("Teach: recording hit max duration (%.0fs) — auto-stopping", max_s)
                    break
                next_t += dt
                sleep = next_t - time.time()
                if sleep > 0:
                    time.sleep(sleep)
            # Hand the arms back to locomotion.
            self._ramp_weight(1.0, 0.0, ramp_s)
            self._persist_session()
        except Exception as e:
            self._last_error = f"record: {e}"
            log.exception("Teach: record loop failed")
            self._safe_release()
        finally:
            with self._lock:
                self._mode = Mode.IDLE
                self._worker = None

    def _persist_session(self) -> None:
        sess = self._session
        if not sess.frames:
            self._last_error = "no_frames"
            return
        dt = 1.0 / float(settings.teach_record_hz)
        payload = {
            "name": sess.name,
            "created_at": sess.started_at,
            "hz": settings.teach_record_hz,
            "dt": dt,
            "joints": sess.joints,
            "joint_names": [JOINT_NAMES.get(j, str(j)) for j in sess.joints],
            "frames": sess.frames,
            "duration_s": round(len(sess.frames) * dt, 2),
            "include_waist": bool(settings.teach_include_waist),
        }
        _DIR.mkdir(parents=True, exist_ok=True)
        path = _DIR / f"{sess.name}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)

    # ── Replay ──────────────────────────────────────────────────────────

    def play(self, name: str) -> Dict[str, Any]:
        """Replay a recording. Blocking — intended to run on the command-bus
        consumer thread so it serialises against say()/gesture()."""
        slug = _slugify(name)
        if not self.is_enabled():
            return _skip("disabled")
        rec = get_recording(slug)
        if rec is None:
            return _skip("not_found")
        if not robot.connected:
            return _skip("robot_disconnected")
        with self._lock:
            if self._mode != Mode.IDLE:
                return _skip(f"busy_{self._mode}")
            if not self._ensure_channels():
                return _skip("channel_unavailable")
            if self._read_state() is None:
                return _skip("no_lowstate")
            self._mode = Mode.PLAYING
            self._stop_evt.clear()
        try:
            self._play_blocking(rec)
            self._last_played = slug
            return {"ok": True, "name": slug, "frames": len(rec.get("frames", []))}
        except Exception as e:
            self._last_error = f"play: {e}"
            log.exception("Teach: replay failed")
            self._safe_release()
            return _skip(f"play_exception:{type(e).__name__}")
        finally:
            with self._lock:
                self._mode = Mode.IDLE

    def _play_blocking(self, rec: Dict[str, Any]) -> None:
        joints: List[int] = [int(j) for j in rec.get("joints", [])]
        frames: List[List[float]] = rec.get("frames", [])
        if not joints or not frames:
            return
        kp = float(settings.teach_play_kp)
        kd = float(settings.teach_play_kd)
        dt = float(rec.get("dt") or (1.0 / settings.teach_record_hz))
        ramp_s = float(settings.teach_ramp_s)
        approach_s = float(settings.teach_approach_s)

        st = self._read_state()
        start_q = [float(st.motor_state[j].q) for j in joints] if st else list(frames[0])
        target0 = [float(v) for v in frames[0]]

        # Phase 1 — take control + glide from current pose to frame 0.
        steps = max(1, int(approach_s / dt))
        for i in range(steps + 1):
            if self._stop_evt.is_set() or settings.shutdown:
                self._ramp_weight(self._current_weight, 0.0, ramp_s)
                return
            a = i / steps
            weight = min(1.0, a * (approach_s / ramp_s)) if ramp_s > 0 else 1.0
            q = [start_q[k] + (target0[k] - start_q[k]) * a for k in range(len(joints))]
            self._publish_positions(joints, q, kp, kd, weight)
            time.sleep(dt)

        # Phase 2 — stream the recorded trajectory at full weight.
        for frame in frames:
            if self._stop_evt.is_set() or settings.shutdown:
                break
            q = [float(v) for v in frame]
            self._publish_positions(joints, q, kp, kd, 1.0)
            time.sleep(dt)

        # Phase 3 — hold last frame, then hand back to locomotion.
        last = [float(v) for v in frames[-1]]
        hold_steps = max(1, int(0.3 / dt))
        for _ in range(hold_steps):
            self._publish_positions(joints, last, kp, kd, 1.0)
            time.sleep(dt)
        self._ramp_weight(1.0, 0.0, ramp_s, hold_positions=(joints, last, kp, kd))

    # ── Emergency / cleanup ─────────────────────────────────────────────

    def stop(self) -> Dict[str, Any]:
        """Abort any record/replay and hand the arms back to locomotion.
        Always safe to call, even when idle."""
        self._stop_evt.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=settings.teach_ramp_s + 2.0)
        self._safe_release()
        with self._lock:
            self._mode = Mode.IDLE
            self._worker = None
        return {"ok": True, "mode": self._mode}

    def _safe_release(self) -> None:
        """Best-effort: drop the arm_sdk weight to 0 so locomotion regains
        the arms. Never raises."""
        try:
            self._ramp_weight(self._current_weight, 0.0, settings.teach_ramp_s)
        except Exception:
            try:
                self._publish_weight_only(0.0)
            except Exception:
                log.debug("Teach: safe_release weight write failed", exc_info=True)

    # ── DDS plumbing (lazy SDK import, mirrors app/robot.py) ────────────

    _current_weight: float = 0.0

    def _ensure_channels(self) -> bool:
        if self._pub is not None and self._sub is not None:
            return True
        try:
            from unitree_sdk2py.core.channel import (
                ChannelPublisher,
                ChannelSubscriber,
            )
            from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
            from unitree_sdk2py.utils.crc import CRC
        except Exception as e:  # pragma: no cover - depends on on-robot SDK
            self._last_error = f"sdk_unavailable: {e}"
            log.info("Teach: unitree_sdk2py low-level unavailable (%s) — teach disabled", e)
            return False
        try:
            pub = ChannelPublisher("rt/arm_sdk", LowCmd_)
            pub.Init()
            sub = ChannelSubscriber("rt/lowstate", LowState_)
            sub.Init(self._on_lowstate, 10)
            self._pub = pub
            self._sub = sub
            self._crc = CRC()
            log.info("Teach: low-level arm channel ready (rt/arm_sdk + rt/lowstate)")
            return True
        except Exception as e:  # pragma: no cover
            self._last_error = f"channel_init: {e}"
            log.warning("Teach: channel init failed (%s)", e)
            self._pub = self._sub = self._crc = None
            return False

    def _on_lowstate(self, msg: Any) -> None:
        with self._state_lock:
            self._latest_state = msg

    def _read_state(self) -> Any:
        with self._state_lock:
            return self._latest_state

    def _new_cmd(self):
        from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_

        return unitree_hg_msg_dds__LowCmd_()

    def _write(self, cmd) -> None:
        if self._pub is None or self._crc is None:
            return
        cmd.crc = self._crc.Crc(cmd)
        self._pub.Write(cmd)

    def _publish_positions(
        self, joints: List[int], q: List[float], kp: float, kd: float, weight: float
    ) -> None:
        cmd = self._new_cmd()
        cmd.motor_cmd[WEIGHT_JOINT].q = float(max(0.0, min(1.0, weight)))
        for idx, j in enumerate(joints):
            m = cmd.motor_cmd[j]
            m.mode = 1
            m.q = float(q[idx])
            m.dq = 0.0
            m.kp = float(kp)
            m.kd = float(kd)
            m.tau = 0.0
        self._current_weight = cmd.motor_cmd[WEIGHT_JOINT].q
        self._write(cmd)

    def _publish_compliant(self, joints: List[int], kd: float) -> None:
        """kp=0 damping hold — arms stay soft & movable while we record."""
        cmd = self._new_cmd()
        cmd.motor_cmd[WEIGHT_JOINT].q = 1.0
        for j in joints:
            m = cmd.motor_cmd[j]
            m.mode = 1
            m.q = 0.0
            m.dq = 0.0
            m.kp = 0.0
            m.kd = float(kd)
            m.tau = 0.0
        self._current_weight = 1.0
        self._write(cmd)

    def _publish_weight_only(self, weight: float) -> None:
        cmd = self._new_cmd()
        cmd.motor_cmd[WEIGHT_JOINT].q = float(max(0.0, min(1.0, weight)))
        self._current_weight = cmd.motor_cmd[WEIGHT_JOINT].q
        self._write(cmd)

    def _ramp_weight(
        self,
        start: float,
        end: float,
        seconds: float,
        *,
        hold_compliant: bool = False,
        kd: float = 1.0,
        hold_positions: Optional[tuple] = None,
    ) -> None:
        """Linearly move the blend weight start→end. Optionally keep
        publishing a compliant (record) or position (replay) hold while we
        ramp so the arms don't twitch during the transfer."""
        dt = 1.0 / float(settings.teach_record_hz)
        steps = max(1, int(max(0.0, seconds) / dt))
        joints = _joint_set()
        for i in range(steps + 1):
            w = start + (end - start) * (i / steps)
            if hold_positions is not None:
                hj, hq, hkp, hkd = hold_positions
                self._publish_positions(hj, hq, hkp, hkd, w)
            elif hold_compliant:
                cmd = self._new_cmd()
                cmd.motor_cmd[WEIGHT_JOINT].q = float(max(0.0, min(1.0, w)))
                for j in joints:
                    m = cmd.motor_cmd[j]
                    m.mode = 1
                    m.kp = 0.0
                    m.kd = float(kd)
                    m.tau = 0.0
                self._current_weight = cmd.motor_cmd[WEIGHT_JOINT].q
                self._write(cmd)
            else:
                self._publish_weight_only(w)
            time.sleep(dt)


# ── Module-level singleton + file helpers ───────────────────────────────
teach = TeachEngine()


def list_recordings() -> List[Dict[str, Any]]:
    """Lightweight listing (name + duration + frame count), no frame data."""
    if not _DIR.exists():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append(
            {
                "name": data.get("name", path.stem),
                "duration_s": data.get("duration_s"),
                "frames": len(data.get("frames", [])),
                "joints": len(data.get("joints", [])),
                "include_waist": bool(data.get("include_waist")),
                "created_at": data.get("created_at"),
            }
        )
    return out


def get_recording(name: str) -> Optional[Dict[str, Any]]:
    slug = _slugify(name)
    if not slug:
        return None
    path = _DIR / f"{slug}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def delete_recording(name: str) -> bool:
    slug = _slugify(name)
    path = _DIR / f"{slug}.json"
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except Exception:
        log.warning("Teach: failed to delete '%s'", slug)
        return False


def _skip(reason: str) -> Dict[str, Any]:
    return {"ok": False, "skipped": reason}
