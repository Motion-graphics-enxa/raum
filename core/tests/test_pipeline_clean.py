"""Streaming noise cleaning (raum.pipeline.clean): alignment with the raw 16 kHz path, context, the
utterance swap, the realtime gate, and one model per process."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import numpy as np
import pytest
import soxr

from raum.pipeline import denoise as denoise_mod
from raum.pipeline.clean import IN_RATE, Cleaner, RealtimeGate, cleaner_for


class Fake:
    """A denoiser stand-in: ``gain`` x input, optional per-call delay, records input sizes."""

    name = "fake"
    active = True

    def __init__(self, gain: float = 1.0, delay_s: float = 0.0) -> None:
        self.gain = gain
        self.delay_s = delay_s
        self.sizes: list[int] = []
        self.threads: set[str] = set()

    def process(self, x: np.ndarray, sr: int) -> np.ndarray:
        assert sr == IN_RATE
        self.sizes.append(x.size)
        self.threads.add(threading.current_thread().name)
        if self.delay_s:
            time.sleep(self.delay_s)
        return x * self.gain


def speech(seconds: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(IN_RATE * seconds)) / IN_RATE
    return (0.3 * np.sin(2 * np.pi * 220 * t) + 0.05 * rng.standard_normal(t.size)).astype(np.float32)


async def feed(cs, x48: np.ndarray, frame: int = 480) -> np.ndarray:
    """Push 10 ms frames like the worker does; returns the raw path's 16 kHz audio."""
    rs = soxr.ResampleStream(IN_RATE, 16000, 1, dtype="float32")
    raw = []
    for i in range(0, x48.size, frame):
        f = x48[i : i + frame]
        cs.push(f)
        raw.append(rs.resample_chunk(f))
        await asyncio.sleep(0)
    for _ in range(200):
        if cs._task is None or cs._task.done():
            break
        await asyncio.sleep(0.01)
    return np.concatenate(raw)


async def test_clean_index_matches_the_raw_path():
    c = Cleaner(Fake(gain=1.0))
    cs = c.stream()
    raw16 = await feed(cs, speech(2.0))
    assert cs.end > 16000 * 1.5  # all whole blocks cleaned
    s, e = 4000, cs.end - 100
    np.testing.assert_allclose(cs.slice(s, e), raw16[s:e], atol=2e-3)


async def test_context_is_processed_and_dropped():
    fake = Fake(gain=0.5)
    c = Cleaner(fake, {"block_ms": 200, "context_ms": 100})
    cs = c.stream()
    raw16 = await feed(cs, speech(1.0, seed=1))
    assert fake.sizes[0] == 9600 and set(fake.sizes[1:]) == {9600 + 4800}
    s, e = 3200, cs.end - 50
    np.testing.assert_allclose(cs.slice(s, e), 0.5 * raw16[s:e], atol=2e-3)
    assert all(n.startswith("raum-denoise") for n in fake.threads)  # never on the event loop


async def test_cleaned_swaps_what_is_ready_and_keeps_the_raw_tail():
    c = Cleaner(Fake(gain=0.0))  # "cleans" to silence, so swapped samples are easy to see
    cs = c.stream()
    raw16 = await feed(cs, speech(1.1, seed=2))  # 5 whole blocks + 100 ms the cleaner has not had yet
    end_index = raw16.size  # the utterance runs to the newest raw sample (VAD samples_index)
    utt = raw16[end_index - 12000 : end_index]
    out = cs.cleaned(utt, end_index)
    ready = cs.end - (end_index - 12000)
    assert 0 < ready < utt.size
    assert np.all(out[:ready] == 0.0)
    np.testing.assert_array_equal(out[ready:], utt[ready:])  # not cleaned yet: raw, no waiting
    assert cs.cleaned(utt, end_index + 10**7) is utt  # nothing kept that far back: raw unchanged


async def test_ring_keeps_only_keep_s():
    c = Cleaner(Fake(), {"keep_s": 0.5})
    cs = c.stream()
    await feed(cs, speech(1.4, seed=3))
    assert cs.slice(0, 1000).size == 0
    assert cs.slice(cs.end - 4000, cs.end).size == 4000


async def test_gate_trips_on_a_slow_model_and_logs_once(caplog):
    fake = Fake(delay_s=0.08)  # 80 ms per 100 ms block: realtime factor 0.8
    c = Cleaner(fake, {"block_ms": 100, "context_ms": 0, "max_lag_s": 5})
    cs = c.stream()
    with caplog.at_level(logging.WARNING, logger="raum.pipeline.clean"):
        await feed(cs, speech(0.8, seed=4))
    assert c.gate.tripped and "realtime factor" in c.gate.reason
    assert not c.usable and not cs.active
    n = len(fake.sizes)
    cs.push(speech(0.5))
    assert len(fake.sizes) == n  # off: pushes are dropped
    assert sum("noise cleaning off" in r.message for r in caplog.records) == 1
    raw = np.ones(1600, dtype=np.float32)
    assert cs.cleaned(raw, cs.end + 10**6) is raw


async def test_gate_trips_when_the_worker_falls_behind():
    started = threading.Event()
    release = threading.Event()

    class Stuck(Fake):
        def process(self, x, sr):
            started.set()
            release.wait(2.0)
            return x

    c = Cleaner(Stuck(), {"max_lag_s": 0.5})
    cs = c.stream()
    for i in range(0, IN_RATE, 480):  # 1 s while the first block hangs
        cs.push(np.zeros(480, dtype=np.float32))
        if i == 9600:
            await asyncio.sleep(0.05)
    release.set()
    assert c.gate.tripped and "behind" in c.gate.reason
    cs.close()


async def test_gate_trips_when_the_model_fails():
    class Broken(Fake):
        def process(self, x, sr):
            raise RuntimeError("cuda gone")

    c = Cleaner(Broken())
    cs = c.stream()
    await feed(cs, speech(0.5))
    assert c.gate.tripped and "RuntimeError" in c.gate.reason


def test_gate_ignores_the_first_blocks():
    g = RealtimeGate(0.3, warmup_blocks=3)
    g.record(1.0, 0.2)
    g.record(1.0, 0.2)
    assert not g.tripped  # warm-up blocks can be slow
    g = RealtimeGate(0.3, warmup_blocks=3)
    for _ in range(10):
        g.record(0.02, 0.2)
    assert not g.tripped and g.rtf == pytest.approx(0.1)


def test_one_cleaner_per_denoiser_and_none_for_passthrough():
    f = Fake()
    assert cleaner_for(f) is cleaner_for(f)
    assert cleaner_for(denoise_mod.PassthroughDenoiser()) is None
    assert cleaner_for(None) is None


def test_warm_runs_silent_blocks_outside_the_gate():
    fake = Fake()
    c = Cleaner(fake)
    c.warm()
    assert fake.sizes == [9600 + 4800] * 2 and c.gate.blocks == 0


def test_deepfilternet_loads_once_per_process(monkeypatch):
    loads = []

    class DF:
        name = "deepfilternet"
        active = True

        def __init__(self, options=None):
            loads.append(options)

    monkeypatch.setattr(denoise_mod, "DeepFilterNetDenoiser", DF)
    monkeypatch.setattr(denoise_mod, "_loaded", {})
    a = denoise_mod.make_denoiser("deepfilternet", {"threads": 1})
    b = denoise_mod.make_denoiser("deepfilternet", {"threads": 1})
    assert a is b and len(loads) == 1
    assert denoise_mod.make_denoiser("deepfilternet", {"threads": 2}) is not a


def test_failed_load_is_remembered(monkeypatch, caplog):
    class DF:
        def __init__(self, options=None):
            raise ImportError("no df")

    monkeypatch.setattr(denoise_mod, "DeepFilterNetDenoiser", DF)
    monkeypatch.setattr(denoise_mod, "_loaded", {})
    with caplog.at_level(logging.WARNING, logger="raum.pipeline.denoise"):
        for _ in range(3):
            assert isinstance(denoise_mod.make_denoiser("deepfilternet", {}), denoise_mod.PassthroughDenoiser)
    assert sum("deepfilternet unavailable" in r.message for r in caplog.records) == 1
