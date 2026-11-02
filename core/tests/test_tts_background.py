"""Background (speculative) voice requests: ``synthesize(..., background=True)`` sends ``priority: background``
(services/tts/API.md, "Priorities"). A refusal or a cut stream raises ``VoiceBusy``; a background call never puts
the service aside (``retry_s``) and never uses the fallback voice. Live calls are unchanged."""

from __future__ import annotations

import json
import time

import httpx
import numpy as np
import pytest

from raum.pipeline.base import takes_kwarg
from raum.pipeline.tts import MockTTS, PiperTTS
from raum.pipeline.tts_http import HttpTTS, TtsServiceError, VoiceBusy, tts_request

AUDIO = b"\x10\x00" * 4800  # 0.2 s of int16 at 24 kHz


class Service:
    """A MockTransport stand-in for the voice service: a status, or audio that may break off after N bytes."""

    def __init__(self, status: int = 200, cut_after: int | None = None, down: bool = False,
                 version: str = "1.3") -> None:
        self.status, self.cut_after, self.down, self.version = status, cut_after, down, version
        self.bodies: list[dict] = []

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        await req.aread()
        is_json = req.headers.get("content-type", "").startswith("application/json")
        self.bodies.append(json.loads(req.content) if is_json else {"multipart": req.content})
        if self.down:
            raise httpx.ConnectError("refused", request=req)
        if self.status >= 400:
            return httpx.Response(self.status, json={"error": "tts busy", "reason": "busy: GPU in use"})
        cut = self.cut_after

        async def gen():
            for i in range(0, len(AUDIO), 960):
                if cut is not None and i >= cut:  # what a client sees when the service cuts a stream
                    raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
                yield AUDIO[i:i + 960]

        headers = {"x-sample-rate": "24000"}
        if self.version >= "1.3":  # a service with priorities says which one it served
            headers["x-priority"] = self.bodies[-1].get("priority", "live")
        return httpx.Response(200, headers=headers, content=gen())


class Fallback:
    name, sample_rate = "fallback", 24000

    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(self, text, lang, *, voice_ref=None, rate=None):
        self.calls += 1
        yield np.full(100, 7, dtype=np.int16)


def http_tts(svc: Service, fallback=None, **options) -> HttpTTS:
    client = httpx.AsyncClient(transport=httpx.MockTransport(svc))
    return HttpTTS("http://tts.test", options={"sample_rate": 24000, "retry_s": 30, **options}, fallback=fallback,
                   client=client)


async def collect(tts, text="eins zwei drei", lang="de", **kw) -> list[np.ndarray]:
    return [c async for c in tts.synthesize(text, lang, **kw)]


def test_request_mapping_sends_priority_only_for_background():
    opts = {"sample_rate": 24000}
    assert tts_request("x", "de", voice_ref=None, rate=None, model=None, options=opts,
                       background=True)["json"]["priority"] == "background"
    assert "priority" not in tts_request("x", "de", voice_ref=None, rate=None, model=None, options=opts)["json"]
    mp = tts_request("x", "de", voice_ref=b"RIFF", rate=None, model=None, background=True,
                     options={**opts, "voice_ref_as": "multipart"})
    assert mp["data"]["priority"] == "background"


async def test_background_sends_the_field_and_live_stays_as_it_was():
    svc = Service()
    tts = http_tts(svc, fallback=Fallback())
    pcm = np.concatenate(await collect(tts, background=True, voice_ref=b"RIFF-voice", rate=1.1))
    assert len(pcm) == 4800 and tts.ready
    assert svc.bodies[0]["priority"] == "background" and svc.bodies[0]["rate"] == 1.1
    await collect(tts)
    assert "priority" not in svc.bodies[1]  # a live request is byte-for-byte what it was before priorities
    await tts.aclose()


@pytest.mark.parametrize("status", [409, 429, 503])
async def test_busy_raises_voice_busy_without_putting_the_service_aside_or_falling_back(status):
    svc = Service(status=status)
    fb = Fallback()
    tts = http_tts(svc, fallback=fb)
    with pytest.raises(VoiceBusy) as e:
        await collect(tts, background=True)
    assert e.value.reason == f"HTTP {status}" and isinstance(e.value, TtsServiceError)
    assert fb.calls == 0 and tts._down_until == 0.0  # nothing said in another voice; the service not skipped
    svc.status = 200
    assert np.concatenate(await collect(tts)).tolist() == [16] * 4800  # the live clause goes to the service
    assert len(svc.bodies) == 2 and fb.calls == 0


async def test_a_cut_background_stream_raises_voice_busy_after_its_chunks():
    svc = Service(cut_after=1920)
    fb = Fallback()
    tts = http_tts(svc, fallback=fb)
    got: list[np.ndarray] = []
    with pytest.raises(VoiceBusy) as e:
        async for c in tts.synthesize("eins zwei drei", "de", background=True):
            got.append(c)
    assert got and e.value.reason == "stream cut"  # the caller discards what it collected
    assert isinstance(e.value.__cause__, httpx.RemoteProtocolError)
    assert fb.calls == 0 and tts._down_until == 0.0

    live = http_tts(Service(cut_after=1920), fallback=fb)  # live, unchanged: what was said stays said
    assert sum(map(len, await collect(live))) == 960 and fb.calls == 0 and live._down_until > 0


async def test_other_background_failures_raise_as_they_are_and_change_nothing():
    fb = Fallback()
    tts = http_tts(Service(status=500), fallback=fb)
    with pytest.raises(TtsServiceError) as e:
        await collect(tts, background=True)
    assert not isinstance(e.value, VoiceBusy)
    down = http_tts(Service(down=True), fallback=fb)
    with pytest.raises(httpx.ConnectError):
        await collect(down, background=True)
    assert fb.calls == 0 and tts._down_until == 0.0 and down._down_until == 0.0

    live = http_tts(Service(status=409), fallback=fb)  # a live request: any refusal -> fallback, put aside
    assert np.concatenate(await collect(live)).tolist() == [7] * 100 and live._down_until > 0


async def test_background_is_not_sent_while_the_live_path_skips_the_service():
    svc = Service()
    fb = Fallback()
    tts = http_tts(svc, fallback=fb)
    tts._down_until = time.monotonic() + 30  # a live clause failed: live speech is on the fallback voice
    with pytest.raises(VoiceBusy, match="put aside"):
        await collect(tts, background=True)
    tts._down_until = 0.0
    tts.gate = True  # cold service, gate up: live clauses go to the fallback until warm
    with pytest.raises(VoiceBusy, match="not warm"):
        await collect(tts, background=True)
    assert svc.bodies == [] and fb.calls == 0
    assert await collect(tts, text="  ", background=True) == []  # nothing to say: no request, no error


async def test_no_background_requests_to_a_service_without_priorities():
    old = Service(version="1.2")  # would serve a background request as live, ahead of the next live clause
    fb = Fallback()
    tts = http_tts(old, fallback=fb)
    assert tts.priorities is None
    await tts.warmup()
    assert tts.priorities is False
    with pytest.raises(VoiceBusy, match="without priorities"):
        await collect(tts, background=True)
    assert len(old.bodies) == 1 and fb.calls == 0 and tts._down_until == 0.0  # only the warm-up was sent
    assert len(np.concatenate(await collect(tts))) == 4800  # live clauses: as before

    new = http_tts(Service(), fallback=fb)
    await collect(new)
    assert new.priorities is True and len(np.concatenate(await collect(new, background=True))) == 4800


async def test_other_engines_accept_and_ignore_background():
    assert await collect(MockTTS({"first_audio_ms": 0}), background=True) == []
    assert takes_kwarg(PiperTTS.synthesize, "background") and takes_kwarg(HttpTTS.synthesize, "background")
    assert takes_kwarg(MockTTS.synthesize, "background")


def test_takes_kwarg():
    def plain(text, lang): ...
    def kw_only(text, lang, *, voice_ref=None, background=False): ...
    def var_kw(text, lang, **kw): ...
    def positional_only(background, /): ...

    assert not takes_kwarg(plain, "background") and takes_kwarg(kw_only, "background")
    assert takes_kwarg(var_kw, "background") and not takes_kwarg(positional_only, "background")
    assert not takes_kwarg(object(), "background")
