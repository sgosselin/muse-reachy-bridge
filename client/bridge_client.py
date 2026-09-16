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


def main():
    ap = argparse.ArgumentParser(description="signed reachy-bridge client")
    ap.add_argument("--url", required=True, help="bridge base URL")
    ap.add_argument("--key", default="~/.ssh/reachy_bridge_astro",
                    help="Ed25519 private key")
    ap.add_argument("--client", default="astro", help="client id in clients/")
    ap.add_argument("command", choices=["health", "state", "limits", "goto",
                                        "preset", "motors", "estop", "estop-reset"])
    ap.add_argument("--pitch", type=float, default=0.0)
    ap.add_argument("--yaw", type=float, default=0.0)
    ap.add_argument("--roll", type=float, default=0.0)
    ap.add_argument("--duration", type=float, default=1.5)
    ap.add_argument("--name", default="nod")
    ap.add_argument("--mode", default="disabled")
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
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
