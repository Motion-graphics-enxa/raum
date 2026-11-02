"""Resident Piper (binary) against a fake ``piper``: a small Python script with the same stdin/stdout
protocol (``--output_dir``: a text line in, a wav path out; ``--output-raw``: raw PCM on stdout)."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

import numpy as np
import pytest

from raum.pipeline.tts import PiperTTS, close_residents, make_tts, read_wav

FAKE_PIPER = r'''
import json, os, struct, sys, time, wave

args = sys.argv[1:]


def opt(*names):
    for n in names:
        if n in args:
            return args[args.index(n) + 1]
    return None


model = opt("-m", "--model")
out_dir = opt("-d", "--output_dir", "--output-dir")
raw = "--output-raw" in args or "--output_raw" in args
try:
    rate = json.load(open(model + ".json"))["audio"]["sample_rate"]
except Exception:
    rate = 22050
log = os.environ.get("FAKE_PIPER_LOG")


def note(s):
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {s}\n")


note("start " + ("raw" if raw else "dir"))
time.sleep(float(os.environ.get("FAKE_PIPER_LOAD_S", "0")))
for line in sys.stdin:
    text = line.strip()
    note("text " + text)
    if not raw and "CRASH" in text:
        sys.exit(3)
    if not raw and "HANG" in text:
        time.sleep(60)
    n = 160 * len(text.split())  # 160 samples per word: the length tells which clause this is
    data = struct.pack("<%dh" % n, *([1000] * n))
    if raw:
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
        continue
    path = os.path.join(out_dir, "%d.wav" % time.time_ns())
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)
    print("[info] synthesized", flush=True)  # noise a build might print: skipped
    print(path, flush=True)
'''


@pytest.fixture
def fake_piper(tmp_path, monkeypatch):
    binary = tmp_path / "piper"
    binary.write_text(f"#!{sys.executable}\n{FAKE_PIPER}", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    voices = tmp_path / "voices"
    voices.mkdir()
    for name in ("de_DE-test-medium", "ar_JO-test-medium"):
        (voices / f"{name}.onnx").write_bytes(b"")
        (voices / f"{name}.onnx.json").write_text(json.dumps({"audio": {"sample_rate": 16000}}), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    log = tmp_path / "piper.log"
    monkeypatch.setenv("FAKE_PIPER_LOG", str(log))
    opts = {"binary": str(binary), "voices_dir": str(voices), "sample_rate": 16000, "output_dir": str(out),
            "timeout_s": 2.0}
    yield opts, log, out
    close_residents()


def lines(log: Path) -> list[tuple[int, str, str]]:
    if not log.exists():
        return []
    out = []
    for ln in log.read_text(encoding="utf-8").splitlines():
        pid, kind, *rest = ln.split(" ", 2)
        out.append((int(pid), kind, rest[0] if rest else ""))
    return out


def starts(log: Path, mode: str) -> list[int]:
    return [pid for pid, kind, rest in lines(log) if kind == "start" and rest == mode]


async def collect(tts: PiperTTS, text: str, lang: str = "de") -> np.ndarray:
    chunks = [c async for c in tts.synthesize(text, lang)]
    assert all(c.dtype == np.int16 for c in chunks)
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)


async def until(cond, timeout: float = 10.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def ready(tts: PiperTTS, lang: str = "de") -> None:
    r = tts.resident_for(lang)
    assert r is not None
    await until(lambda: r.ready)


def test_read_wav_roundtrip_and_delete(tmp_path):
    import wave

    p = tmp_path / "x.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(np.array([1, -1, 2, -2, 3, -3], dtype="<i2").tobytes())
    pcm, rate = read_wav(p)
    assert rate == 22050 and pcm.dtype == np.int16 and pcm.tolist() == [1, 2, 3]
    assert not p.exists()


async def test_resident_starts_per_voice_present_and_streams_chunks(fake_piper):
    opts, log, out = fake_piper
    tts = make_tts("piper", opts)
    assert isinstance(tts, PiperTTS) and tts.resident
    assert tts.prepare(["de", "ar", "tr", "de"]) == ["de", "ar"]  # no Turkish voice: nothing started
    await ready(tts, "de")
    await ready(tts, "ar")
    assert len(starts(log, "dir")) == 2

    pcm = await collect(tts, "Guten Tag, Frau Demir.")
    assert len(pcm) == 160 * 4 and set(pcm.tolist()) == {1000}
    long = await collect(tts, " ".join(["Wort"] * 20))  # 3200 samples: streamed in 100 ms chunks
    chunks = [c async for c in tts.synthesize(" ".join(["Wort"] * 20), "de")]
    assert len(long) == 3200 and [len(c) for c in chunks] == [1600, 1600]
    assert len(starts(log, "dir")) == 2 and starts(log, "raw") == []  # same processes, no one-shot
    assert list(out.glob("*/*.wav")) == []  # every wav deleted after use (warm-up ones too)
    de = tts.resident_for("de")
    texts = [rest for pid, kind, rest in lines(log) if kind == "text" and pid == de.pid]
    assert texts[:2] == ["ok.", "Guten Tag, Frau Demir."]  # warm-up first
    assert [rest for pid, kind, rest in lines(log) if kind == "text" and pid != de.pid] == ["ok."]  # ar: warm-up


async def test_requests_per_voice_are_serialized_in_order(fake_piper):
    opts, log, _ = fake_piper
    tts = make_tts("piper", opts)
    tts.prepare(["de"])
    await ready(tts)
    clauses = [f"Satz {i} " + "w " * i for i in range(6)]
    results = await asyncio.gather(*(collect(tts, c) for c in clauses))
    assert [len(r) for r in results] == [160 * len(c.split()) for c in clauses]  # each got its own audio
    pid = starts(log, "dir")[0]
    texts = [rest for p, kind, rest in lines(log) if kind == "text" and p == pid][1:]
    assert texts == [" ".join(c.split()) for c in clauses]  # one at a time, in submission order


async def test_dead_resident_falls_back_to_one_shot_and_restarts(fake_piper):
    opts, log, _ = fake_piper
    tts = make_tts("piper", opts)
    tts.prepare(["de"])
    await ready(tts)
    first = starts(log, "dir")[0]

    pcm = await collect(tts, "bitte CRASH jetzt")  # the resident dies on this line
    assert len(pcm) == 160 * 3  # ... the clause is still spoken, one-shot
    assert len(starts(log, "raw")) == 1
    await ready(tts)  # restarted in the background
    pcm = await collect(tts, "wieder da")
    assert len(pcm) == 320
    dirs = starts(log, "dir")
    assert len(dirs) == 2 and dirs[0] == first
    served = [p for p, kind, rest in lines(log) if kind == "text" and rest == "wieder da"]
    assert served == [dirs[1]]  # by the new resident, not one-shot


async def test_hung_resident_is_killed_and_the_clause_goes_one_shot(fake_piper):
    opts, log, _ = fake_piper
    tts = make_tts("piper", {**opts, "timeout_s": 0.3})
    tts.prepare(["de"])
    await ready(tts)
    pcm = await collect(tts, "HANG hier")
    assert len(pcm) == 320 and len(starts(log, "raw")) == 1
    await ready(tts)
    assert len(starts(log, "dir")) == 2


async def test_not_ready_yet_goes_one_shot_and_starts_the_resident(fake_piper, monkeypatch):
    opts, log, _ = fake_piper
    monkeypatch.setenv("FAKE_PIPER_LOAD_S", "0.3")  # model load
    tts = make_tts("piper", opts)
    pcm = await collect(tts, "sofort bitte")  # not prepared: spoken one-shot, no waiting for the load
    assert len(pcm) == 320 and len(starts(log, "raw")) == 1
    await ready(tts)
    assert len(starts(log, "dir")) == 1


async def test_resident_off_spawns_per_clause(fake_piper):
    opts, log, _ = fake_piper
    tts = make_tts("piper", {**opts, "resident": False})
    assert tts.prepare(["de"]) == []
    assert len(await collect(tts, "eins zwei")) == 320
    ignored = [c async for c in tts.synthesize("drei", "de", voice_ref=b"RIFF....", rate=1.3)]  # accepted, ignored
    assert sum(len(c) for c in ignored) == 160
    assert starts(log, "dir") == [] and len(starts(log, "raw")) == 2


async def test_resident_resamples_to_the_output_rate(fake_piper):
    opts, _, _ = fake_piper
    tts = make_tts("piper", {**opts, "sample_rate": 48000})
    tts.prepare(["de"])
    await ready(tts)
    pcm = await collect(tts, "eins zwei")  # 320 samples at 16 kHz -> 960 at 48 kHz
    assert abs(len(pcm) - 960) <= 2


def test_no_binary_disables_voice(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert make_tts("piper", {"voices_dir": str(tmp_path)}) is None
