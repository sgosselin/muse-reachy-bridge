#!/usr/bin/env python3
"""
Signed client for reachy-bridge.

Proves agent identity with Ed25519 request signatures (see bridge.py
docstring for the scheme). stdlib-only apart from `cryptography`.

    from bridge_client import BridgeClient
    b = BridgeClient("https://reachy-bridge.tail12345.ts.net",
                     client_id="astro", key_path="~/.ssh/reachy_bridge_astro")
    b.goto(pitch=15, yaw=-30, duration=1.5)
    b.preset("nod")

Or as a smoke-test CLI:
    python bridge_client.py --url <url> --key <private-key> health
    python bridge_client.py --url <url> --key <private-key> goto --pitch 15 --yaw -30
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import serialization


class BridgeError(Exception):
    def __init__(self, status, body):
        super().__init__(f"bridge returned {status}: {body}")
        self.status = status
        self.body = body


class BridgeClient:
    def __init__(self, base_url: str, client_id: str = "astro",
                 key_path: str = "~/.ssh/reachy_bridge_astro"):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        with open(os.path.expanduser(key_path), "rb") as f:
            key_data = f.read()
        try:
            self._priv = serialization.load_pem_private_key(key_data, password=None)
        except ValueError:
            # OpenSSH format (ssh-keygen default)
            self._priv = serialization.load_ssh_private_key(key_data, password=None)

    def _signed_headers(self, method: str, path: str, body: bytes) -> dict:
        ts = str(int(time.time()))
        nonce = secrets.token_hex(16)
        body_hash = hashlib.sha256(body).hexdigest()
        message = f"{method}\n{path}\n{ts}\n{nonce}\n{body_hash}".encode()
        sig = base64.b64encode(self._priv.sign(message)).decode()
        return {
            "X-Bridge-Client": self.client_id,
            "X-Bridge-Timestamp": ts,
            "X-Bridge-Nonce": nonce,
            "X-Bridge-Signature": sig,
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, payload=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        req = urllib.request.Request(
            self.base_url + path,
            data=body if body or method in ("POST", "PUT", "PATCH") else None,
            method=method,
            headers=self._signed_headers(method, path, body),
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            raise BridgeError(e.code, e.read().decode()) from e
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {"raw": raw}

    def request_bytes(self, method: str, path: str, body: bytes = b"",
                      content_type: str = "application/json"):
        """Raw variant of request(): returns (bytes, content_type). The
        signature covers the exact body bytes, whatever the content type.
        Query strings are part of the URL but NOT of the signed path
        (the bridge signs request.url.path, which excludes the query)."""
        sign_path = path.split("?", 1)[0]
        headers = self._signed_headers(method, sign_path, body)
        headers["Content-Type"] = content_type
        req = urllib.request.Request(
            self.base_url + path,
            data=body if body or method in ("POST", "PUT", "PATCH") else None,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raise BridgeError(e.code, e.read().decode()) from e

    # -- convenience wrappers ------------------------------------------------
    def health(self): return self.request("GET", "/health")
    def state(self): return self.request("GET", "/state")
    def limits(self): return self.request("GET", "/limits")

    def goto(self, pitch=0.0, yaw=0.0, roll=0.0, duration=1.5,
             interpolation="minjerk"):
        return self.request("POST", "/goto", {
            "pitch": pitch, "yaw": yaw, "roll": roll,
            "duration": duration, "interpolation": interpolation})

    def preset(self, name): return self.request("POST", f"/preset/{name}")
    def motors(self, mode): return self.request("POST", "/motors", {"mode": mode})
    def estop(self): return self.request("POST", "/estop")
    def estop_reset(self): return self.request("POST", "/estop/reset")
    def proxy(self, method, subpath, payload=None):
        return self.request(method, "/proxy/" + subpath.lstrip("/"), payload)

    # -- media ---------------------------------------------------------------
    def snapshot(self, path, width=None):
        """Save a camera JPEG frame to `path`. Returns the saved path."""
        q = f"?width={int(width)}" if width else ""
        raw, _ = self.request_bytes("GET", "/camera/snapshot" + q)
        with open(os.path.expanduser(path), "wb") as f:
            f.write(raw)
        return os.path.expanduser(path)

    def record(self, path, seconds=5.0):
        """Record `seconds` of mic audio to `path` (WAV). Returns the path."""
        raw, _ = self.request_bytes(
            "POST", "/mic/record", json.dumps({"seconds": seconds}).encode())
        with open(os.path.expanduser(path), "wb") as f:
            f.write(raw)
        return os.path.expanduser(path)

    def play(self, audio_path, wait_seconds=0.0):
        """Play an audio file (wav/mp3/ogg/flac) on the robot's speakers.
        The raw file bytes are the request body (no multipart)."""
        import mimetypes
        from urllib.parse import quote
        audio_path = os.path.expanduser(audio_path)
        with open(audio_path, "rb") as f:
            audio = f.read()
        mime = mimetypes.guess_type(audio_path)[0] or "application/octet-stream"
        name = quote(os.path.basename(audio_path))
        raw, _ = self.request_bytes(
            "POST", f"/speaker/play?wait_seconds={wait_seconds}&filename={name}",
            audio, mime)
        return json.loads(raw.decode())

    def doa(self):
        return self.request("GET", "/sense/doa")


def main():
    ap = argparse.ArgumentParser(description="signed reachy-bridge client")
    ap.add_argument("--url", required=True, help="bridge base URL")
    ap.add_argument("--key", default="~/.ssh/reachy_bridge_astro",
                    help="Ed25519 private key")
    ap.add_argument("--client", default="astro", help="client id in keys/")
    ap.add_argument("command", choices=["health", "state", "limits", "goto",
                                        "preset", "motors", "estop", "estop-reset",
                                        "snapshot", "record", "play", "doa"])
    ap.add_argument("--pitch", type=float, default=0.0)
    ap.add_argument("--yaw", type=float, default=0.0)
    ap.add_argument("--roll", type=float, default=0.0)
    ap.add_argument("--duration", type=float, default=1.5)
    ap.add_argument("--name", default="nod")
    ap.add_argument("--mode", default="disabled")
    ap.add_argument("--out", default="snapshot.jpg",
                    help="output path for snapshot/record")
    ap.add_argument("--file", default="",
                    help="audio file to play on the robot")
    ap.add_argument("--seconds", type=float, default=5.0,
                    help="mic recording length")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="block until playback finished (speaker/play)")
    ap.add_argument("--width", type=int, default=0,
                    help="downscale snapshot to this width")
    a = ap.parse_args()

    b = BridgeClient(a.url, client_id=a.client, key_path=a.key)
    if a.command == "health":
        out = b.health()
    elif a.command == "state":
        out = b.state()
    elif a.command == "limits":
        out = b.limits()
    elif a.command == "goto":
        out = b.goto(pitch=a.pitch, yaw=a.yaw, roll=a.roll, duration=a.duration)
    elif a.command == "preset":
        out = b.preset(a.name)
    elif a.command == "motors":
        out = b.motors(a.mode)
    elif a.command == "estop":
        out = b.estop()
    elif a.command == "estop-reset":
        out = b.estop_reset()
    elif a.command == "snapshot":
        out = {"saved": b.snapshot(a.out, width=a.width or None)}
    elif a.command == "record":
        out = {"saved": b.record(a.out, seconds=a.seconds)}
    elif a.command == "play":
        if not a.file:
            ap.error("--file is required for play")
        out = b.play(a.file, wait_seconds=a.wait)
    elif a.command == "doa":
        out = b.doa()
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
