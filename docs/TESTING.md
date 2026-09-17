# Local bridge test environment

Run these commands from the repository root. Use Python 3.10 or newer.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The environment is local to this checkout and ignored by Git. Activation is
optional (`source .venv/bin/activate`). The tests need neither the Reachy SDK
nor Tailscale, a running server, a robot, or real credentials.

## What the tests exercise

- Real Ed25519 signing and verification with temporary test keys, rejection
  of modified or replayed requests, panel tokens, and audit records.
- Simulated motion, input validation, clamping, daemon errors, and the
  ordinary e-stop/reset flow.
- Decodable JPEG snapshots and WAV recordings, plus simulated playback.
- Startup key discovery and the Mini daemon's motion/motor route contract.

Requests run inside FastAPI's test client. Fixtures replace robot discovery
and media initialization and reject normal outbound HTTP/socket connections.
Temporary files, test keys, audit logs, and playback files go into pytest's
temporary directory. Tests do not load your `.env`, use your `keys/`, or move
hardware. The startup test runs in its own temporary working directory.

## Known deployment blockers

Four tests are marked `readiness` and **strict xfail**: they describe existing
bugs, not passing behavior. Pytest reports them as expected failures. An
unexpected pass fails the suite so that the marker is removed when fixed.

1. Installers save public keys in `keys/`, but `main()` defaults to `clients/`.
   `.env.example` and the separate-host setup script also select `clients/`.
2. `/proxy` forwards movement commands while the software e-stop is latched.
3. Mini motion calls `/api/goto`; upstream exposes `/api/move/goto`.
4. Motor control calls `/api/motors/set-mode` with JSON; upstream exposes
   `/api/motors/set_mode/{mode}`. This also affects the e-stop's motor-off call.

The route fixtures reflect Pollen's source inspected on 2026-09-16:
[move router](https://github.com/pollen-robotics/reachy_mini/blob/main/src/reachy_mini/daemon/app/routers/move.py)
and [motor router](https://github.com/pollen-robotics/reachy_mini/blob/main/src/reachy_mini/daemon/app/routers/motors.py).
They are a compatibility check, not proof of the API version on your robot.
Check the robot's `/openapi.json` before selecting the deployed SDK/API version.

To turn these known failures into a failing deployment gate:

```bash
.venv/bin/python -m pytest -m readiness --runxfail
```

Passing the normal mock suite does **not** mean the bridge is ready for a live
robot. Hardware validation must separately check the actual daemon API,
motor disable, camera, microphone, speaker, and service startup after reboot.
The suite does not yet verify concurrent motion/e-stop handling or real-time
voice conversation.

## Optional local panel

Run a simulated bridge without an internet tunnel:

```bash
.venv/bin/python bridge.py --mock --clients-dir keys
```

Open `http://127.0.0.1:8765/panel`. If there is no configured panel token, the
bridge prints a temporary token on startup. Unlike the tests, this manual
command loads the checkout's `.env` and writes the configured audit log.
`--mock` explicitly selects simulated motion and media.
