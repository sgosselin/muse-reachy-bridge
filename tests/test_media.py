import io
import wave

import pytest
from PIL import Image


def test_snapshot_is_a_resized_jpeg(api):
    response = api.signed("GET", "/camera/snapshot?width=320")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(response.content)) as frame:
        assert frame.format == "JPEG"
        assert frame.size == (320, 240)


@pytest.mark.parametrize("width", [0, 79, 1921])
def test_invalid_snapshot_width_is_rejected(api, width):
    assert api.signed("GET", f"/camera/snapshot?width={width}").status_code == 400


def test_recording_is_mono_16khz_wav(api):
    response = api.signed("POST", "/mic/record", {"seconds": 1})
    assert response.status_code == 200
    with wave.open(io.BytesIO(response.content), "rb") as recording:
        assert recording.getnchannels() == 1
        assert recording.getframerate() == 16000
        assert recording.getsampwidth() == 2
        assert recording.getnframes() == 16000


@pytest.mark.parametrize("seconds", [0, 31])
def test_invalid_recording_duration_is_rejected(api, seconds):
    assert api.signed("POST", "/mic/record", {"seconds": seconds}).status_code == 422


def test_recorded_audio_can_be_sent_for_simulated_playback(api):
    wav = api.signed("POST", "/mic/record", {"seconds": 1}).content
    response = api.signed("POST", "/speaker/play?filename=test.wav", body=wav)
    assert response.status_code == 200
    assert response.json() == {"status": "done", "mock": True}


def test_empty_audio_is_rejected(api):
    assert api.signed("POST", "/speaker/play", body=b"").status_code == 400
