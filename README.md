# reachy-bridge 🤖

Give your Reachy a remote body over HTTPS — securely. A small server that runs
**on the robot itself** exposes it to an AI agent anywhere on the internet,
with **Ed25519 request signatures** proving *which* agent is calling.
No open ports, no shared passwords for the agent, no anonymous access.

```
  agent (anywhere)                  Reachy Mini (onboard computer)
 ┌──────────────┐   https        ┌────────────────────────────┐
 │ signs every  │ ─────────────▶ │ bridge.py :8765            │─┐ localhost
 │ request with │  Tailscale     │  signature auth + panel    │ │ daemon :8000
 │ private key  │  Funnel        │  (bearer token)            │◀┘
 └──────────────┘ ◀───────────── └────────────────────────────┘
                     signed JSON
```

Why signatures instead of mTLS? Tailscale Funnel terminates TLS at its edge,
so mTLS can't work end-to-end. Signatures live at the HTTP layer and survive
any tunnel. See `docs/ARCHITECTURE.md`.

## Quickstart (Reachy Mini owners)

SSH into the robot and run the installer:

```bash
ssh pollen@reachy-mini.local        # factory password: root
curl -fsSLO https://raw.githubusercontent.com/sgosselin/muse-reachy-bridge/main/scripts/install-robot.sh
chmod +x install-robot.sh
./install-robot.sh
```

The script installs Tailscale, clones this repo, builds a virtualenv
(including the `reachy-mini` SDK so camera/mic/speaker work), asks for your
agent's **public** key, writes `.env` (daemon at `http://127.0.0.1:8000/api`,
random panel token), and enables two systemd services so the bridge and its
Funnel tunnel survive reboots. At the end it prints your public
`https://<…>.ts.net` URL and the panel token.

Then:

1. Open `https://<your-url>/panel` — paste the panel token, wiggle the head.
   If it moves, the bridge works.
2. Send your agent the funnel URL. Its public key is already registered from
   the install step — the agent's *private* key never leaves the agent's machine.

That's it. The agent drives the robot; you keep the panel and the e-stop.

Prerequisites: the robot logged into your Tailscale tailnet (`sudo tailscale
up` during install, or `TS_AUTHKEY=tskey-… ./install-robot.sh` for headless
login), and Funnel enabled in the Tailscale admin console.

## Giving an agent access (the 60-second ceremony)

1. Agent generates an Ed25519 keypair on its own machine and sends you the
   **public** key (safe to paste in chat — it's public).
2. You paste it when `install-robot.sh` asks (or pass `AGENT_PUBKEY=…`); it
   lands in `clients/<name>.pub`.
3. To rotate later: `./scripts/add-client.sh <name>`, paste the new key, then
   `sudo systemctl restart reachy-bridge`. To revoke: `rm clients/<name>.pub`
   and restart. Done.

## API

All calls need either a valid Ed25519 signature (`X-Bridge-*` headers — see
`client/bridge_client.py`) or the panel bearer token. Units are **degrees**.

| Endpoint | What it does |
|---|---|
| `GET /health` | liveness, robot type, registered agent clients |
| `GET /state` | last commanded pose + raw daemon state |
| `GET /limits` | clamp table + interpolation modes |
| `POST /goto` | `{pitch, yaw, roll, duration, interpolation}` — clamped to safe ranges |
| `POST /preset/{nod,shake,look_around,curious}` | canned expression, background task |
| `POST /motors` | `{mode: enabled\|disabled\|gravity_compensation}` |
| `POST /estop` / `POST /estop/reset` | latching software e-stop |
| `ANY /proxy/{subpath}` | raw passthrough to the Mini daemon |
| `GET /camera/snapshot?width=` | JPEG frame from the robot camera |
| `POST /mic/record` | `{seconds: 1–30}` → mono 16 kHz WAV of mic audio |
| `POST /speaker/play?wait_seconds=&filename=` | raw audio bytes in body → robot speakers |
| `GET /sense/doa` | mic-array direction of arrival (radians + speech flag) |
| `GET /panel` | browser control panel (now with camera view) |

Agents: use `client/bridge_client.py` — it signs everything for you:

```bash
python client/bridge_client.py --url https://<your-funnel-url> --key ~/.ssh/my_bridge_key health
python client/bridge_client.py --url https://<your-funnel-url> --key ~/.ssh/my_bridge_key goto --yaw -30 --pitch 10
python client/bridge_client.py --url https://<your-funnel-url> --key ~/.ssh/my_bridge_key snapshot --out cam.jpg
python client/bridge_client.py --url https://<your-funnel-url> --key ~/.ssh/my_bridge_key record --seconds 5 --out hello.wav
```

## Media notes

On a real Reachy Mini the media endpoints are served by `media.py` through the
`reachy_mini` SDK's `MediaManager` (LOCAL backend — the bridge runs on the
robot, so it reads camera frames from the daemon's local IPC feed and audio
via GStreamer; no WebRTC needed). The SDK is imported lazily: without it the
media endpoints answer `501` with the reason, and everything else keeps
working. `scripts/install-robot.sh` installs it (`pip install reachy-mini`,
plus `python3-gi` from apt on Raspberry Pi OS so PyGObject doesn't build from
source).

## Layout

```
bridge.py                the server (auth, adapters, safety, audit, panel)
media.py                 camera / mic / speaker / direction-of-arrival
client/bridge_client.py  signing client library + smoke-test CLI
panel/panel.html         human control UI (camera view, sliders, e-stop)
scripts/install-robot.sh on-robot installer: Tailscale, venv, agent key intake,
                         .env, systemd + Funnel services (recommended)
scripts/setup.sh         wizard for running the bridge on a separate host
scripts/add-client.sh    add or rotate an agent's public key
clients/                 agent public keys (gitignored — yours, not the repo's)
docs/ARCHITECTURE.md     full design rationale
docs/SECURITY.md         threat model + operational guidance
deploy/                  always-on units if the bridge lives off-robot
```

## Safety

- Motion clamped to Pollen's published limits; `/estop` latches (423 on all
  motion until reset); every call lands in `audit.log` with the caller id.
- Don't leave the tunnel up unattended with a live robot. Read
  `docs/SECURITY.md` before exposing yours.

## Deploy notes

`install-robot.sh` already sets up always-on systemd services (bridge +
Funnel) on the robot. If you'd rather run the bridge on a separate machine
(e.g. a Mac on the same LAN as the robot), see
[deploy/README.md](deploy/README.md).

## Roadmap

- Real-time voice loop (VAD + STT → LLM → TTS on the robot; the bridge's
  `/mic/record` + `/speaker/play` are the transport, a realtime API is the brain)
- Full-size Reachy head motion (stub marked in `ClassicAdapter.goto`)
