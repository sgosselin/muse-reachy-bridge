# Architecture

`reachy-bridge` gives a Reachy robot a remote body over HTTPS: a small server
on the owner's LAN exposes the robot to an AI agent anywhere on the internet,
with cryptographic proof of *which* agent is calling.

## The constraint that shapes everything

The recommended exposure path is Tailscale Funnel (or Cloudflare Tunnel):
zero port-forwarding, public `https://` URL. But these tunnels **terminate TLS
at the edge** — by the time traffic reaches the bridge it's plain HTTP from
localhost. Classic mTLS therefore cannot work end-to-end: there is no client
certificate left to verify.

So authentication happens at the **application layer** with Ed25519 request
signatures, which survive any tunnel or proxy. The tunnel's TLS still provides
confidentiality in transit; signatures provide *identity*.

## Components

```
  agent (anywhere)                    owner's Mac (LAN)              robot
 ┌────────────────┐   https        ┌──────────────────────┐   LAN  ┌──────────────┐
 │ signs requests │ ─────────────▶ │ bridge.py :8765      │ ─────▶ │ Reachy Mini  │
 │ w/ private key │  tunnel edge   │  ├─ auth middleware  │        │ daemon :8000 │
 │                │  (TLS term.)   │  ├─ adapters         │        └──────────────┘
 │ client/        │ ◀───────────── │  ├─ safety / e-stop  │
 │ bridge_client  │   signed JSON   │  ├─ audit log        │
 └────────────────┘               │  └─ panel/ (token)   │
                                  └──────────────────────┘
```

| Piece | Role |
|---|---|
| `bridge.py` | FastAPI server: signature verification, robot adapters, safety clamps, latching e-stop, audit log, serves the panel |
| `client/bridge_client.py` | Signing client library (stdlib + `cryptography`) — what agents use; doubles as a smoke-test CLI |
| `panel/panel.html` | Human control UI, bearer-token auth, served by the bridge |
| `scripts/setup.sh` | Interactive one-command installer: venv, robot probe, agent pubkey intake, panel token, `.env` |
| `scripts/add-client.sh` | Add/replace an agent public key later |
| `clients/<name>.pub` | Agent public keys (gitignored, owner-local) |
| `audit.log` | Append-only JSON log of every API call (gitignored, owner-local) |

## Authentication in detail

**Door 1 — agent API (Ed25519 signatures).** For each request the client builds:

```
message   = method + "\n" + path + "\n" + timestamp + "\n" + nonce + "\n" + sha256(body_hex)
signature = base64( ed25519_sign(private_key, message) )
```

sent as `X-Bridge-Client`, `X-Bridge-Timestamp`, `X-Bridge-Nonce`,
`X-Bridge-Signature`. The bridge:

1. looks up `clients/<name>.pub`,
2. rejects timestamps outside ±`BRIDGE_AUTH_WINDOW` (default 120 s) — clients
   and server need roughly synced clocks (NTP is enough),
3. rejects already-seen nonces (in-memory cache, replay protection),
4. verifies the signature — only the holder of the private key can produce it.

**Door 2 — human panel (bearer token).** `Authorization: Bearer <BRIDGE_PANEL_TOKEN>`
for the owner's browser/phone. A separate credential from the agent's key, so
the two access paths can be rotated and revoked independently. The panel HTML
itself is served unauthenticated (it's useless without the token — every API
call it makes is authenticated).

**Key ceremony** (the private key never crosses the wire):

1. The agent generates an Ed25519 keypair on its own machine.
2. The agent sends the *public* key to the owner (safe to paste in chat).
3. The owner runs `setup.sh` (or `add-client.sh`) and pastes it → `clients/<name>.pub`.
4. Revocation = `rm clients/<name>.pub` + restart.

## Robot adapters

`bridge.py` speaks to the robot through a small adapter interface
(`state / goto / set_motors / proxy`):

- **Reachy Mini** — the onboard daemon's REST API (`POST /api/goto`,
  `GET /api/state/full-state`, `POST /api/motors/set-mode`). Auto-probed at
  `reachy-mini.local:8000` / `localhost:8000`, or set `BRIDGE_MINI_URL`.
  The bridge's `/goto` takes **degrees** and converts to the daemon's radians.
- **Full-size Reachy 1/2/2023** — classic `reachy-sdk` over gRPC. State readout
  is wired; head motion is deliberately left as a marked stub because the SDK's
  motion calls differ per generation — wire your generation's call rather than
  shipping a guess that moves hardware wrong.
- **Mock** — fake robot (`BRIDGE_MOCK=1`) for testing auth, API, panel and the
  tunnel with no hardware.

`ANY /proxy/{subpath}` forwards raw calls to the Mini daemon, so new daemon
endpoints (body yaw, antennas, emotions, sounds) are reachable the moment
they're discovered via the daemon's live `/docs` — no bridge changes needed.

## Media adapters

`media.py` mirrors the motion-adapter pattern (`snapshot_jpeg / record / play / doa`):

- **Reachy Mini** — the `reachy_mini` SDK's `MediaManager` with the LOCAL
  backend. The bridge runs on the robot, so the camera comes from the
  daemon's local IPC feed (`get_frame_jpeg()`) and audio goes through
  GStreamer: `start_recording()` + `get_audio_sample()` (float32 stereo @
  16 kHz, downmixed to mono WAV) for the mic, `play_sound(path)` for the
  speakers, `get_DoA()` for the mic array's direction of arrival. No WebRTC
  on the bridge side — the tunnel only needs to carry HTTPS. The SDK is
  imported lazily; without it the media endpoints answer **501** with the
  reason instead of breaking the bridge.
- **Mock** — synthetic test card JPEG, sine-wave WAV, instant playback,
  fixed DoA. The whole media surface is testable with no hardware.

`/speaker/play` takes raw audio bytes (not multipart): the auth layer reads
the request body to verify the signature, which would break form parsing.
Played files land in a temp dir and are garbage-collected after an hour,
because `play_sound()` reads the file asynchronously.

## Safety

- Every motion value is clamped to Pollen's published limits (head pitch/roll
  ±40°, yaw ±180°, duration 0.2–10 s); responses flag which axes were clamped.
- `POST /estop` latches a software e-stop (and cuts motors); all motion
  endpoints return **423** until `POST /estop/reset`.
- Every API call is appended to `audit.log` with timestamp, client id, method,
  path and status.

## What's deliberately out of scope (v1)

- **Real-time voice.** `/mic/record` + `/speaker/play` are the transport for
  a voice loop, but duplex conversation (VAD, barge-in) belongs in a
  dedicated on-robot process against a realtime speech API, not in this bridge.
- **Multi-robot.** One bridge = one robot. Run two bridges on different ports
  for two robots.
