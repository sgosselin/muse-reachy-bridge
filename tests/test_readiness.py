"""Known blockers, expressed as strict expected failures rather than hidden.

The API contract below is based on Pollen's upstream source, not a measurement
of the owner's robot. See docs/TESTING.md for sources and hardware validation.
"""

import sys

import httpx
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization

import bridge

pytestmark = pytest.mark.readiness


@pytest.mark.xfail(reason="Startup still defaults to clients/ while installers write keys/",
                   raises=AssertionError)
def test_startup_discovers_installer_key_directory(api, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in list(bridge.os.environ):
        if name.startswith("BRIDGE_"):
            monkeypatch.delenv(name)
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "test-agent.pub").write_bytes(api.private_key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH,
    ))
    monkeypatch.setattr(sys, "argv", ["bridge.py", "--mock", "--panel-token", "test-token"])
    monkeypatch.setattr(bridge, "detect_adapter", lambda cfg: bridge.MockAdapter())
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    bridge.main()
    assert "test-agent" in bridge.CLIENTS


@pytest.mark.xfail(reason="Raw daemon proxy does not check the e-stop latch",
                   raises=AssertionError)
def test_estop_blocks_proxy_motion(api, monkeypatch):
    forwarded = []
    monkeypatch.setattr(bridge.ADAPTER, "proxy",
                        lambda *args: forwarded.append(args) or {"ok": True})
    assert api.signed("POST", "/estop").status_code == 200
    response = api.signed("POST", "/proxy/move/goto", {
        "head_pose": {"yaw": 0.1}, "duration": 1,
    })
    assert response.status_code == 423
    assert forwarded == []


@pytest.fixture
def upstream_daemon(monkeypatch):
    seen = []

    def respond(request):
        seen.append((request.method, request.url.path))
        accepted = {
            ("GET", "/api/state/full-state"),
            ("POST", "/api/move/goto"),
            ("POST", "/api/motors/set_mode/disabled"),
        }
        return httpx.Response(200 if seen[-1] in accepted else 404, json={})

    client = httpx.Client(
        base_url="http://simulated-daemon/api", transport=httpx.MockTransport(respond),
    )
    with monkeypatch.context() as patch:
        patch.setattr(bridge.httpx, "Client", lambda **kwargs: client)
        adapter = bridge.MiniAdapter("http://simulated-daemon/api")
    try:
        yield adapter, seen
    finally:
        client.close()


@pytest.mark.xfail(reason="MiniAdapter calls /api/goto; upstream uses /api/move/goto",
                   raises=httpx.HTTPStatusError)
def test_motion_matches_upstream_daemon_route(upstream_daemon):
    adapter, seen = upstream_daemon
    adapter.goto(pitch=0, yaw=5, roll=0, duration=1, interpolation="minjerk")
    assert seen[-1] == ("POST", "/api/move/goto")


@pytest.mark.xfail(reason="MiniAdapter uses /set-mode JSON; upstream uses /set_mode/{mode}",
                   raises=httpx.HTTPStatusError)
def test_motor_disable_matches_upstream_daemon_route(upstream_daemon):
    adapter, seen = upstream_daemon
    adapter.set_motors("disabled")
    assert seen[-1] == ("POST", "/api/motors/set_mode/disabled")
