"""Several voice-service instances (``tts.options.urls``, tts_http.py): a clause goes to an instance that is not busy,
a busy refusal or a failure tries the other one before the fallback voice, "down" is per instance, background
clauses take the idle one, and ``forget`` / ``warmup`` reach every instance. One instance (``url`` only) behaves as
before: test_pipeline_voice.py and test_tts_background.py."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import numpy as np
import pytest

from raum.pipeline.tts import make_tts
from raum.pipeline.tts_http import HttpTTS, VoiceBusy

AUDIO = b"\x10\x00" * 4800  # 0.2 s of int16 at 24 kHz
A, B = "http://a.test", "http://b.test"


class Instance:
    """One raum-tts instance behind a MockTransport: /tts, /health, DELETE /tts/voices/{id}."""

    def __init__(self, status: int = 200, down: bool = False, busy: str | None = None, priorities: bool = True):
        self.status, self.down, self.busy, self.priorities = status, down, busy, priorities
        self.posts: list[dict] = []
        self.deleted: list[str] = []
        self.probes = 0
        self.hold: asyncio.Event | None = None  # a clause's audio waits for it: the request stays in flight

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        await req.aread()
        if req.method == "POST":
            self.posts.append(json.loads(req.content))
        if self.down:
            raise httpx.ConnectError("refused", request=req)
        if req.method == "GET":
            self.probes += 1
            return httpx.Response(200, json={"busy": self.busy is not None, "busy_priority": self.busy, "waiting": 0})
        if req.method == "DELETE":
            self.deleted.append(req.url.path.rsplit("/", 1)[1])
            return httpx.Response(204)
        if self.status >= 400:
            return httpx.Response(self.status, json={"error": "tts busy", "reason": "busy: predicted wait 1.4s"})
        hold = self.hold

        async def gen():
            yield AUDIO[:960]
            if hold is not None:
                await hold.wait()
            yield AUDIO[960:]

        headers = {"x-sample-rate": "24000"}
        if self.priorities:
            headers["x-priority"] = self.posts[-1].get("priority", "live")
        return httpx.Response(200, headers=headers, content=gen())


class Box:
    def __init__(self, **inst: Instance) -> None:
        self.inst = inst

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        return await self.inst[req.url.host.split(".")[0]](req)


class Fallback:
    name, sample_rate = "piper", 24000

    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(self, text, lang, *, voice_ref=None, rate=None):
        self.calls += 1
        yield np.full(100, 7, dtype=np.int16)


def two(a: Instance, b: Instance, fallback=None, **options) -> HttpTTS:
    client = httpx.AsyncClient(transport=httpx.MockTransport(Box(a=a, b=b)))
    return HttpTTS(A, options={"sample_rate": 24000, "retry_s": 30, "urls": [A, B], **options},
                   fallback=fallback, client=client)


async def say(tts, **kw) -> np.ndarray:
    return np.concatenate([c async for c in tts.synthesize("eins zwei drei", "de", **kw)])


def test_urls_option_and_a_single_url_as_before():
    tts = make_tts("http", {"urls": [A + "/", B]}, url="http://ignored:1", fallback="none")
    assert tts.urls == [A, B] and tts.url == A
    assert make_tts("http", {"urls": f"{A}, {B}"}, fallback="none").urls == [A, B]
    one = make_tts("http", {}, url=A, fallback="none")
    assert one.urls == [A] and one.url == A  # urls unset: url alone


async def test_a_live_clause_goes_to_the_instance_that_is_not_busy():
    a, b = Instance(busy="live"), Instance()  # another room's clause is being made on a
    fb = Fallback()
    tts = two(a, b, fallback=fb)
    assert len(await say(tts)) == 4800
    assert a.posts == [] and len(b.posts) == 1 and a.probes == b.probes == 1 and fb.calls == 0
    a.busy, b.busy = None, "live"
    await say(tts)
    assert len(a.posts) == 1 and len(b.posts) == 1
    a.busy = b.busy = None  # both idle: the first of urls
    await say(tts)
    assert len(a.posts) == 2
    a.busy = "background"  # a speculative job: a live request preempts it, but an idle instance is better still
    await say(tts)
    assert len(b.posts) == 2
    await tts.aclose()


async def test_a_busy_refusal_tries_the_other_instance_before_the_fallback():
    a, b = Instance(status=503), Instance()  # no probe: a looks idle, refuses ("busy: predicted wait 1.4 s")
    fb = Fallback()
    tts = two(a, b, fallback=fb, probe_s=0)
    assert len(await say(tts)) == 4800 and len(a.posts) == len(b.posts) == 1 and fb.calls == 0
    ia = tts._inst[0]
    assert ia.down_until == 0.0 and ia.busy_until > time.monotonic() and tts._down_until == 0.0  # busy, not down
    await say(tts)
    assert len(a.posts) == 1 and len(b.posts) == 2  # b first while a was busy a moment ago
    await tts.aclose()


async def test_one_instance_down_no_fallback_while_the_other_answers():
    a, b = Instance(down=True), Instance()
    fb = Fallback()
    tts = two(a, b, fallback=fb, probe_s=0)  # no probe: the clause itself finds a down
    for _ in range(3):
        assert len(await say(tts)) == 4800
    assert len(a.posts) == 1 and len(b.posts) == 3 and fb.calls == 0  # a tried once, then left aside for retry_s
    ia, ib = tts._inst
    assert ia.down_until > time.monotonic() and ib.down_until == 0.0 and tts._down_until == 0.0
    assert not tts._skip_service()  # the service as a whole is not put aside
    assert len(await say(tts, background=True)) == 4800 and len(b.posts) == 4  # background: b too
    await tts.aclose()

    a, b = Instance(down=True), Instance()
    probed = two(a, b, fallback=fb)  # with the probe: a's /health fails, so a is not even tried
    assert len(await say(probed)) == 4800 and a.posts == [] and len(b.posts) == 1 and fb.calls == 0
    await probed.aclose()


async def test_every_instance_failing_falls_back_and_puts_the_service_aside():
    a, b = Instance(status=503), Instance(down=True)
    fb = Fallback()
    tts = two(a, b, fallback=fb)
    assert (await say(tts)).tolist() == [7] * 100 and fb.calls == 1
    assert tts._down_until > time.monotonic()  # retry_s, as with one instance
    await say(tts)
    assert len(a.posts) == len(b.posts) == 1 and fb.calls == 2  # skipped meanwhile
    with pytest.raises(VoiceBusy, match="put aside"):
        await say(tts, background=True)
    tts._down_until = 0.0  # retry_s has passed
    a.status = 200
    assert len(await say(tts)) == 4800 and fb.calls == 2
    await tts.aclose()


async def test_mid_clause_failure_is_not_repeated_on_the_other_instance():
    class Cut(Instance):
        async def __call__(self, req):
            r = await super().__call__(req)
            if req.method != "POST":
                return r

            async def gen():
                yield AUDIO[:960]
                raise httpx.ReadError("connection reset")

            return httpx.Response(200, headers=r.headers, content=gen())

    a, b = Cut(), Instance()
    fb = Fallback()
    tts = two(a, b, fallback=fb)
    assert len(await say(tts)) == 480 and b.posts == [] and fb.calls == 0  # what was said stays said
    assert tts._inst[0].down_until > 0 and tts._inst[1].down_until == 0.0
    await tts.aclose()


async def test_background_takes_the_idle_instance():
    a, b = Instance(), Instance()
    tts = two(a, b, fallback=Fallback())
    a.hold = asyncio.Event()  # a live clause still streaming from a (this room's)
    live = asyncio.create_task(say(tts))
    while not a.posts:
        await asyncio.sleep(0)
    assert len(await say(tts, background=True)) == 4800
    assert len(a.posts) == 1 and b.posts[0]["priority"] == "background"
    a.hold.set()
    await live

    a.status = 409  # a refuses for its own live speech: b makes it
    assert len(await say(tts, background=True)) == 4800 and len(a.posts) == 2 and len(b.posts) == 2
    b.status = 409
    with pytest.raises(VoiceBusy, match="HTTP 409"):  # both refuse: VoiceBusy, as with one instance
        await say(tts, background=True)
    assert tts._down_until == 0.0 and all(i.down_until == 0.0 for i in tts._inst)
    await tts.aclose()


async def test_background_skips_an_instance_without_priorities():
    a, b = Instance(priorities=False), Instance()
    tts = two(a, b)
    await tts.warmup()
    assert tts._inst[0].priorities is False and tts._inst[1].priorities is True
    assert len(await say(tts, background=True)) == 4800
    assert len(a.posts) == 1 and len(b.posts) == 2  # a: only the warm-up
    await tts.aclose()


async def test_forget_reaches_every_instance():
    a, b = Instance(), Instance()
    tts = two(a, b)
    assert await tts.forget(["v1", "v2", "v1"]) == 2
    assert a.deleted == b.deleted == ["v1", "v2"]
    b.down = True  # b unreachable: a still forgets, the voice is not reported gone
    assert await tts.forget(["v3"]) == 0 and a.deleted[-1] == "v3"
    await tts.aclose()


async def test_warmup_warms_every_instance_and_survives_one_down():
    a, b = Instance(), Instance()
    tts = two(a, b, fallback=Fallback())
    tts.gate = True
    await tts.warmup()
    assert tts.ready and len(a.posts) == len(b.posts) == 1
    a2, b2 = Instance(), Instance(down=True)
    half = two(a2, b2, fallback=Fallback())
    await half.warmup()  # no raise: a is warm
    assert half.ready and half._inst[1].down_until > time.monotonic() and half._inst[0].down_until == 0.0
    none = two(Instance(down=True), Instance(down=True))
    with pytest.raises(httpx.ConnectError):  # the agent retries the warm-up
        await none.warmup()
    assert not none.ready
    for t in (tts, half, none):
        await t.aclose()
