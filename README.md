# g1-core

A small, **stable** companion to `g1-brain`. It does one thing well:
**speak text on demand from any device on the internet**, with the active
personality's voice, through whichever audio sink you choose (the G1's
built-in speaker, or a Bluetooth / USB / AUX speaker on the host).

```
HTTP / ngrok                 ┌────────────────────────────────────┐
   │ POST /say "hello"  ───► │  webhook  ─►  command bus  ─► main │
   ▼                         │                                ▼   │
                             │                            speaker │ ─► ElevenLabs WS ─► PCM
                             │                                ▼   │           ┌► G1 AudioClient
                             │                          audio sink│           └► Bluetooth / AUX
                             └────────────────────────────────────┘
```

## What's different from g1-brain

`g1-brain` is the full assistant (mic → STT → LLM → TTS → speakers + music
+ news + crypto). `g1-core` keeps **only** the parts you can't afford to
have flakey:

| g1-core has                                     | g1-core does NOT have      |
|-------------------------------------------------|----------------------------|
| Webhook (`/say`, `/greet`, `/health`, …)        | Microphone / ASR / Whisper |
| Personality switching (live, persisted)         | LLM brain                  |
| ElevenLabs streaming TTS                        | Music generation           |
| Bluetooth / USB / AUX speaker support           | News / crypto feeds        |
| `/bluetooth/{scan,connect,disconnect,sink}` API | Locomotion commands        |
| PulseAudio sink switcher (builtin ↔ JBL)        | Robohire DB poller         |
| Force-unitree fallback when BT dies mid-stream  |                            |
| Self-healing robot DDS connection (optional)    |                            |
| Self-supervising ngrok tunnel                   |                            |
| Idempotent commands with status polling         |                            |
| Watchdog (BT + DDS auto-reconnect)              |                            |
| Pluggable services + auto-mounted HTTP routers  |                            |

The two services use **different ports** (`8765` vs `8766`) so they can
coexist during the migration. `install.sh` will stop `g1-brain.service`
automatically because both compete for the single ngrok agent slot on a
free-tier account.

## File layout

```
g1-core/
├── main.py                # orchestrator: registers services, drains the bus
├── app/
│   ├── config.py          # one Settings class — single source of truth
│   ├── personality.py     # JSON-backed personality registry + state file
│   ├── command_bus.py     # in-process queue with idempotency + history
│   ├── audio.py           # SystemAudioPlayer (aplay/paplay subprocess)
│   ├── audio_sink.py      # pactl set-default-sink switcher (builtin ↔ jbl)
│   ├── bluetooth.py       # bluetoothctl wrappers (scan/connect/info/...)
│   ├── tts.py             # ElevenLabs streaming WebSocket
│   ├── robot.py           # Unitree SDK wrapper with reconnect (optional)
│   └── speaker.py         # the only "say(text)" the rest of the code uses
├── services/              # pluggable feature modules (one file = one feature)
│   ├── base.py            # Service: name/start/stop/health/http_router
│   ├── webhook.py         # FastAPI HTTP API + auto-mounts other services' routers
│   ├── bluetooth.py       # /bluetooth/{devices,scan,connect,disconnect,sink}
│   ├── tunnel.py          # ngrok supervisor (kills stale agents, respawns)
│   └── watchdog.py        # periodic BT + robot reconnect
├── personalities/
│   ├── comedian.json
│   ├── kickoff.json
│   └── nickk.json
├── systemd/
│   ├── g1-core.service    # USER-level unit
│   ├── install.sh         # disables g1-brain, installs + starts g1-core
│   ├── uninstall.sh
│   ├── preflight.sh       # kills stale processes, frees ports — runs on every start
│   └── run.sh             # tiny launcher (exec python -m main)
├── scripts/
│   ├── bt-connect.sh      # pair + connect a Bluetooth speaker
│   ├── health.sh          # one-line health probe
│   └── say.sh             # one-line speak probe
├── state/                 # active_personality is persisted here
├── .env.example
└── requirements.txt
```

## Install on the Orin

```bash
# 1. Get the code into ~/g1-core (from Windows: PowerShell scp / WinSCP / WSL)
scp -r g1-core unitree@192.168.123.164:~/

# 2. SSH in and configure
ssh unitree@192.168.123.164
cd ~/g1-core
pip3 install -r requirements.txt
cp .env.example .env
nano .env   # set ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, NGROK_DOMAIN, WEBHOOK_API_KEY

# 3. (optional) pair a Bluetooth speaker
bash scripts/bt-connect.sh AA:BB:CC:DD:EE:FF
# add BLUETOOTH_MAC=AA:BB:CC:DD:EE:FF to .env

# 4. Install + start the service (also stops g1-brain if active).
#    Always go through bootstrap.py — it strips Windows CRLF from every
#    shell script first and only THEN runs install.sh, so a Windows-
#    edited file can never break the install with `$'\r': command not
#    found`. Subsequent restarts can use plain `systemctl --user
#    restart g1-core`.
python3 systemd/bootstrap.py

# 5. Watch it boot
journalctl --user -u g1-core -f
```

### "$'\r': command not found" / "set: pipefail: invalid option name"

You copied files from Windows and CRLF leaked into a `.sh`. Two recoveries:

```bash
# A — preferred: bootstrap re-sanitizes everything and runs install.sh
python3 ~/g1-core/systemd/bootstrap.py

# B — manual one-liner (no python needed)
cd ~/g1-core && sed -i 's/\r$//' systemd/*.sh systemd/*.service scripts/*.sh && \
  bash systemd/install.sh
```

Permanent prevention is already wired in: `.editorconfig` tells Cursor /
VS Code / JetBrains to save every `*.sh` with LF, and `.gitattributes`
forces git to do the same on checkout. As long as you edit files
through one of those, CRLF can't sneak back in.

## External commands

```bash
# health
curl -s http://127.0.0.1:8766/health

# speak (idempotent: the same command_id will not speak twice)
curl -s -X POST http://<orin>:8766/say \
     -H "Content-Type: application/json" \
     -H "X-API-Key: <key>" \
     -d '{"text": "Welcome to the stage, humans.", "command_id": "deck-button-7"}'
# → {"id":"deck-button-7","kind":"SAY","status":"queued",...}

# poll the result
curl -s http://<orin>:8766/commands/deck-button-7

# greet with the personality intro line
curl -s -X POST http://<orin>:8766/greet -H "X-API-Key: <key>"

# personalities
curl -s http://<orin>:8766/control/personalities
curl -s -X POST http://<orin>:8766/control/personality \
     -H "Content-Type: application/json" -H "X-API-Key: <key>" \
     -d '{"slug":"nickk"}'
```

## Bluetooth from the operator UI (no SSH)

```bash
# list paired + visible devices
curl -s http://<orin>:8766/bluetooth/devices

# scan 10s and return everything we found
curl -s -X POST http://<orin>:8766/bluetooth/scan \
     -H 'Content-Type: application/json' -d '{"timeout_s":10}'

# pair + trust + connect (idempotent)
curl -s -X POST http://<orin>:8766/bluetooth/connect \
     -H 'Content-Type: application/json' \
     -d '{"mac":"AA:BB:CC:DD:EE:FF"}'

# flip the system audio sink without reconnecting anything
curl -s -X POST http://<orin>:8766/bluetooth/sink \
     -H 'Content-Type: application/json' -d '{"mode":"jbl"}'      # → BT
curl -s -X POST http://<orin>:8766/bluetooth/sink -d '{"mode":"builtin"}'  # → G1 chest
```

Set sink names in `.env` once (run `pactl list sinks short` on the Orin):

```
AUDIO_SINK_BUILTIN=alsa_output.platform-snd_aloop.0.analog-stereo
AUDIO_SINK_JBL=bluez_sink.AA_BB_CC_DD_EE_FF.a2dp_sink
AUDIO_SINK_DEFAULT=jbl
```

If `jbl` is requested but the sink is gone (BT off / out of range), the
switcher silently falls back to `builtin` so the robot keeps talking
through the chest speaker. If the BT speaker dies *mid-utterance*, the
speaker module force-routes the rest of the audio to the G1 built-in
speaker — no silent dropouts.

## Stability principles (read these once)

1. **One mouth.** All TTS goes through the command bus; the bus is drained
   by a single thread (main). No two utterances ever overlap.
2. **Subsystems heal themselves, the process never crashes for a network
   reason.** TTS WS dies → that one command is marked failed. BT speaker
   drops → next utterance respawns the sink (and the watchdog reconnects
   the link). DDS dies → robot ops return False, reconnect runs in the
   background, audio still plays through the system sink.
3. **systemd is the last-resort restarter.** It restarts the whole process
   on Python crashes; everything else is in-process supervision.
4. **Preflight kills stale state.** Every start: kill old python, kill old
   ngrok, free ports — so a hung process from a previous boot can't
   silently break the new one.
5. **Idempotent commands.** Clients re-send with the same `command_id` on
   network errors; we de-duplicate.
6. **One feature = one file under `services/`.** Adding MQTT, a button
   GPIO, a scheduled poke etc. is `class XService(Service)` + one
   `service_registry.register(...)` line in `main.py`. Touching `app/` is
   never required.

## Adding a new feature (the modular promise)

```python
# services/scheduler.py
from services.base import Service
from app.command_bus import bus, KIND_SAY
import threading, time

class SchedulerService(Service):
    name = "scheduler"
    def __init__(self): self._stop = threading.Event(); self._t = None
    def start(self):
        self._t = threading.Thread(target=self._loop, daemon=True); self._t.start()
    def stop(self): self._stop.set()
    def health(self): return {"running": self._t is not None and self._t.is_alive()}
    def _loop(self):
        while not self._stop.is_set():
            bus.submit(KIND_SAY, payload={"text": "Hourly sponsor reminder."}, source="scheduler")
            self._stop.wait(3600)
```

Then in `main.py`:

```python
from services.scheduler import SchedulerService
service_registry.register(SchedulerService())
```

That's the whole change — no edits to `app/`, no edits to existing
services. `/health` will start including the new entry automatically.

## Day-to-day operations

```bash
# logs
journalctl --user -u g1-core -f

# restart after editing .env or pulling code
systemctl --user restart g1-core

# port / process diagnostic
ss -tlnp | grep 8766
ps -ef | grep -E 'g1-core|ngrok'

# current public ngrok URL (if NGROK_DOMAIN is unset)
curl -s http://127.0.0.1:8766/health | python3 -c 'import sys,json;print(json.load(sys.stdin)["services"]["tunnel"]["public_url"])'

# uninstall
bash systemd/uninstall.sh
```

## Troubleshooting

- **`/health` works but nothing speaks** → check `health` for
  `tts.session_error` (quota/auth). Top up ElevenLabs credits or rotate
  the key, then `systemctl --user restart g1-core`.
- **ngrok doesn't open a tunnel** → free-tier allows one agent per
  account. Preflight kills stray `ngrok` processes on every start, but
  another machine using the same authtoken will still steal the slot.
- **No sound from Bluetooth speaker** → run `pactl list short sinks` to
  confirm the BT sink exists and is the default; rerun
  `scripts/bt-connect.sh`.
- **`Robot: DDS init failed`** → either set `ROBOT_ENABLED=false` (audio
  still works via system sink) or fix the SDK install (see g1-brain's
  README, steps 4–5).
