"""The voice made ahead (speculate.py): the whole utterance's clauses are synthesised while the end of turn
waits, played only after it; unchanged clauses are reused when the final differs; the next clause is made while
one plays; speculative synthesis never slows a live clause. No LiveKit, models or network."""

from __future__ import annotations

import asyncio
import time

import numpy as np
from pipeline_helpers import Harness
from test_pipeline_simul import (  # noqa: F401  (no_guard: a fixture)
    DOC_DE,
    PAT_AR_SPK,
    Eot,
    Voice,
    idx,
    no_guard,
    pause,
    room,
    speak,
    until,
    until_ended,
)


async def test_the_whole_utterance_is_voiced_ahead_and_played_only_after_the_end(no_guard):  # noqa: F811
    h = room([DOC_DE, PAT_AR_SPK], complete_clauses=False, final_reveal=True)
    h.p.eot = Eot(waits=[0.25])
    voice = Voice(h)
    utt = await speak(h, DOC_DE.id, 0.6)  # "Guten Tag, Frau Demir. Ich bin" ... pause
    await pause(h, DOC_DE.id)
    await until_ended(h, utt)
    await h.p.drain()
    end = idx(h, "utterance.end", utt)
    assert len(voice.made) == 2 and all(at <= end for at in voice.made_at)  # every clause made during the wait
    assert voice.spoken == voice.made and all(at > end for at in voice.at)  # nothing published before the end
    t = h.one("pipeline.timing", utt)
    assert t["voice_clauses"] == {"spec": 2, "ahead": 0, "live": 0}
    assert h.p.spec.voice_hits == 2


class Fixed:
    """A translator with fixed outputs per source text."""

    name = "fixed"

    def __init__(self, table: dict[str, str]) -> None:
        self.table = table

    async def stream(self, req):
        for w in self.table[req.text].split(" "):
            await asyncio.sleep(0.001)
            yield w + " "


class Slow:
    """A voice engine: ``n`` chunks of ``chunk_s`` each; records starts, ends, kwargs and overlap."""

    name = "slow"
    sample_rate = 16000
    simulated = False

    def __init__(self, n: int = 3, chunk_s: float = 0.02, background: bool = False) -> None:
        self.n, self.chunk_s = n, chunk_s
        self.log: list[tuple[str, str, float, dict]] = []  # (event, text, t, kwargs)
        self.running = 0
        self.max_running = 0
        if background:
            self.synthesize = self._synth_bg  # type: ignore[method-assign]

    async def synthesize(self, text, lang, **_kw):  # voice_ref / rate / background: taken, ignored
        async for c in self._run(text, {}):
            yield c

    async def _synth_bg(self, text, lang, *, background=False):
        async for c in self._run(text, {"background": background}):
            yield c

    async def _run(self, text, kw):
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        self.log.append(("start", text, time.monotonic(), kw))
        try:
            for _ in range(self.n):
                await asyncio.sleep(self.chunk_s)
                yield np.ones(160, dtype=np.int16)
        finally:
            self.running -= 1
            self.log.append(("end", text, time.monotonic(), kw))

    def at(self, event: str, text: str) -> float:
        return next(t for e, x, t, _ in self.log if e == event and x == text)


SRC = "Guten Tag, Frau Demir. Ich bin Dr. Weber."


async def spec_room(tts, table):
    h = room([DOC_DE, PAT_AR_SPK], complete_clauses=False)
    h.engines.translate = Fixed(table)  # type: ignore[assignment]
    h.engines.tts = tts  # type: ignore[assignment]
    h.p.tts_sink = None
    await h.p.speech_start(DOC_DE.id)
    return h, h.p._active[DOC_DE.id]


async def play(h: Harness, text: str, lang: str = "ar", marks: dict | None = None, sink_s: float = 0.0) -> int:
    n = 0
    async for _ in h.p.spec.synthesize(h.engines.tts, text, lang, marks=marks):
        n += 1
        if sink_s:
            await asyncio.sleep(sink_s)
    return n


async def test_a_final_that_differs_in_its_tail_still_plays_the_unchanged_clauses_from_the_cache():
    tts = Slow(n=2, chunk_s=0.005)
    h, u = await spec_room(tts, {SRC: "مرحبًا سيدة دمير. أنا الدكتور فيبر."})
    h.p.spec.on_text(u, SRC, "de")
    await until(lambda: len([e for e in tts.log if e[0] == "end"]) == 2)  # both clauses made ahead
    marks: dict = {}
    assert await play(h, "مرحبًا سيدة دمير.", marks=marks) == 2  # the final kept the first clause: cached
    assert await play(h, "أنا الطبيب فيبر.", marks=marks) == 2  # its tail changed: synthesised now
    assert marks == {"voice_spec": 1, "voice_live": 1}
    assert [x for e, x, _, _ in tts.log if e == "start"] == ["مرحبًا سيدة دمير.", "أنا الدكتور فيبر.",
                                                              "أنا الطبيب فيبر."]
    await h.p.drain()


async def test_other_voice_args_are_a_miss():
    tts = Slow(n=1, chunk_s=0.001)
    h, u = await spec_room(tts, {SRC: "مرحبًا سيدة دمير."})
    h.p.spec.on_text(u, SRC, "de")
    await until(lambda: any(e == "end" for e, *_ in tts.log))
    marks: dict = {}
    n = 0
    async for _ in h.p.spec.synthesize(tts, "مرحبًا سيدة دمير.", "ar", marks=marks, rate=2.0):  # another pace
        n += 1
    assert n == 1 and marks == {"voice_live": 1}
    await h.p.drain()


async def test_a_resume_drops_the_voice_made_ahead():
    tts = Slow(n=20, chunk_s=0.01)
    h, u = await spec_room(tts, {SRC: "مرحبًا سيدة دمير. أنا الدكتور فيبر."})
    h.p.spec.on_text(u, SRC, "de")
    await until(lambda: tts.running)  # the first clause is being made
    tasks = [v.task for v in h.p.spec._voices.values()]
    h.p.spec.drop(u.id)  # speech_start during the end-of-turn wait
    assert h.p.spec._voices == {} and h.p.spec._specs == {}
    await asyncio.sleep(0.02)
    assert all(t.done() for t in tasks) and not tts.running
    await h.p.drain()


async def test_burn_drops_every_cached_audio():
    tts = Slow(n=1, chunk_s=0.001)
    h, u = await spec_room(tts, {SRC: "مرحبًا سيدة دمير."})
    h.p.spec.on_text(u, SRC, "de")
    await until(lambda: any(e == "end" for e, *_ in tts.log))
    assert h.p.spec._voices
    h.p._forget_voices()  # burn / session end / close
    assert h.p.spec._voices == {} and h.p.spec._specs == {}
    await h.p.drain()


async def test_speculative_synthesis_waits_while_a_live_clause_is_made():
    tts = Slow(n=5, chunk_s=0.02)
    h, u = await spec_room(tts, {SRC: "مرحبًا سيدة دمير."})
    live = asyncio.create_task(play(h, "Live clause."))
    await until(lambda: tts.running)
    h.p.spec.on_text(u, SRC, "de")
    await live
    await until(lambda: any(e == "end" and x == "مرحبًا سيدة دمير." for e, x, *_ in tts.log))
    assert tts.at("start", "مرحبًا سيدة دمير.") >= tts.at("end", "Live clause.")  # never beside it
    assert tts.max_running == 1
    await h.p.drain()


async def test_speculative_synthesis_stops_when_a_live_clause_starts_and_is_made_again_if_needed():
    tts = Slow(n=10, chunk_s=0.02)
    h, u = await spec_room(tts, {SRC: "مرحبًا سيدة دمير."})
    h.p.spec.on_text(u, SRC, "de")
    await until(lambda: tts.running)  # the speculative clause is being made
    await asyncio.sleep(0.03)
    t_live = time.monotonic()
    assert await play(h, "Live clause.") == 10  # a live clause in the room
    assert tts.at("end", "مرحبًا سيدة دمير.") < t_live + 0.03  # it stopped at the next chunk
    marks: dict = {}
    assert await play(h, "مرحبًا سيدة دمير.", marks=marks) == 10  # stopped ahead: made live after all, whole
    assert marks == {"voice_live": 1}
    await h.p.drain()


async def test_the_voice_service_gets_background_priority_for_speculative_clauses_only():
    tts = Slow(n=1, chunk_s=0.001, background=True)
    h, u = await spec_room(tts, {SRC: "مرحبًا سيدة دمير."})
    h.p.spec.on_text(u, SRC, "de")
    await until(lambda: any(e == "end" for e, *_ in tts.log))
    await play(h, "Live clause.")
    kws = {x: kw for e, x, _, kw in tts.log if e == "start"}
    assert kws == {"مرحبًا سيدة دمير.": {"background": True}, "Live clause.": {"background": False}}
    await h.p.drain()


async def test_the_next_clause_is_made_while_one_plays_one_request_at_a_time():
    tts = Slow(n=3, chunk_s=0.01)
    h, _ = await spec_room(tts, {})
    h.p.spec.prepare(tts, "ar", "Erster Satz.", "Zweiter Satz.", {})
    marks: dict = {}
    t0 = time.monotonic()
    await play(h, "Erster Satz.", marks=marks, sink_s=0.03)  # plays slower than it is made
    t_played = time.monotonic()
    assert tts.at("start", "Zweiter Satz.") >= tts.at("end", "Erster Satz.")  # one request at a time ...
    assert tts.at("start", "Zweiter Satz.") < t_played  # ... yet made while the first still plays
    assert await play(h, "Zweiter Satz.", marks=marks) == 3
    assert time.monotonic() - t_played < 0.02  # already there: no gap
    assert marks == {"voice_live": 1, "voice_ahead": 1} and tts.max_running == 1 and t0
    await h.p.drain()


async def test_a_speculative_clause_a_listener_waits_for_goes_at_live_priority():
    tts = Slow(n=4, chunk_s=0.02, background=True)
    h, u = await spec_room(tts, {SRC: "مرحبًا سيدة دمير."})
    live = asyncio.create_task(play(h, "Live clause."))
    await until(lambda: tts.running)
    h.p.spec.on_text(u, SRC, "de")  # waits: a live clause is being made
    await until(lambda: h.p.spec._voices)
    assert await play(h, "مرحبًا سيدة دمير.") == 4  # the turn ended: it is wanted now, not in the background
    await live
    kws = {x: kw for e, x, _, kw in tts.log if e == "start"}
    assert kws["مرحبًا سيدة دمير."] == {"background": False}
    await h.p.drain()


class Busy(Slow):
    """The voice service with live speech elsewhere: every background request is refused (409)."""

    def __init__(self) -> None:
        super().__init__(n=2, chunk_s=0.001, background=True)
        self.refused = 0

    async def _synth_bg(self, text, lang, *, background=False):
        from raum.pipeline.tts_http import VoiceBusy

        if background:
            self.refused += 1
            raise VoiceBusy("busy: live")
        async for c in self._run(text, {"background": background}):
            yield c


async def test_a_refused_speculative_clause_is_made_live_at_the_end_of_turn():
    tts = Busy()
    h, u = await spec_room(tts, {SRC: "مرحبًا سيدة دمير."})
    h.p.spec.on_text(u, SRC, "de")
    await until(lambda: tts.refused)
    await until(lambda: all(v.done.is_set() for v in h.p.spec._voices.values()))
    marks: dict = {}
    assert await play(h, "مرحبًا سيدة دمير.", marks=marks) == 2
    assert marks == {"voice_live": 1} and h.p.spec.voice_hits == 0
    await h.p.drain()
