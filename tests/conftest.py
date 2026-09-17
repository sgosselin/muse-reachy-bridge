"""Isolated bridge: ephemeral credentials, simulated hardware, no network."""

import json
import socket
from dataclasses import dataclass

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import bridge
import media
from client.bridge_client import BridgeClient


@pytest.fixture(autouse=True)
def prevent_hardware_access(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must use simulated hardware and in-process HTTP")

    monkeypatch.setattr(bridge, "detect_adapter", forbidden)
    monkeypatch.setattr(media, "MiniMedia", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


@dataclass
class BridgeHarness:
    client: TestClient
    signer: BridgeClient
    private_key: Ed25519PrivateKey

    def signed(self, method, path, payload=None, *, body=None):
        if body is None:
            body = json.dumps(payload).encode() if payload is not None else b""
        headers = self.signer._signed_headers(method, path.split("?", 1)[0], body)
        return self.client.request(method, path, content=body, headers=headers)


@pytest.fixture
def api(monkeypatch, tmp_path):
    private_key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "test-agent-private.pem"
    key_path.write_bytes(private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    key_path.chmod(0o600)
    monkeypatch.setattr(bridge, "CONFIG", bridge.Config(
        mock=True,
        panel_token="test-panel-token",
        clients_dir=str(tmp_path / "keys"),
        audit_log=str(tmp_path / "audit.log"),
    ))
    monkeypatch.setattr(bridge, "CLIENTS", {"test-agent": private_key.public_key()})
    monkeypatch.setattr(bridge, "NONCES", {})
    monkeypatch.setattr(bridge, "ESTOP", {"latched": False})
    monkeypatch.setattr(bridge, "LAST_COMMAND", dict(bridge.LAST_COMMAND))
    monkeypatch.setattr(bridge, "ADAPTER", bridge.MockAdapter())
    monkeypatch.setattr(bridge, "_PLAY_TMPDIR", str(tmp_path / "playback"))
    monkeypatch.setattr(media, "_MEDIA", None)
    monkeypatch.setattr(media, "_MEDIA_ERROR", None)
    signer = BridgeClient("http://testserver", "test-agent", str(key_path))
    with TestClient(bridge.app) as client:
        yield BridgeHarness(client, signer, private_key)
