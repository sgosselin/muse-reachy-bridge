#!/usr/bin/env python3
"""
reachy-bridge — give your Reachy a remote body over HTTPS.

Runs on a machine on the same LAN as the robot (e.g. your Mac), talks to the
robot's local control surface, and exposes a small authenticated REST API that
a remote agent can drive through Tailscale Funnel / Cloudflare Tunnel.

AUTHENTICATION (two doors, two credentials):
  1. Agent API — Ed25519 request signatures (proves WHO is calling).
     The agent holds the private key; the bridge holds the agent's public key
     in keys/<name>.pub. Every request carries:
         X-Bridge-Client:    <name>
         X-Bridge-Timestamp: <unix seconds>
         X-Bridge-Nonce:     <random hex>
         X-Bridge-Signature: <base64( ed25519_sign(
             method + "\\n" + path + "\\n" + timestamp + "\\n" +
             nonce + "\\n" + sha256(body_hex) ) )>
     The bridge verifies the signature, rejects timestamps older than
     BRIDGE_AUTH_WINDOW seconds, and rejects reused nonces (replay protection).
     Signatures live at the HTTP layer, so they survive TLS-terminating
     tunnels (Tailscale Funnel, Cloudflare Tunnel) where mTLS cannot work.
  2. Human panel — bearer token (BRIDGE_PANEL_TOKEN), for the owner's
     browser/phone. Separate credential from the agent's key.

Nothing owner-specific (keys, tokens, URLs) belongs in the git repo:
keys/*.pub, .env and audit.log are gitignored.

Endpoints (all JSON; /panel HTML is public, its API calls are authenticated):
  GET  /health            liveness + robot type + auth mode
  GET  /state             unified state (last commanded pose + raw daemon state)
  GET  /limits            clamp table
  POST /goto              {pitch, yaw, roll (deg), duration, interpolation}
  POST /preset/{name}     nod | shake | look_around | curious (background task)
  POST /motors            {mode: enabled|disabled|gravity_compensation}
  POST /estop             latching software e-stop (also cuts motors)
  POST /estop/reset       clear the latch
  ANY  /proxy/{subpath}   raw passthrough to the Mini daemon, e.g.
                          POST /proxy/move/goto == POST <daemon>/api/move/goto
  GET  /camera/snapshot   JPEG frame from the robot camera (?width=px)
  POST /mic/record        {seconds} -> mono 16kHz WAV of mic audio
  POST /speaker/play      raw audio bytes in body (?filename=, ?wait_seconds=)
                          -> played on the robot's speakers
  GET  /sense/doa         mic-array direction of arrival
  GET  /panel             browser control panel
"""

import argparse
import base64
import hashlib
import json
import math
import os
import secrets
import sys
import tempfile
import time
from dataclasses import dataclass

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from media import get_media, media_status

# --------------------------------------------------------------------------
# Safety limits (degrees) — from Pollen Robotics' published safety table.
# --------------------------------------------------------------------------
LIMITS = {
    "head_pitch": (-40.0, 40.0),
    "head_roll": (-40.0, 40.0),
    "head_yaw": (-180.0, 180.0),
    "body_yaw": (-160.0, 160.0),
    "antenna": (-28.0, 28.0),
    "duration": (0.2, 10.0),
}
INTERPOLATIONS = ("linear", "minjerk", "ease_in_out", "cartoon")


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def clamp_deg(value: float, name: str) -> float:
    lo, hi = LIMITS[name]
    return clamp(value, lo, hi)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def load_dotenv(path: str = ".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Config:
    port: int = 8765
    mini_url: str | None = None
    reachy_host: str | None = None
    mock: bool = False
    panel_token: str | None = None
    no_auth: bool = False
    clients_dir: str = "keys"
    audit_log: str = "audit.log"
    auth_window: int = 120


CONFIG = Config()
CLIENTS: dict[str, Ed25519PublicKey] = {}
NONCES: dict[str, float] = {}  # nonce -> expiry
ESTOP = {"latched": False}
LAST_COMMAND: dict = {"pitch": 0.0, "yaw": 0.0, "roll": 0.0,
                      "duration": 1.5, "interpolation": "minjerk",
                      "at": None}


def load_clients(directory: str) -> dict[str, Ed25519PublicKey]:
    """Load keys/<name>.pub -> {name: Ed25519PublicKey}. Accepts OpenSSH
    one-liners and PEM blocks."""
    clients: dict[str, Ed25519PublicKey] = {}
    if not os.path.isdir(directory):
        return clients
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".pub"):
            continue
        name = fname[: -len(".pub")]
        with open(os.path.join(directory, fname), "rb") as f:
            data = f.read().strip()
        key = None
        for loader in (serialization.load_ssh_public_key,
                       serialization.load_pem_public_key):
            try:
                key = loader(data)
                break
            except Exception:  # noqa: BLE001 — try the next format
                continue
        if not isinstance(key, Ed25519PublicKey):
            print(f"[bridge] WARNING: keys/{fname} is not an Ed25519 key — ignored",
                  file=sys.stderr)
            continue
        clients[name] = key
    return clients


# --------------------------------------------------------------------------
# Robot adapters — one per robot flavour, same interface.
# --------------------------------------------------------------------------
class RobotAdapter:
    name = "unknown"

    def state(self) -> dict:
        raise NotImplementedError

    def goto(self, *, pitch: float, yaw: float, roll: float,
             duration: float, interpolation: str) -> dict:
        raise NotImplementedError

    def set_motors(self, mode: str) -> dict:
        raise NotImplementedError

    def proxy(self, method: str, subpath: str, body) -> dict:
        raise HTTPException(501, f"raw proxy not supported for {self.name}")


class MiniAdapter(RobotAdapter):
    """Reachy Mini — talks to the onboard daemon's REST API.

    Routes confirmed on the robot's Reachy Mini daemon 1.9.0:
      POST /api/move/goto          {head_pose:{x,y,z,roll,pitch,yaw (rad)},
                                    duration, interpolation}
      GET  /api/state/full
      POST /api/motors/set_mode/{mode}
    Body-yaw / antenna REST shapes are NOT verified — use /proxy with the
    daemon's live /docs (http://<daemon>:8000/docs) to discover them.
    """

    name = "reachy-mini"

    def __init__(self, base_url: str):
        base_url = base_url.rstrip("/")
        if base_url.endswith("/api"):
            base_url = base_url[: -len("/api")]
        self.base_url = base_url
        self.client = httpx.Client(base_url=base_url + "/api", timeout=10.0)
        r = self.client.get("/state/full")  # fail fast if daemon is down
        r.raise_for_status()

    def state(self) -> dict:
        r = self.client.get("/state/full")
        r.raise_for_status()
        return r.json()

    def goto(self, *, pitch, yaw, roll, duration, interpolation) -> dict:
        payload = {
            "head_pose": {
                "x": 0.0, "y": 0.0, "z": 0.0,
                "roll": math.radians(roll),
                "pitch": math.radians(pitch),
                "yaw": math.radians(yaw),
            },
            "duration": duration,
            "interpolation": interpolation,
        }
        r = self.client.post("/move/goto", json=payload)
        r.raise_for_status()
        return r.json()

    def set_motors(self, mode: str) -> dict:
        if mode not in ("enabled", "disabled", "gravity_compensation"):
            raise HTTPException(400, f"unknown motor mode: {mode}")
        r = self.client.post(f"/motors/set_mode/{mode}")
        r.raise_for_status()
        return r.json()

    def proxy(self, method: str, subpath: str, body) -> dict:
        r = self.client.request(method, "/" + subpath.lstrip("/"), json=body)
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = {"raw": r.text}
        return {"daemon_status": r.status_code, "daemon_response": data}


class ClassicAdapter(RobotAdapter):
    """Full-size Reachy (1/2/2023) via the classic `reachy-sdk` (gRPC).

    State readout is implemented against the documented SDK surface.
    Head goto is left unwired on purpose — motion calls differ between robot
    generations, so wire the exact call for YOUR robot in the marked spot
    rather than shipping a guess that moves hardware wrong.
    """

    name = "reachy-classic"

    def __init__(self, host: str):
        try:
            from reachy_sdk import ReachySDK  # type: ignore
        except ImportError:
            raise SystemExit("reachy-sdk is not installed. Run: pip install reachy-sdk")
        self.reachy = ReachySDK(host=host)

    def state(self) -> dict:
        joints = {name: getattr(j, "present_position", None)
                  for name, j in self.reachy.joints.items()}
        return {"joints_deg": joints}

    def goto(self, *, pitch, yaw, roll, duration, interpolation) -> dict:
        # ---- WIRE YOUR ROBOT HERE -------------------------------------
        # Example for a Reachy 2021 head (verify against YOUR SDK version):
        #   self.reachy.head.goto(...)
        # Until this is wired, motion is refused rather than guessed.
        # ---------------------------------------------------------------
        raise HTTPException(
            501,
            "head goto is not wired for reachy-classic in this bridge. "
            "Edit ClassicAdapter.goto() in bridge.py with the motion call "
            "from your reachy-sdk version, then restart.",
        )

    def set_motors(self, mode: str) -> dict:
        raise HTTPException(501, "motor modes not wired for reachy-classic yet.")


class MockAdapter(RobotAdapter):
    """Fake robot: test the API, panel, auth and Funnel with no hardware."""

    name = "mock"

    def __init__(self):
        self.pose = {"pitch": 0.0, "yaw": 0.0, "roll": 0.0}
        self.motors = "enabled"

    def state(self) -> dict:
        return {"mock": True, "pose_deg": dict(self.pose),
                "motor_mode": self.motors,
                "note": "no hardware attached — all motion is simulated"}

    def goto(self, *, pitch, yaw, roll, duration, interpolation) -> dict:
        self.pose.update(pitch=pitch, yaw=yaw, roll=roll)
        time.sleep(min(duration, 0.3))
        return {"mock": True, "moved_to_deg": dict(self.pose)}

    def set_motors(self, mode: str) -> dict:
        self.motors = mode
        return {"mock": True, "motor_mode": mode}

    def proxy(self, method: str, subpath: str, body) -> dict:
        return {"mock": True, "method": method, "subpath": subpath, "body": body}


ADAPTER: RobotAdapter = MockAdapter()  # replaced at startup

# --------------------------------------------------------------------------
# App + authentication
# --------------------------------------------------------------------------
app = FastAPI(title="reachy-bridge", version="0.3.0")


def _prune_nonces(now: float):
    for n, exp in list(NONCES.items()):
        if exp < now:
            del NONCES[n]


async def authenticate(request: Request) -> dict:
    """Two doors: Ed25519 request signature (agent API) or panel bearer token."""
    if CONFIG.no_auth:
        request.state.auth = {"via": "no-auth", "client": "anyone"}
        return request.state.auth

    # Door 1 — agent signature
    client_id = request.headers.get("x-bridge-client")
    if client_id:
        pubkey = CLIENTS.get(client_id)
        ts = request.headers.get("x-bridge-timestamp", "")
        nonce = request.headers.get("x-bridge-nonce", "")
        sig = request.headers.get("x-bridge-signature", "")
        if pubkey is None or not (ts and nonce and sig):
            raise HTTPException(401, "unknown client or missing signature headers")
        try:
            ts_int = int(ts)
        except ValueError:
            raise HTTPException(401, "bad timestamp")  # noqa: B904
        now = time.time()
        if abs(now - ts_int) > CONFIG.auth_window:
            raise HTTPException(401, "stale timestamp — check clock sync")
        _prune_nonces(now)
        if nonce in NONCES:
            raise HTTPException(401, "replayed nonce")
        body = await request.body()
        body_hash = hashlib.sha256(body).hexdigest()
        message = f"{request.method}\n{request.url.path}\n{ts}\n{nonce}\n{body_hash}".encode()
        try:
            pubkey.verify(base64.b64decode(sig), message)
        except (InvalidSignature, ValueError, base64.binascii.Error):
            raise HTTPException(401, "bad signature")  # noqa: B904
        NONCES[nonce] = now + CONFIG.auth_window * 2
        request.state.auth = {"via": "signature", "client": client_id}
        return request.state.auth

    # Door 2 — human panel token
    authz = request.headers.get("authorization", "")
    if CONFIG.panel_token and authz == f"Bearer {CONFIG.panel_token}":
        request.state.auth = {"via": "panel-token", "client": "panel"}
        return request.state.auth

    raise HTTPException(401, "missing credentials: sign the request or present the panel token")


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.url.path != "/panel" and CONFIG.audit_log:
        info = getattr(request.state, "auth", None) or {
            "via": "rejected", "client": request.headers.get("x-bridge-client", "?")}
        entry = {"ts": time.time(), "via": info.get("via"),
                 "client": info.get("client"), "method": request.method,
                 "path": request.url.path, "status": response.status_code}
        try:
            with open(CONFIG.audit_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass
    return response


def require_live():
    if ESTOP["latched"]:
        raise HTTPException(423, "e-stop latched — POST /estop/reset to clear it")


class GotoRequest(BaseModel):
    pitch: float = 0.0
    yaw: float = 0.0
    roll: float = 0.0
    duration: float = Field(default=1.5, ge=0.2, le=10.0)
    interpolation: str = "minjerk"


class MotorsRequest(BaseModel):
    mode: str


@app.get("/health")
async def health(_=Depends(authenticate)):
    return {"ok": True, "robot": ADAPTER.name,
            "estop_latched": ESTOP["latched"],
            "signature_auth": not CONFIG.no_auth,
            "registered_clients": sorted(CLIENTS.keys()),
            "media": media_status(CONFIG.mock),
            "time": time.time()}


@app.get("/limits")
async def limits(_=Depends(authenticate)):
    return {"limits_deg": LIMITS, "interpolations": list(INTERPOLATIONS)}


@app.get("/state")
async def state(_=Depends(authenticate)):
    try:
        daemon = ADAPTER.state()
    except Exception as e:  # noqa: BLE001 — surface daemon errors as data
        daemon = {"error": f"{type(e).__name__}: {e}"}
    return {"robot": ADAPTER.name, "estop_latched": ESTOP["latched"],
            "last_command": LAST_COMMAND, "daemon": daemon}


@app.post("/goto")
async def goto(req: GotoRequest, _=Depends(authenticate)):
    require_live()
    pitch, yaw, roll = (clamp_deg(req.pitch, "head_pitch"),
                        clamp_deg(req.yaw, "head_yaw"),
                        clamp_deg(req.roll, "head_roll"))
    interp = req.interpolation if req.interpolation in INTERPOLATIONS else "minjerk"
    try:
        daemon = ADAPTER.goto(pitch=pitch, yaw=yaw, roll=roll,
                              duration=req.duration, interpolation=interp)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"robot daemon error: {type(e).__name__}: {e}")
    LAST_COMMAND.update(pitch=pitch, yaw=yaw, roll=roll, duration=req.duration,
                        interpolation=interp, at=time.time())
    return {"ok": True,
            "commanded_deg": {"pitch": pitch, "yaw": yaw, "roll": roll},
            "clamped": [pitch != req.pitch, yaw != req.yaw, roll != req.roll],
            "daemon": daemon}


def _run_preset(name: str):
    seq: list[tuple[float, float, float, float]] = []
    if name == "nod":
        seq = [(20, 0, 0, 0.5), (-12, 0, 0, 0.5)] * 2 + [(0, 0, 0, 0.6)]
    elif name == "shake":
        seq = [(0, 28, 0, 0.45), (0, -28, 0, 0.45)] * 2 + [(0, 0, 0, 0.6)]
    elif name == "look_around":
        seq = [(0, -55, 0, 0.9), (0, 55, 0, 1.4), (0, 0, 0, 0.9)]
    elif name == "curious":
        seq = [(0, 0, 14, 0.7), (-8, 0, 14, 0.5), (0, 0, 0, 0.7)]
    for pitch, yaw, roll, dur in seq:
        if ESTOP["latched"]:
            break
        try:
            ADAPTER.goto(pitch=pitch, yaw=yaw, roll=roll,
                         duration=dur, interpolation="minjerk")
        except Exception:  # noqa: BLE001 — a preset never kills the server
            break
        time.sleep(dur + 0.1)


@app.post("/preset/{name}")
async def preset(name: str, tasks: BackgroundTasks, _=Depends(authenticate)):
    require_live()
    if name not in ("nod", "shake", "look_around", "curious"):
        raise HTTPException(400, f"unknown preset: {name}")
    tasks.add_task(_run_preset, name)
    return {"ok": True, "preset": name, "status": "started"}


@app.post("/motors")
async def motors(req: MotorsRequest, _=Depends(authenticate)):
    if ESTOP["latched"] and req.mode == "enabled":
        raise HTTPException(423, "e-stop latched — reset it before enabling motors")
    try:
        daemon = ADAPTER.set_motors(req.mode)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"robot daemon error: {type(e).__name__}: {e}")
    return {"ok": True, "mode": req.mode, "daemon": daemon}


@app.post("/estop")
async def estop(_=Depends(authenticate)):
    ESTOP["latched"] = True
    try:
        ADAPTER.set_motors("disabled")
    except Exception:  # noqa: BLE001 — latch holds even if the daemon is down
        pass
    return {"ok": True, "estop_latched": True}


@app.post("/estop/reset")
async def estop_reset(_=Depends(authenticate)):
    ESTOP["latched"] = False
    return {"ok": True, "estop_latched": False}


@app.api_route("/proxy/{subpath:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(subpath: str, request: Request, _=Depends(authenticate)):
    require_live()
    body = None
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = None
    try:
        return ADAPTER.proxy(request.method, subpath, body)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"proxy error: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------
# Media — camera / mic / speaker / direction-of-arrival.
# Sync `def` endpoints run in FastAPI's threadpool so GStreamer blocking
# calls and multi-second recordings never stall the event loop.
# --------------------------------------------------------------------------
class RecordRequest(BaseModel):
    seconds: float = Field(default=5.0, ge=1.0, le=30.0)


@app.get("/camera/snapshot")
def camera_snapshot(width: int | None = None, _=Depends(authenticate)):
    if width is not None and not (80 <= width <= 1920):
        raise HTTPException(400, "width must be between 80 and 1920")
    jpeg = get_media(CONFIG.mock).snapshot_jpeg(width)
    return Response(content=jpeg, media_type="image/jpeg")


@app.post("/mic/record")
def mic_record(req: RecordRequest, _=Depends(authenticate)):
    wav = get_media(CONFIG.mock).record(req.seconds)
    return Response(content=wav, media_type="audio/wav",
                    headers={"Content-Disposition":
                             'attachment; filename="reachy-mic.wav"'})


_PLAY_TMPDIR = os.path.join(tempfile.gettempdir(), "reachy-bridge-play")


def _clean_play_tmpdir():
    """Delete played files older than an hour. play_sound() reads the file
    asynchronously, so we can't unlink right after the call returns."""
    try:
        os.makedirs(_PLAY_TMPDIR, exist_ok=True)
        now = time.time()
        for fname in os.listdir(_PLAY_TMPDIR):
            p = os.path.join(_PLAY_TMPDIR, fname)
            if now - os.path.getmtime(p) > 3600:
                os.unlink(p)
    except OSError:
        pass


@app.post("/speaker/play")
async def speaker_play(request: Request,
                       wait_seconds: float = 0.0,
                       filename: str = "audio.wav",
                       _=Depends(authenticate)):
    """Body = raw audio bytes (Content-Type: audio/wav, audio/mpeg, ...).
    Multipart is deliberately avoided: the auth layer already consumes the
    request stream for signature verification, so form parsing would fail."""
    if not (0.0 <= wait_seconds <= 120.0):
        raise HTTPException(400, "wait_seconds must be between 0 and 120")
    suffix = os.path.splitext(filename or "")[1].lower() or ".wav"
    if suffix not in (".wav", ".mp3", ".ogg", ".flac"):
        raise HTTPException(400, f"unsupported audio type: {suffix or '?'} "
                                 "(wav, mp3, ogg, flac)")
    body = await request.body()
    if not body:
        raise HTTPException(400, "empty audio body")
    if len(body) > 20 * 1024 * 1024:
        raise HTTPException(413, "audio file too large (20 MB max)")
    _clean_play_tmpdir()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix,
                                      dir=_PLAY_TMPDIR)
    try:
        with open(tmp.name, "wb") as f:
            f.write(body)
        return get_media(CONFIG.mock).play(tmp.name, wait_seconds)
    finally:
        tmp.close()


@app.get("/sense/doa")
def sense_doa(_=Depends(authenticate)):
    res = get_media(CONFIG.mock).doa()
    if res is None:
        raise HTTPException(502, "direction-of-arrival unavailable")
    return res


@app.get("/panel", response_class=HTMLResponse)
async def panel_page():
    """The control UI itself is public — it's useless without the panel token,
    which every API call it makes must carry."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "panel", "panel.html"), encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------
def detect_adapter(cfg: Config) -> RobotAdapter:
    if cfg.mock:
        print("[bridge] using MOCK robot (no hardware)")
        return MockAdapter()
    if cfg.mini_url:
        print(f"[bridge] Reachy Mini daemon: {cfg.mini_url}")
        return MiniAdapter(cfg.mini_url)
    for candidate in ("http://reachy-mini.local:8000/api",
                      "http://localhost:8000/api"):
        try:
            print(f"[bridge] probing {candidate} ...")
            return MiniAdapter(candidate)
        except Exception as e:  # noqa: BLE001
            print(f"[bridge]   no: {type(e).__name__}")
    if cfg.reachy_host:
        print(f"[bridge] full-size Reachy via reachy-sdk at {cfg.reachy_host}")
        return ClassicAdapter(cfg.reachy_host)
    raise SystemExit(
        "No robot found.\n"
        "  Reachy Mini : set BRIDGE_MINI_URL=http://<robot-ip>:8000/api in .env\n"
        "  Full-size   : set BRIDGE_REACHY_HOST=<robot-ip> (needs: pip install reachy-sdk)\n"
        "  Testing     : set BRIDGE_MOCK=1"
    )


def main():
    global ADAPTER, CLIENTS
    load_dotenv()
    p = argparse.ArgumentParser(description="reachy-bridge: HTTPS body for Reachy")
    p.add_argument("--mini-url")
    p.add_argument("--reachy-host")
    p.add_argument("--mock", action="store_true")
    p.add_argument("--port", type=int)
    p.add_argument("--panel-token")
    p.add_argument("--clients-dir")
    p.add_argument("--no-auth", action="store_true")
    args = p.parse_args()

    env = os.environ.get
    cfg = Config(
        port=args.port or int(env("BRIDGE_PORT", "8765")),
        mini_url=args.mini_url or env("BRIDGE_MINI_URL"),
        reachy_host=args.reachy_host or env("BRIDGE_REACHY_HOST"),
        mock=args.mock or env("BRIDGE_MOCK", "") == "1",
        panel_token=args.panel_token or env("BRIDGE_PANEL_TOKEN"),
        no_auth=args.no_auth or env("BRIDGE_NO_AUTH", "") == "1",
        clients_dir=args.clients_dir or env("BRIDGE_CLIENTS_DIR", "keys"),
        audit_log=env("BRIDGE_AUDIT_LOG", "audit.log"),
        auth_window=int(env("BRIDGE_AUTH_WINDOW", "120")),
    )
    globals()["CONFIG"] = cfg

    if not cfg.no_auth and not cfg.panel_token:
        cfg.panel_token = secrets.token_hex(16)
        print("[bridge] generated panel token (set BRIDGE_PANEL_TOKEN in .env to fix it):")
        print(f"[bridge]   {cfg.panel_token}")

    CLIENTS = load_clients(cfg.clients_dir)

    ADAPTER = detect_adapter(cfg)

    print(f"[bridge] robot: {ADAPTER.name}")
    print(f"[bridge] agent clients: {sorted(CLIENTS.keys()) or '(none — signed API calls will 401)'}")
    if cfg.no_auth:
        print("[bridge] WARNING: --no-auth — EVERYONE on the network can drive the robot")
    else:
        print(f"[bridge] auth: Ed25519 signatures (window {cfg.auth_window}s) + panel token")
    print(f"[bridge] listening on http://127.0.0.1:{cfg.port}")
    print(f"[bridge] panel:      http://127.0.0.1:{cfg.port}/panel")
    print(f"[bridge] expose with: tailscale funnel {cfg.port}")
    print(f"[bridge] audit log:  {cfg.audit_log}")
    print("[bridge] e-stop: POST /estop  (latches; POST /estop/reset clears)")

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
