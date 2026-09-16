#!/usr/bin/env python3
"""Media adapters: camera snapshots, mic recording, speaker playback, DoA.

Same pattern as the motion adapters in bridge.py — one interface, two
implementations:

  MiniMedia — real hardware through the `reachy_mini` SDK's MediaManager
      (LOCAL backend: camera frames via the daemon's local IPC endpoint,
      audio via GStreamer). The SDK is imported lazily; if it isn't
      installed the media endpoints answer 501 with a clear message.
  MockMedia — synthetic outputs for hardware-free testing.

API surface verified against reachy_mini 1.10.0
(reachy_mini/media/media_manager.py):
  MediaManager(backend=MediaBackend.DEFAULT)
      .get_frame_jpeg()      -> JPEG bytes | None
      .start_recording() / .get_audio_sample() -> float32 (n,2) stereo
      .stop_recording();      .get_input_audio_samplerate() -> 16000
      .play_sound(path)      fire-and-forget GStreamer playbin
      .get_DoA()             -> (angle_rad, speech_detected) | None
"""

import io
import math
import threading
import time
import wave

import numpy as np
from fastapi import HTTPException
from PIL import Image, ImageDraw

SAMPLE_RATE = 16000  # SDK input rate (audio_base.SAMPLE_RATE)


def _wav_bytes(pcm_mono: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    pcm16 = (np.clip(pcm_mono, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16.tobytes())
    return buf.getvalue()


class MediaAdapter:
    name = "unknown"

    def snapshot_jpeg(self, width: int | None = None) -> bytes:
        raise NotImplementedError

    def record(self, seconds: float) -> bytes:
        """Record `seconds` of mic audio; returns mono 16 kHz WAV bytes."""
        raise NotImplementedError

    def play(self, path: str, wait_seconds: float) -> dict:
        raise NotImplementedError

    def doa(self) -> dict | None:
        """Direction of arrival, or None if unavailable."""
        raise NotImplementedError


class MiniMedia(MediaAdapter):
    """Real Reachy Mini media via the reachy_mini SDK (LOCAL backend)."""

    name = "reachy-mini"

    def __init__(self):
        from reachy_mini.media.media_manager import (  # noqa: PLC0415
            MediaBackend, MediaManager)
        # May raise (no camera, no audio device, GStreamer trouble) —
        # get_media() converts that into a 501 with the reason.
        self._mm = MediaManager(backend=MediaBackend.DEFAULT,
                                log_level="WARNING")
        self._lock = threading.Lock()

    def snapshot_jpeg(self, width: int | None = None) -> bytes:
        with self._lock:
            jpeg = self._mm.get_frame_jpeg()
        if jpeg is None:
            raise HTTPException(502, "camera frame unavailable from daemon")
        if width:
            img = Image.open(io.BytesIO(jpeg))
            if img.width > width:
                img = img.resize((width, round(img.height * width / img.width)),
                                 Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=85)
                jpeg = buf.getvalue()
        return jpeg

    def record(self, seconds: float) -> bytes:
        chunks: list[np.ndarray] = []
        with self._lock:
            self._mm.start_recording()
            try:
                deadline = time.time() + seconds
                while time.time() < deadline:
                    s = self._mm.get_audio_sample()
                    if s is not None:
                        chunks.append(np.asarray(s, dtype=np.float32))
                    else:
                        time.sleep(0.02)
            finally:
                self._mm.stop_recording()
        if not chunks:
            raise HTTPException(502, "no audio captured from microphone")
        stereo = np.concatenate(chunks, axis=0)
        mono = stereo.mean(axis=1)
        want = int(seconds * SAMPLE_RATE)
        if len(mono) > want:
            mono = mono[:want]
        elif len(mono) < want:
            mono = np.pad(mono, (0, want - len(mono)))
        return _wav_bytes(mono)

    def play(self, path: str, wait_seconds: float) -> dict:
        with self._lock:
            self._mm.play_sound(path)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
            return {"status": "done", "waited_s": wait_seconds}
        return {"status": "playing"}

    def doa(self) -> dict | None:
        with self._lock:
            res = self._mm.get_DoA()
        if res is None:
            return None
        angle_rad, speech = res
        return {"angle_rad": float(angle_rad),
                "angle_deg": round(math.degrees(angle_rad), 1),
                "speech_detected": bool(speech)}


class MockMedia(MediaAdapter):
    """Synthetic media: a test-card JPEG, a sine WAV, instant playback."""

    name = "mock"

    def snapshot_jpeg(self, width: int | None = None) -> bytes:
        w, h = 640, 480
        img = Image.new("RGB", (w, h))
        px = img.load()
        for y in range(h):
            for x in range(w):
                px[x, y] = (x * 255 // w, y * 255 // h, 128)
        d = ImageDraw.Draw(img)
        d.text((16, 16), f"mock reachy-mini  {time.strftime('%H:%M:%S')}",
               fill=(255, 255, 255))
        d.ellipse([w // 2 - 60, h // 2 - 60, w // 2 + 60, h // 2 + 60],
                  outline=(255, 255, 255), width=4)
        if width and width < w:
            img = img.resize((width, round(h * width / w)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return buf.getvalue()

    def record(self, seconds: float) -> bytes:
        n = int(seconds * SAMPLE_RATE)
        t = np.arange(n) / SAMPLE_RATE
        mono = 0.3 * np.sin(2 * math.pi * 440 * t).astype(np.float32)
        return _wav_bytes(mono)

    def play(self, path: str, wait_seconds: float) -> dict:
        if wait_seconds > 0:
            time.sleep(min(wait_seconds, 2.0))
        return {"status": "done", "mock": True}

    def doa(self) -> dict | None:
        return {"angle_rad": 0.3, "angle_deg": 17.2,
                "speech_detected": False, "mock": True}


_MEDIA: MediaAdapter | None = None
_MEDIA_ERROR: Exception | None = None


def get_media(mock: bool) -> MediaAdapter:
    """Singleton media adapter. Raises 501 with the reason if the real
    backend can't initialise (e.g. reachy-mini SDK not installed)."""
    global _MEDIA, _MEDIA_ERROR
    if mock:
        return MockMedia()
    if _MEDIA is None and _MEDIA_ERROR is None:
        try:
            _MEDIA = MiniMedia()
        except Exception as e:  # noqa: BLE001 — surfaced as the 501 reason
            _MEDIA_ERROR = e
    if _MEDIA_ERROR is not None:
        raise HTTPException(
            501, f"media unavailable: {_MEDIA_ERROR} "
                 "(on the bridge host: pip install reachy-mini)")
    assert _MEDIA is not None
    return _MEDIA


def media_status(mock: bool) -> str:
    """One-word media readiness for /health — never initialises hardware."""
    if mock:
        return "mock"
    try:
        import reachy_mini  # noqa: F401, PLC0415
        return "sdk-installed"
    except ImportError:
        return "unavailable (pip install reachy-mini)"
