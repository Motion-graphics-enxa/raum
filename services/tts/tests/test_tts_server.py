"""raum-tts server.py end to end without the model: the real FastAPI app under uvicorn, a fake engine that streams
like cb_engine (token steps, ``should_stop`` polled every token), and RAUM's real ``HttpTTS`` client. Checks the
priorities of API.md: a background clause never delays a live one.

torch / soundfile / librosa / cb_engine are stand-ins in ``sys.modules`` for the test only. Needs core's venv
(fastapi, uvicorn, httpx): ``cd core && uv run pytest ../services/tts/tests``."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import socket
import sys
import time
import types
from pathlib import Path

import httpx
import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
SR = 24000
SAMPLES_PER_TOKEN = SR // 25  # 25 speech tokens per second, as Chatterbox


class FakeEngine:
    """cb_engine.CBEngine's surface. Two tokens per character; a chunk every ``tokens_per_chunk`` tokens."""

    langs = ("de", "en")
    step_s = 0.0  # per token (0 while the server warms up at import)
    tokens_per_chunk = 5
    wave = None  # None: every sample 0.1 (no silence to trim); or wave(text) -> the clause's float32 audio

    def make_voice(self, wav, sr, exaggeration=0.5):
        return object()

    def stream(self, text, lang, voice=None, *, should_stop=None, stats=None, max_new_tokens=600, **kw):
        w = None if self.wave is None else self.wave(text)
        n = min(max_new_tokens, 2 * len(text) if w is None else -(-len(w) // SAMPLES_PER_TOKEN))

        def audio(a, b):  # tokens a..b
            if w is None:
                return np.full(SAMPLES_PER_TOKEN * (b - a), 0.1, dtype=np.float32)
            return w[a * SAMPLES_PER_TOKEN:b * SAMPLES_PER_TOKEN]

        for i in range(n):
            if should_stop is not None and should_stop():
                if stats is not None:
                    stats.update(stopped=True, tokens=i)
                return
            time.sleep(self.step_s)
            if (i + 1) % self.tokens_per_chunk == 0:
                yield audio(i + 1 - self.tokens_per_chunk, i + 1)
        if n % self.tokens_per_chunk:  # the final vocode: the rest
            yield audio(n - n % self.tokens_per_chunk, n)
        if stats is not None:
            stats.update(tokens=n, capped=False)


def samples(text):
    return 2 * len(text) * SAMPLES_PER_TOKEN


@pytest.fixture
def srv(monkeypatch, tmp_path):
    """server.py imported against the stand-ins; sys.modules, sys.path and the uvicorn logger restored after."""
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(memory_allocated=lambda: 0, memory_reserved=lambda: 0)
    soundfile = types.ModuleType("soundfile")
    librosa = types.ModuleType("librosa")
    librosa.resample = lambda y, orig_sr, target_sr: y
    cb = types.ModuleType("cb_engine")
    cb.CBEngine, cb.SR = FakeEngine, SR
    for name, mod in (("torch", torch), ("soundfile", soundfile), ("librosa", librosa), ("cb_engine", cb)):
        monkeypatch.setitem(sys.modules, name, mod)
    for name in ("admission", "textnorm", "raum_tts_server"):  # removed again (or put back) at teardown
        monkeypatch.setitem(sys.modules, name, sys.modules.get(name))
        sys.modules.pop(name)
    monkeypatch.setattr(sys, "path", list(sys.path))  # server.py prepends its own directory
    monkeypatch.setenv("RAUM_TTS_DEFAULT_VOICES", str(tmp_path / "no-voices"))
    uv_log = logging.getLogger("uvicorn.error")
    filters = list(uv_log.filters)
    spec = importlib.util.spec_from_file_location("raum_tts_server", SRC / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["raum_tts_server"] = mod
    spec.loader.exec_module(mod)
    mod.engine.step_s = 0.01  # a 40-char clause: 80 tokens, 3.2 s of audio in 0.8 s
    yield mod
    uv_log.filters[:] = filters


@contextlib.asynccontextmanager
async def serving(app):
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await task


class NoFallback:
    """Counts fallback use: background clauses must never reach it."""

    name, sample_rate, calls = "piper", SR, 0

    async def synthesize(self, text, lang, **kw):
        NoFallback.calls += 1
        yield np.zeros(240, dtype=np.int16)


def client(url):
    from raum.pipeline.tts_http import HttpTTS

    NoFallback.calls = 0
    return HttpTTS(url, options={"sample_rate": SR, "retry_s": 2.0}, fallback=NoFallback())


class Run:
    """One synthesize call as a task: when its first chunk came, how much audio, and how it ended."""

    def __init__(self, tts, text, **kw):
        self.t0 = time.perf_counter()
        self.first_s = None
        self.samples = 0
        self.first = asyncio.Event()
        self.task = asyncio.create_task(self._go(tts, text, kw))

    async def _go(self, tts, text, kw):
        async for c in tts.synthesize(text, "de", **kw):
            if self.first_s is None:
                self.first_s = time.perf_counter() - self.t0
                self.first.set()
            self.samples += len(c)


LONG = "Nehmen Sie dieses Medikament nicht zusammen mit Alkohol ein."  # 120 tokens: 4.8 s of audio in 1.2 s
SHORT = "Haben Sie heute schon gegessen?"  # 62 tokens: 2.5 s of audio in 0.6 s


def test_background_alone_is_made_and_marked(srv):
    async def main():
        async with serving(srv.app) as url, httpx.AsyncClient() as c:
            async with c.stream("POST", url + "/tts", json={"text": SHORT, "lang": "de",
                                                           "priority": "background"}) as r:
                body = b"".join([b async for b in r.aiter_bytes()])
            assert r.status_code == 200 and r.headers["x-priority"] == "background"
            assert len(body) == 2 * samples(SHORT)
            bad = await c.post(url + "/tts", json={"text": SHORT, "lang": "de", "priority": "low"})
            assert bad.status_code == 422
            h = (await c.get(url + "/health")).json()
            assert h["version"] == "1.4" and h["busy_priority"] is None and h["counters"]["done"] >= 1

    asyncio.run(main())


def test_background_is_refused_at_once_while_live_speaks(srv):
    from raum.pipeline.tts_http import VoiceBusy

    async def main():
        async with serving(srv.app) as url:
            tts = client(url)
            live = Run(tts, LONG)
            await live.first.wait()
            t0 = time.perf_counter()
            with pytest.raises(VoiceBusy, match="HTTP 409"):
                async for _ in tts.synthesize(SHORT, "de", background=True):
                    pass
            assert time.perf_counter() - t0 < 0.2  # never queued
            await live.task
            assert live.samples == samples(LONG)  # the live clause, whole
            assert NoFallback.calls == 0 and tts._down_until == 0.0
            assert srv.stats["bg_rejected_busy"] == 1 and srv.stats["preempted"] == 0
            await tts.aclose()

    asyncio.run(main())


def test_live_cuts_a_background_clause_mid_stream(srv):
    from raum.pipeline.tts_http import VoiceBusy

    async def main():
        async with serving(srv.app) as url:
            tts = client(url)
            bg = Run(tts, LONG, background=True)
            await bg.first.wait()  # the background clause is streaming
            live = Run(tts, SHORT)
            await live.task
            with pytest.raises(VoiceBusy, match="stream cut"):
                await bg.task
            assert 0 < bg.samples < samples(LONG)  # a cut clause: the caller discards it
            assert live.samples == samples(SHORT) and live.first_s < 0.25  # it used to wait out the clause
            assert NoFallback.calls == 0 and tts._down_until == 0.0
            assert srv.stats["preempted"] == 1 and srv.gate.preemptions == 1
            await tts.aclose()

    asyncio.run(main())


def test_live_preempts_a_background_clause_before_its_first_audio(srv):
    from raum.pipeline.tts_http import VoiceBusy

    async def main():
        srv.engine.tokens_per_chunk = 30  # first audio after 0.3 s
        async with serving(srv.app) as url:
            tts = client(url)
            bg = Run(tts, LONG, background=True)
            await asyncio.sleep(0.1)  # admitted and generating, no audio yet
            assert srv.gate.holder is not None and srv.gate.holder.background
            live = Run(tts, SHORT)
            with pytest.raises(VoiceBusy, match="HTTP 409"):
                await bg.task
            await live.task
            assert bg.samples == 0 and live.samples == samples(SHORT)
            assert live.first_s < 0.8  # free within a token step, then 30 tokens (0.3 s); not preempted: >= 1.2 s
            assert srv.stats["bg_rejected_preempted"] == 1 and NoFallback.calls == 0
            await tts.aclose()

    asyncio.run(main())


def test_too_long_background_text_is_refused(srv):
    from raum.pipeline.tts_http import VoiceBusy

    async def main():
        async with serving(srv.app) as url:
            tts = client(url)
            with pytest.raises(VoiceBusy, match="HTTP 409"):
                async for _ in tts.synthesize("Wort " * 100, "de", background=True):  # 500 chars > 400
                    pass
            assert srv.stats["bg_rejected_too_long"] == 1 and not srv.gate.lock.locked()
            await tts.aclose()

    asyncio.run(main())


def test_live_reports_its_gpu_wait_in_x_queue_ms(srv):
    """``X-Queue-Ms``: the admission wait for the GPU, known before the headers go out; HttpTTS notes it as the
    ``tts`` stage's ``queue_ms`` (core/raum/pipeline/stats.py)."""
    from raum.pipeline import stats

    async def main():
        async with serving(srv.app) as url:
            tts = client(url)
            s = stats.Stages()
            stats.use(s)
            assert srv.gate.lock.acquire(blocking=False)  # the GPU busy for 0.1 s (inside the 0.25 s grace)
            asyncio.get_running_loop().call_later(0.1, srv.gate.lock.release)
            assert sum(len(c) for c in [c async for c in tts.synthesize(SHORT, "de")]) == samples(SHORT)
            t = s.snapshot()["tts"]
            assert 80 <= t["queue_ms"] < 240 and t["ttfb_ms"] >= t["queue_ms"]
            async with httpx.AsyncClient() as c, c.stream("POST", url + "/tts", json={"text": SHORT, "lang": "de"}) as r:
                await r.aread()
            assert r.status_code == 200 and 0 <= int(r.headers["x-queue-ms"]) < 80  # the GPU free: no wait
            await tts.aclose()

    asyncio.run(main())


# ---------------- two instances (RAUM's tts options.urls, :8090 + :8091) ----------------
def second_instance(monkeypatch):
    """Another server.py process, as ops run on :8091: its own GPU gate, voices and counters."""
    spec = importlib.util.spec_from_file_location("raum_tts_server_b", SRC / "server.py")
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "raum_tts_server_b", mod)
    spec.loader.exec_module(mod)
    mod.engine.step_s = 0.01
    return mod


def test_two_instances_a_clause_takes_the_idle_one_and_forget_reaches_both(srv, monkeypatch):
    from raum.pipeline.tts_http import HttpTTS

    b = second_instance(monkeypatch)

    async def main():
        async with serving(srv.app) as ua:
            async with serving(b.app) as ub:
                other_room = HttpTTS(ua, options={"sample_rate": SR, "retry_s": 2.0})  # another room's process
                tts = HttpTTS(ua, options={"sample_rate": SR, "retry_s": 2.0, "urls": [ua, ub]},
                              fallback=NoFallback())
                NoFallback.calls = 0
                busy = Run(other_room, LONG)
                await busy.first.wait()  # a is making the other room's clause (1.2 s)
                mine = Run(tts, SHORT)
                await mine.task
                assert mine.samples == samples(SHORT) and mine.first_s < 0.2  # on a: >= 0.25 s grace, or a 503
                await busy.task
                assert srv.stats["done"] == 1 and b.stats["done"] == 1 and NoFallback.calls == 0
                assert sum(srv.stats[k] for k in srv.stats if k.startswith("rejected")) == 0

                for m in (srv, b):  # the speaker's sample, registered on both
                    m._store_voice("0123456789abcdef", object(), 4.0, "de")
                assert await tts.forget(["0123456789abcdef"]) == 1
                assert not srv.voices and not b.voices
                await other_room.aclose()
            # b is gone (stopped, crashed): the next clause goes to a, no Piper, the service not put aside
            async for _ in tts.synthesize(SHORT, "de"):
                pass
            assert srv.stats["done"] == 2 and NoFallback.calls == 0 and tts._down_until == 0.0
            await tts.aclose()

    asyncio.run(main())


# ---------------- v1.4: the silence at a clause's edges (edges.py) ----------------
def edged(lead_s=0.12, speech_s=1.0, tail_s=0.6):
    """A clause as Chatterbox makes it: silence, speech (every sample 0.1), the T3 tail of silence."""
    def wave(text):
        return np.concatenate([np.zeros(int(lead_s * SR), np.float32), np.full(int(speech_s * SR), 0.1, np.float32),
                               np.zeros(int(tail_s * SR), np.float32)])
    return wave


async def body_of(url, text, **extra):
    t0 = time.perf_counter()
    first = None
    body = b""
    async with httpx.AsyncClient() as c, c.stream("POST", url + "/tts", json={"text": text, "lang": "de", **extra}) as r:
        assert r.status_code == 200
        async for b in r.aiter_bytes():
            first = first if first is not None else time.perf_counter() - t0
            body += b
    return np.frombuffer(body, dtype="<i2"), first


def test_clause_edges_trimmed_by_how_the_text_ends(srv):
    srv.engine.wave = edged()
    assert srv.tail_keep_s("Guten Tag.") == srv.tail_keep_s("Wirklich?") == srv.tail_keep_s("Er sagte: »Ja!«") == 0.20
    assert srv.tail_keep_s("Guten Tag,") == srv.tail_keep_s("Nehmen Sie das Medikament") == 0.08

    async def main():
        async with serving(srv.app) as url:
            speech = int(1.0 * SR)
            for text, tail in (("Nehmen Sie das Medikament,", 0.08), ("Nehmen Sie das Medikament", 0.08),
                               ("Nehmen Sie das Medikament.", 0.20)):
                pcm, _ = await body_of(url, text)
                assert len(pcm) == int(0.03 * SR) + speech + int(tail * SR), text  # was 0.12 + 1.0 + 0.6 s
                assert pcm[0] == 0 and pcm[-1] == 0 and (pcm[int(0.03 * SR):][:speech] == 3276).all()
            tts = client(url)  # RAUM's client gets the trimmed clause, and no fallback
            got = sum([len(c) async for c in tts.synthesize("Haben Sie heute schon gegessen?", "de")])
            assert got == int(0.03 * SR) + speech + int(0.20 * SR) and NoFallback.calls == 0
            await tts.aclose()
            async with httpx.AsyncClient() as c:
                h = (await c.get(url + "/health")).json()
            assert h["trim_edges"] == {"lead_s": 0.03, "tail_s": 0.08, "tail_sentence_s": 0.2}
            assert h["counters"]["edge_trimmed_ms"] == 2 * (90 + 520) + 2 * (90 + 400)  # lead + tail, per clause

            srv.TRIM_EDGES = False  # RAUM_TTS_TRIM_EDGES=0: both edges as the engine made them
            pcm, _ = await body_of(url, "Nehmen Sie das Medikament.")
            assert len(pcm) == int(1.72 * SR)
            async with httpx.AsyncClient() as c:
                assert (await c.get(url + "/health")).json()["trim_edges"] is None

    asyncio.run(main())


def test_trimming_adds_no_first_chunk_latency(srv):
    """The first chunk with sound goes out as soon as the engine yields it, trimmed or not."""
    srv.engine.wave = edged(lead_s=0.12)  # the lead silence lies inside the first chunk, as on the box
    srv.engine.tokens_per_chunk = 10  # a chunk every 0.1 s: one held back would show as +0.1 s

    async def main():
        async with serving(srv.app) as url:
            firsts: dict[bool, list[float]] = {}
            for trim in (False, True, False, True):
                srv.TRIM_EDGES = trim
                pcm, first = await body_of(url, "Guten Tag.")
                firsts.setdefault(trim, []).append(first)
                assert len(pcm) == (int(0.03 * SR) + int(1.2 * SR) if trim else int(1.72 * SR))
            assert min(firsts[True]) < min(firsts[False]) + 0.05, firsts

    asyncio.run(main())
