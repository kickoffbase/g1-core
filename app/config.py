"""
Settings
========
All knobs live in `.env` (loaded by pydantic-settings). Keep this file as
the single source of truth — every module reads `settings.foo`, never an
ad-hoc env lookup. Adding a new knob is a 1-line change here + 1 line in
`.env.example` so the operator always knows what's available.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # ── Robot / DDS ────────────────────────────────────────────────────
    robot_enabled: bool = Field(
        default=True,
        description=(
            "If false, never touch unitree_sdk2py — service runs in "
            "speak-only mode using the system audio sink (handy for testing "
            "off-robot or when the SDK is broken). The webhook stays up."
        ),
    )
    network_interface: str = Field(default="eth0", description="DDS NIC towards the robot subnet")
    robot_volume: int = Field(default=100, ge=0, le=100)
    robot_init_timeout_s: float = Field(default=10.0, description="Per-call SDK timeout")
    robot_reconnect_min_s: float = Field(default=2.0, description="Initial backoff for DDS reconnect")
    robot_reconnect_max_s: float = Field(default=30.0, description="Cap for DDS reconnect backoff")

    # ── Audio routing ──────────────────────────────────────────────────
    # `unitree`  → built-in G1 speaker via AudioClient.PlayStream
    # `system`   → host audio sink (PulseAudio / ALSA → AUX / Bluetooth / USB)
    # `both`     → both at once
    audio_output: str = Field(default="system", description="unitree | system | both")
    system_audio_cmd: str = Field(
        default="aplay -q -t raw -f S16_LE -r 16000 -c 1",
        description="Command that accepts raw s16le mono 16kHz PCM on stdin",
    )
    system_audio_fallback_cmd: str = Field(
        default="aplay -q -t raw -f S16_LE -r 16000 -c 1",
        description="Fallback if SYSTEM_AUDIO_CMD closes its stdin",
    )

    # ── PulseAudio sink switcher (builtin G1 chest ↔ external JBL) ─────
    # Run `pactl list sinks short` on the Orin, paste names. Empty = the
    # corresponding mode no-ops (handy if the host has no PulseAudio at
    # all — then everything just rides SYSTEM_AUDIO_CMD).
    audio_sink_builtin: str = Field(default="", description="PulseAudio sink for the G1 chest speaker")
    audio_sink_jbl: str = Field(default="", description="PulseAudio sink for the paired BT/JBL speaker")
    audio_sink_default: str = Field(
        default="jbl",
        description="Which sink the speaker prefers when AUDIO_OUTPUT uses 'system' (builtin | jbl)",
    )
    audio_preroll_ms: int = Field(
        default=600,
        ge=0,
        le=2000,
        description=(
            "Silence (ms) prepended to every utterance. Bluetooth speakers "
            "leave standby in 150-300 ms AND PA buffers ~200 ms on top — so "
            "anything below ~500 ms still clips the first phoneme. 0 disables "
            "preroll entirely."
        ),
    )

    # ── Bluetooth speaker ──────────────────────────────────────────────
    # When set we proactively reconnect to this MAC every watchdog tick if
    # the link is gone — keeps the speaker alive across power-cycles and
    # range hiccups. Helper script: `scripts/bt-connect.sh`.
    bluetooth_mac: str = Field(default="", description="Target BT speaker MAC, e.g. AA:BB:CC:DD:EE:FF")
    bluetooth_autoreconnect: bool = Field(default=True)

    # ── ElevenLabs streaming TTS ───────────────────────────────────────
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = Field(default="eleven_flash_v2_5")
    elevenlabs_stability: float = Field(default=0.4, ge=0.0, le=1.0)
    elevenlabs_similarity: float = Field(default=0.75, ge=0.0, le=1.0)
    elevenlabs_style: float = Field(default=0.4, ge=0.0, le=1.0)
    elevenlabs_speed: float = Field(default=1.0, ge=0.7, le=1.2)

    # ── Personality ────────────────────────────────────────────────────
    # Default slug at boot. The active slug is persisted in `state/active_personality`
    # so a webhook switch survives restart.
    personality: str = Field(default="comedian")

    # ── Webhook ────────────────────────────────────────────────────────
    webhook_host: str = Field(default="0.0.0.0")
    # Different from g1-brain (8765) so both can coexist during migration.
    webhook_port: int = Field(default=8766, ge=1, le=65535)
    webhook_api_key: str = Field(default="", description="X-API-Key header. Blank = open (only safe on 127.0.0.1)")
    command_history_size: int = Field(default=200, description="How many recent commands to remember for status polling")

    # ── ngrok tunnel ───────────────────────────────────────────────────
    ngrok_enabled: bool = Field(default=True)
    ngrok_domain: str = Field(default="", description="Optional reserved domain (stable public URL)")
    ngrok_api_port: int = Field(default=4041, description="Local ngrok inspector port (4040 is g1-brain's; pick a free one)")

    # ── Camera (MJPEG preview for the operator panel) ──────────────────
    # Default device is the head RGB camera on the Unitree G1 Orin
    # (consistently /dev/video2 across the fleet we tested). Override
    # via CAMERA_DEVICE in .env only if your unit differs. Accepts both
    # an integer V4L2 index ("0", "2") and a path ("/dev/video2").
    camera_enabled: bool = Field(default=True)
    camera_device: str = Field(default="/dev/video2", description="V4L2 index or /dev path")
    camera_width: int = Field(default=640, ge=64, le=4096)
    camera_height: int = Field(default=480, ge=64, le=4096)
    camera_fps: int = Field(default=10, ge=1, le=60)
    camera_jpeg_quality: int = Field(default=70, ge=1, le=100)
    camera_idle_close_s: int = Field(default=10, ge=0, le=600)

    # ── Watchdog ───────────────────────────────────────────────────────
    watchdog_interval_s: float = Field(default=15.0, description="How often to re-check subsystems")

    shutdown: bool = False

    model_config = {
        "env_file": str(REPO_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
