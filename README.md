# reachy-bridge 🤖

Give your Reachy a remote body over HTTPS — securely. A small server on your
Mac (same LAN as the robot) exposes it to an AI agent anywhere on the
internet, with **Ed25519 request signatures** proving *which* agent is calling.
No open ports, no shared passwords for the agent, no anonymous access.

```
  agent (anywhere)              your Mac (LAN)               robot
 ┌──────────────┐  https     ┌────────────────────┐   LAN  ┌──────────────┐
 │ signs every  │ ─────────▶ │ bridge.py :8765    │ ─────▶ │ Reachy Mini  │
 │ request with │  tunnel    │  signature auth +  │        │ daemon :8000 │
 │ private key  │  (Funnel)  │  panel (token)     │        └──────────────┘
 └──────────────┘ ◀───────── └────────────────────┘
                    signed JSON
```

Why signatures instead of mTLS? Tailscale Funnel and Cloudflare Tunnel
terminate TLS at their edge, so mTLS can't work end-to-end. Signatures live
at the HTTP layer and survive any tunnel. See `docs/ARCHITECTURE.md`.

## Quickstart (robot owners)

```bash
git clone <this-repo> && cd reachy-bridge
./scripts/setup.sh        # venv, robot probe, agent key intake, .env — ~2 min
source .venv/bin/activate
python bridge.py          # or: BRIDGE_MOCK=1 python bridge.py  (no hardware)
```

Then:

1. Open http://127.0.0.1:8765/panel — paste the panel token from `.env`, wiggle the head. If it moves, the bridge works.
2. In another terminal: `tailscale funnel 8765` → copy the public `https://…` URL.
3. Send your agent **two** things: the funnel URL, and your agent's *public* key
   (you pasted it into `clients/` during setup — the agent's *private* key
   never leaves the agent's machine).

That's it. The agent drives the robot; you keep the panel and the e-stop.

## Giving an agent access (the 60-second ceremony)

1. Agent generates an Ed25519 keypair on its own machine and sends you the
   **public** key (safe to paste in chat — it's public).
2. You run `./scripts/add-client.sh astro`, paste the key, restart `bridge.py`.
3. To revoke: `rm clients/astro.pub` and restart. Done.

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
working. Install it on the bridge host with `pip install reachy-mini`
(`scripts/install-robot.sh` does this; on Raspberry Pi OS it also pulls
`python3-gi` from apt so PyGObject doesn't build from source).

## Layout

```
bridge.py              the server (auth, adapters, safety, audit, panel)
client/bridge_client.py  signing client library + smoke-test CLI
panel/panel.html       human control UI
scripts/setup.sh       one-command installer / wizard
scripts/add-client.sh  add or rotate an agent's public key
clients/               agent public keys (gitignored — yours, not the repo's)
docs/ARCHITECTURE.md   full design rationale
docs/SECURITY.md       threat model + operational guidance
```

## Safety

- Motion clamped to Pollen's published limits; `/estop` latches (423 on all
  motion until reset); every call lands in `audit.log` with the caller id.
- Don't leave the tunnel up unattended with a live robot. Read
  `docs/SECURITY.md` before exposing yours.

## Deploy (always-on)

`deploy/` has launchd (macOS) and systemd (Linux/Pi) units for the bridge
plus the Tailscale Funnel tunnel, so both survive reboots and crashes.
Full steps in [deploy/README.md](deploy/README.md). Tip: if your Reachy is on
your Tailscale network, point `BRIDGE_MINI_URL` at its Tailscale IP and the
bridge host doesn't even need to be on the robot's LAN.

## Roadmap

- Real-time voice loop (VAD + STT → LLM → TTS on the robot; the bridge's
  `/mic/record` + `/speaker/play` are the transport, a realtime API is the brain)
- Full-size Reachy head motion (stub marked in `ClassicAdapter.goto`)
