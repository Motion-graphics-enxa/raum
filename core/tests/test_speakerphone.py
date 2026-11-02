"""A-03 ``raum speakerphone`` without audio hardware or LiveKit: a fake ``sounddevice``."""

from __future__ import annotations

import asyncio
import builtins
import json
import wave
from types import SimpleNamespace

import numpy as np
import pytest
from livekit import api

from raum.agent import speakerphone as sp
from raum.config import load_config

pytestmark = pytest.mark.filterwarnings("ignore:The HMAC key")

JABRA = "Jabra SPEAK 410 USB: Audio (hw:2,0)"


def dev(name, i, o, rate=48000):
    return {"name": name, "max_input_channels": i, "max_output_channels": o, "default_samplerate": rate,
            "hostapi": 0}


class FakeSD:
    def __init__(self, devices, default=(0, 1), rates=(48000,), channels=(1, 2)):
        self._devices = devices
        self.default = SimpleNamespace(device=list(default))
        self.rates = rates
        self.channels = channels

    def query_devices(self, idx=None):
        return self._devices if idx is None else self._devices[idx]

    def query_hostapis(self):
        return [{"name": "ALSA"}]

    def check_input_settings(self, device, channels, dtype, samplerate):
        if samplerate not in self.rates or channels not in self.channels:
            raise ValueError(f"Invalid sample rate/channels {samplerate}/{channels}")

    check_output_settings = check_input_settings


@pytest.fixture
def sd():
    return FakeSD([dev("pipewire", 64, 64), dev(JABRA, 1, 2, 16000), dev("HDMI 0", 0, 8)], default=(0, 0))


# ---------------------------------------------------------------- no PortAudio: exit 3, clear message


def _import_failing(monkeypatch, exc):
    real = builtins.__import__

    def fake(name, *a, **kw):
        if name == "sounddevice":
            raise exc
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)


def test_missing_package_and_missing_portaudio(monkeypatch):
    _import_failing(monkeypatch, ImportError("No module named 'sounddevice'"))
    with pytest.raises(sp.AudioUnavailable, match="--extra speakerphone"):
        sp.load_sounddevice()
    _import_failing(monkeypatch, OSError("PortAudio library not found"))
    with pytest.raises(sp.AudioUnavailable, match="libportaudio2"):
        sp.load_sounddevice()


def test_cli_exit_codes(monkeypatch, capsys):
    _import_failing(monkeypatch, OSError("PortAudio library not found"))
    assert sp.run(["--list-devices"]) == 3
    assert sp.run(["--room", "sz-4821"]) == 3  # microphone needed: no LiveKit connection is attempted
    assert "no audio: PortAudio not found" in capsys.readouterr().out
    assert sp.run(["--room", "zz"]) == 2
    monkeypatch.delenv("RAUM_SPEAKERPHONE_ROOM", raising=False)
    assert sp.run([]) == 2


def test_cli_no_such_device(monkeypatch, sd, capsys):
    monkeypatch.setattr(sp, "load_sounddevice", lambda: sd)
    assert sp.run(["--room", "sz-4821", "--device", "Konftel"]) == 3
    assert "no input device matching 'Konftel'" in capsys.readouterr().out


# ---------------------------------------------------------------- devices and formats


def test_list_devices(sd):
    out = sp.list_devices(sd)
    assert "Jabra SPEAK 410" in out and "<>  0" in out  # default in and out
    with pytest.raises(sp.AudioUnavailable, match="no audio devices"):
        sp.list_devices(FakeSD([]))


def test_pick_device(sd):
    assert sp.pick_device(sd, "jabra", "input") == 1 and sp.pick_device(sd, "JABRA", "output") == 1
    assert sp.pick_device(sd, "2", "output") == 2
    assert sp.pick_device(sd, None, "input") == 0
    with pytest.raises(sp.AudioUnavailable, match="no input channels"):
        sp.pick_device(sd, "2", "input")
    with pytest.raises(sp.AudioUnavailable, match="no input device matching 'hdmi'"):
        sp.pick_device(sd, "hdmi", "input")
    with pytest.raises(sp.AudioUnavailable, match="no audio device 9"):
        sp.pick_device(sd, "9", "input")
    with pytest.raises(sp.AudioUnavailable, match="no default output device"):
        sp.pick_device(FakeSD([dev("x", 1, 0)], default=(0, -1)), None, "output")


def test_pick_format():
    assert sp.pick_format(FakeSD([dev("a", 1, 1)]), 0, "input") == (48000, 1)
    assert sp.pick_format(FakeSD([dev(JABRA, 1, 2, 16000)], rates=(16000,)), 0, "input") == (16000, 1)
    assert sp.pick_format(FakeSD([dev("st", 2, 2)], channels=(2,)), 0, "output") == (48000, 2)
    with pytest.raises(sp.AudioUnavailable, match="takes no mono/stereo int16 format"):
        sp.pick_format(FakeSD([dev("odd", 1, 1)], rates=(8000,)), 0, "input")


# ---------------------------------------------------------------- playback jitter buffer


def test_speaker_jitter_buffer(sd):
    s = sp.SpeakerSink(sd, "pipewire")  # 48 kHz mono; prebuffer 60 ms = 2880 samples
    out = np.zeros((480, 1), dtype=np.int16)
    s.write(np.full(1440, 7, dtype=np.int16))
    s._cb(out, 480, None, None)
    assert not out.any()  # 30 ms queued: still priming, silence
    s.write(np.full(1440, 7, dtype=np.int16))
    s._cb(out, 480, None, None)
    assert (out == 7).all()
    for _ in range(5):
        s._cb(out, 480, None, None)
    s._cb(out, 480, None, None)  # 2880 written, 3360 asked: underrun pads with silence
    assert not out.any() and s._primed is False
    s.write(np.ones(48000 * 5, dtype=np.int16))  # 5 s while max is 3 s: the oldest voice is dropped
    assert s._n <= s.max


def test_speaker_resamples_and_fills_stereo():
    s = sp.SpeakerSink(FakeSD([dev(JABRA, 1, 2, 16000)], default=(0, 0), rates=(16000,), channels=(2,)), None)
    assert (s.rate, s.channels) == (16000, 2)
    for _ in range(10):
        s.write(np.full(480, 1000, dtype=np.int16))  # 10 ms at 48 kHz -> 160 samples at 16 kHz
    out = np.zeros((160, 2), dtype=np.int16)
    s._cb(out, 160, None, None)
    assert out[:, 0].any() and (out[:, 0] == out[:, 1]).all()


# ---------------------------------------------------------------- microphone


async def test_mic_frames_downmix_resample_and_watchdog(monkeypatch):
    m = sp.MicSource(FakeSD([dev(JABRA, 2, 2, 16000)], default=(0, 0), rates=(16000,), channels=(2,)), None)
    assert (m.rate, m.channels) == (16000, 2)
    m._loop = asyncio.get_running_loop()
    for _ in range(6):  # 60 ms of stereo at 16 kHz
        m._push(np.tile(np.array([[1000, 3000]], dtype=np.int16), (160, 1)))
    gen = m.frames()
    f = await asyncio.wait_for(gen.__anext__(), 1)
    assert f.dtype == np.int16 and len(f) == sp.FRAME
    monkeypatch.setattr(sp, "WATCHDOG_S", 0.05)
    with pytest.raises(sp.AudioUnavailable, match="stopped delivering audio"):
        for _ in range(20):
            await asyncio.wait_for(gen.__anext__(), 1)


async def test_wav_source(tmp_path):
    p = tmp_path / "x.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.full(1600, 500, dtype=np.int16).tobytes())  # 100 ms
    src = sp.WavSource(str(p), repeat=1, gap=0.05)
    frames = []
    async for f in src.frames():
        frames.append(f)
        if len(frames) == 14:
            break
    assert all(len(f) == sp.FRAME for f in frames)
    assert frames[0].any() and not frames[-1].any()  # speech, then silence


# ---------------------------------------------------------------- token


def test_token_metadata_room_config_and_dispatch(monkeypatch):
    monkeypatch.delenv("RAUM_AGENT_NAME", raising=False)
    cfg = load_config()
    a = SimpleNamespace(role="b", lang="tr", identity="b-spk", room="sz-4821", api_key="devkey", api_secret="secret")
    claims = api.TokenVerifier("devkey", "secret").verify(sp.mint_token(cfg, a, "Patient (speakerphone)"))
    # no consent screen at a speakerphone: the standard voice, never a voice sample
    meta = {"role": "b", "lang": "tr", "device": "speakerphone", "path": "direct", "audio_out": "room",
            "voice": "standard"}
    assert claims.identity == "b-spk" and json.loads(claims.metadata) == meta and claims.attributes == meta
    assert claims.video.room == "sz-4821" and claims.video.can_publish and claims.video.can_subscribe
    assert claims.room_config.departure_timeout == cfg.network.departure_timeout_s
    assert not claims.room_config.agents
    monkeypatch.setenv("RAUM_AGENT_NAME", "raum-test")
    claims = api.TokenVerifier("devkey", "secret").verify(sp.mint_token(cfg, a, "x"))
    assert [d.agent_name for d in claims.room_config.agents] == ["raum-test"]
