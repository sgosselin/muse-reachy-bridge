import json
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import bridge


@pytest.mark.parametrize("path", ["/health", "/state", "/limits", "/camera/snapshot"])
def test_api_requires_credentials(api, path):
    assert api.client.get(path).status_code == 401


def test_panel_loads_without_credentials(api):
    response = api.client.get("/panel")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_signed_health_identifies_simulated_robot(api):
    response = api.signed("GET", "/health")
    assert response.status_code == 200
    assert response.json()["robot"] == "mock"
    assert response.json()["registered_clients"] == ["test-agent"]


@pytest.mark.parametrize("token,status", [("test-panel-token", 200), ("wrong", 401)])
def test_panel_token(api, token, status):
    response = api.client.get("/health", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == status


def test_replayed_request_is_rejected(api):
    headers = api.signer._signed_headers("GET", "/health", b"")
    assert api.client.get("/health", headers=headers).status_code == 200
    response = api.client.get("/health", headers=headers)
    assert response.status_code == 401
    assert "replayed" in response.json()["detail"]


def test_changed_body_is_rejected(api):
    original = b'{"yaw": 10}'
    headers = api.signer._signed_headers("POST", "/goto", original)
    response = api.client.post("/goto", content=b'{"yaw": 35}', headers=headers)
    assert response.status_code == 401
    assert bridge.ADAPTER.pose["yaw"] == 0


@pytest.mark.parametrize("method,path", [("GET", "/state"), ("POST", "/estop")])
def test_signature_is_bound_to_method_and_path(api, method, path):
    headers = api.signer._signed_headers("GET", "/health", b"")
    assert api.client.request(method, path, headers=headers).status_code == 401


def test_stale_signed_request_is_rejected(api, monkeypatch):
    old_time = time.time() - bridge.CONFIG.auth_window - 10
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: old_time)
        headers = api.signer._signed_headers("GET", "/health", b"")
    response = api.client.get("/health", headers=headers)
    assert response.status_code == 401
    assert "stale" in response.json()["detail"]


def test_unregistered_agent_is_rejected(api):
    headers = api.signer._signed_headers("GET", "/health", b"")
    headers["X-Bridge-Client"] = "unregistered"
    assert api.client.get("/health", headers=headers).status_code == 401


@pytest.mark.parametrize("encoding,format", [
    (serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH),
    (serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo),
])
def test_public_key_loading(api, tmp_path, encoding, format):
    keys = tmp_path / "public-keys"
    keys.mkdir()
    (keys / "test-agent.pub").write_bytes(api.private_key.public_key().public_bytes(
        encoding, format,
    ))
    (keys / "invalid.pub").write_text("not a public key")
    loaded = bridge.load_clients(str(keys))
    assert set(loaded) == {"test-agent"}
    message = b"verify loaded key"
    loaded["test-agent"].verify(api.private_key.sign(message), message)


def test_wrong_private_key_is_rejected(api):
    api.signer._priv = Ed25519PrivateKey.generate()
    assert api.signed("GET", "/health").status_code == 401


def test_audit_records_caller_and_rejection_without_credentials(api):
    api.signed("GET", "/health")
    api.client.get("/health")
    text = Path(bridge.CONFIG.audit_log).read_text()
    entries = [json.loads(line) for line in text.splitlines()]
    assert [(entry["client"], entry["status"]) for entry in entries] == [
        ("test-agent", 200), ("?", 401),
    ]
    assert "test-panel-token" not in text
    assert "X-Bridge-Signature" not in text
