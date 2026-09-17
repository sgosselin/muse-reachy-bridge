import pytest

import bridge


def test_motion_is_clamped_before_reaching_adapter(api):
    response = api.signed("POST", "/goto", {
        "pitch": 90, "yaw": -300, "roll": -90, "duration": 0.2,
    })
    assert response.status_code == 200
    assert response.json()["commanded_deg"] == {"pitch": 40, "yaw": -180, "roll": -40}
    assert response.json()["clamped"] == [True, True, True]
    assert bridge.ADAPTER.pose == response.json()["commanded_deg"]


@pytest.mark.parametrize("duration", [0, 0.1, 11])
def test_invalid_motion_duration_is_rejected(api, duration):
    assert api.signed("POST", "/goto", {"duration": duration}).status_code == 422
    assert bridge.LAST_COMMAND["at"] is None


def test_estop_disables_motors_and_blocks_motion_until_reset(api):
    assert api.signed("POST", "/estop").status_code == 200
    assert bridge.ADAPTER.motors == "disabled"
    assert api.signed("POST", "/goto", {"yaw": 5}).status_code == 423
    assert api.signed("POST", "/preset/nod").status_code == 423
    assert api.signed("POST", "/motors", {"mode": "enabled"}).status_code == 423
    assert api.signed("POST", "/estop/reset").status_code == 200
    assert bridge.ADAPTER.motors == "disabled"
    assert api.signed("POST", "/motors", {"mode": "enabled"}).status_code == 200
    assert api.signed("POST", "/goto", {"yaw": 5, "duration": 0.2}).status_code == 200


def test_daemon_motion_failure_is_reported(api, monkeypatch):
    def offline(**kwargs):
        raise ConnectionError("simulated daemon outage")

    monkeypatch.setattr(bridge.ADAPTER, "goto", offline)
    response = api.signed("POST", "/goto", {"yaw": 5})
    assert response.status_code == 502
    assert bridge.LAST_COMMAND["at"] is None


def test_unknown_preset_is_rejected(api):
    assert api.signed("POST", "/preset/not-a-preset").status_code == 400
