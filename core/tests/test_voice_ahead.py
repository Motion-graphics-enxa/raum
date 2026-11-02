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
    h.p.spec.on_text(u, SRC, "de", slot="live")
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
    h.p.spec.on_text(u, SRC, "de", slot="live")
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
    h.p.spec.on_text(u, SRC, "de", slot="live")
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
    h.p.spec.on_text(u, SRC, "de", slot="live")
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
    h.p.spec.on_text(u, SRC, "de", slot="live")
    await live
    await until(lambda: any(e == "end" and x == "مرحبًا سيدة دمير." for e, x, *_ in tts.log))
    assert tts.at("start", "مرحبًا سيدة دمير.") >= tts.at("end", "Live clause.")  # never beside it
    assert tts.max_running == 1
    await h.p.drain()


async def test_speculative_synthesis_stops_when_a_live_clause_starts_and_is_made_again_if_needed():
    tts = Slow(n=10, chunk_s=0.02)
    h, u = await spec_room(tts, {SRC: "مرحبًا سيدة دمير."})
    h.p.spec.on_text(u, SRC, "de", slot="live")
    await until(lambda: tts.running)  # the speculative clause is being made
    await asyncio.sleep(0.03)
    t_live = time.monotonic()
    assert await play(h, "Live clause.") == 10  # a live clause in the room
    assert tts.at("end", "مرحبًا سيدة دمير.") < t_live + 0.05  # it stopped at the next chunk, not 0.2 s later
    marks: dict = {}
    assert await play(h, "مرحبًا سيدة دمير.", marks=marks) == 10  # stopped ahead: made live after all, whole
    assert marks == {"voice_live": 1}
    await h.p.drain()


async def test_the_voice_service_gets_background_priority_for_speculative_clauses_only():
    tts = Slow(n=1, chunk_s=0.001, background=True)
    h, u = await spec_room(tts, {SRC: "مرحبًا سيدة دمير."})
    h.p.spec.on_text(u, SRC, "de", slot="live")
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
    h.p.spec.on_text(u, SRC, "de", slot="live")  # waits: a live clause is being made
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
    h.p.spec.on_text(u, SRC, "de", slot="live")
    await until(lambda: tts.refused)
    await until(lambda: all(v.done.is_set() for v in h.p.spec._voices.values()))
    marks: dict = {}
    assert await play(h, "مرحبًا سيدة دمير.", marks=marks) == 2
    assert marks == {"voice_live": 1} and h.p.spec.voice_hits == 0
    await h.p.drain()


class Counting(Fixed):
    def __init__(self, table):
        super().__init__(table)
        self.calls: list[str] = []

    async def stream(self, req):
        self.calls.append(req.text)
        async for d in super().stream(req):
            yield d


SRC1 = "Guten Tag, Frau Demir. Ich"


async def test_a_newer_partial_adopts_the_clause_being_made_instead_of_making_it_again():
    # box log (sz-5122): every new partial restarted the same 36-char clause, cancelled 0.4 s later, 3x per turn
    tts = Slow(n=10, chunk_s=0.01)
    h, u = await spec_room(tts, {SRC1: "مرحبًا سيدة دمير. أنا", SRC: "مرحبًا سيدة دمير. أنا الدكتور فيبر."})
    h.p.spec.on_text(u, SRC1, "de")  # a partial: its first phrase
    await until(lambda: tts.running)
    h.p.spec.on_text(u, SRC, "de", slot="live")  # the pause's early final: a newer text, the same first clause
    await until(lambda: len([e for e in tts.log if e[0] == "end"]) == 2)
    starts = [x for e, x, _, _ in tts.log if e == "start"]
    assert starts == ["مرحبًا سيدة دمير.", "أنا الدكتور فيبر."]  # made once, carried over
    marks: dict = {}
    assert await play(h, "مرحبًا سيدة دمير.", marks=marks) == 10  # whole, never cut
    assert marks == {"voice_spec": 1}
    await h.p.drain()


async def test_a_superseded_clause_the_newer_text_does_not_say_is_stopped():
    tts = Slow(n=30, chunk_s=0.01)
    h, u = await spec_room(tts, {SRC1: "أهلاً سيدة دمير. أنا", SRC: "مرحبًا سيدة دمير. أنا الدكتور فيبر."})
    h.p.spec.on_text(u, SRC1, "de")
    await until(lambda: tts.running)
    h.p.spec.on_text(u, SRC, "de", slot="live")
    await until(lambda: any(e == "end" and x == "أهلاً سيدة دمير." for e, x, *_ in tts.log))
    assert tts.at("end", "أهلاً سيدة دمير.") - tts.at("start", "أهلاً سيدة دمير.") < 0.2  # stopped, not made whole
    await h.p.drain()


async def test_a_partial_voices_its_first_phrase_and_the_pause_the_rest_without_translating_again():
    tts = Slow(n=1, chunk_s=0.001)
    tr = Counting({SRC: "مرحبًا سيدة دمير. أنا الدكتور فيبر."})
    h, u = await spec_room(tts, {})
    h.engines.translate = tr  # type: ignore[assignment]
    h.p.spec.on_text(u, SRC, "de")  # a partial that reads as finished
    await until(lambda: h.p.spec._specs and next(iter(h.p.spec._specs.values())).done)
    await h.p.drain()
    assert [x for e, x, _, _ in tts.log if e == "start"] == ["مرحبًا سيدة دمير."]  # the GPU stays for live work
    h.p.spec.on_text(u, SRC, "de", slot="live")  # the pause says the same text: the rest is voiced now
    await until(lambda: len([e for e in tts.log if e[0] == "end"]) == 2)
    assert tr.calls == [SRC]  # one translation
    await h.p.drain()


async def test_no_partial_is_voiced_in_an_own_voice_whose_sample_still_grows():
    tts = Slow(n=1, chunk_s=0.001)
    h, u = await spec_room(tts, {SRC: "مرحبًا سيدة دمير. أنا الدكتور فيبر."})
    h.p._own_voice = lambda speaker: True  # type: ignore[method-assign]
    h.p.spec.on_text(u, SRC, "de")
    await until(lambda: h.p.spec._specs and next(iter(h.p.spec._specs.values())).done)
    await h.p.drain()
    assert tts.log == []  # the turn's voice (with this utterance in its sample) would never match it
    h.p.spec_voice = "all"
    h.p.spec.on_text(u, SRC, "de", slot="live")  # the pause: the sample is as the turn will use it
    await until(lambda: len([e for e in tts.log if e[0] == "end"]) == 2)
    await h.p.drain()


async def test_the_voice_sample_is_taken_at_the_pause_and_the_turn_uses_the_same_one():
    h, u = await spec_room(Slow(n=1, chunk_s=0.001), {})
    h.p._own_voice = lambda speaker: True  # type: ignore[method-assign]
    audio = (np.random.default_rng(0).standard_normal(16000 * 4) * 0.1).astype(np.float32)
    ref = h.p._grow_ref(u, audio, 250.0, early=True)  # _hold: the pause's silence trimmed
    assert ref is not None and abs(ref.seconds - 3.75) < 0.01
    wav = ref.wav()
    voice = h.p._voice_args(u, audio, 800.0, [], "Guten Tag, Frau Demir.")  # the turn: nothing new to add
    assert voice.ref is ref and ref.wav() == wav  # the voice made ahead used exactly this sample
    more = np.concatenate([audio, audio[:16000]])  # the speaker went on after the pause
    h.p._voice_args(u, more, 250.0, [], "Guten Tag, Frau Demir. Ich bin")
    assert abs(ref.seconds - 4.75) < 0.01  # only the new part
    await h.p.drain()


async def test_pipeline_timing_has_the_stages_wait_vs_work(no_guard):  # noqa: F811
    h = room([DOC_DE, PAT_AR_SPK], complete_clauses=False, final_reveal=True)
    h.p.eot = Eot(waits=[0.25])
    Voice(h)
    utt = await speak(h, DOC_DE.id, 0.6)
    await pause(h, DOC_DE.id)
    await until_ended(h, utt)
    await h.p.drain()
    st = h.one("pipeline.timing", utt)["stages"]
    assert st["slot.live"]["n"] >= 1 and "wait_ms" in st["slot.live"]  # the turn's translation slot
    assert "start_ms" in st["spec.mt"] and "first_ms" in st["spec.mt"]  # the speculation at the pause
    assert st["voice"]["ready"] in (0.0, 1.0) and "claim_ms" in st["voice"] and "first_ms" in st["voice"]
    assert st["spec.voice.spec"]["n"] == 2  # both clauses made ahead
    assert all(isinstance(v, float | int) for d in st.values() for v in d.values())  # numbers only


async def test_the_clause_made_ahead_that_plays_next_is_made_now_while_the_first_plays():
    # box (sz-5163): the second clause, made ahead, waited for the first clause's live synthesis and then for its
    # own turn to play: a gap between the clauses
    tts = Slow(n=5, chunk_s=0.01, background=True)
    h, u = await spec_room(tts, {SRC: "مرحبًا سيدة دمير. أنا الدكتور فيبر."})
    live = asyncio.create_task(play(h, "Live clause."))  # something live in the room: the speculation waits
    await until(lambda: tts.running)
    h.p.spec.on_text(u, SRC, "de", slot="live")
    await until(lambda: len(h.p.spec._voices) == 2)
    await live
    h.p.spec.prepare(tts, "ar", "Erster Satz.", "أنا الدكتور فيبر.", {}, utt=u.id)  # the second plays next
    await until(lambda: any(e == "start" and x == "أنا الدكتور فيبر." for e, x, *_ in tts.log))
    kws = {x: kw for e, x, _, kw in tts.log if e == "start"}
    assert kws["أنا الدكتور فيبر."] == {"background": False}  # wanted: live priority
    await h.p.drain()


class RefuseOnce(Slow):
    """The voice service refuses the first background request (409) before any audio."""

    def __init__(self) -> None:
        super().__init__(n=3, chunk_s=0.01, background=True)
        self.refused = 0
        self.gate = asyncio.Event()

    async def _synth_bg(self, text, lang, *, background=False):
        from raum.pipeline.tts_http import VoiceBusy

        if background:
            await self.gate.wait()  # in flight until the test lets it answer
            self.refused += 1
            raise VoiceBusy("busy: live")
        async for c in self._run(text, {"background": background}):
            yield c


async def test_a_wanted_clause_refused_before_any_audio_is_made_live_at_once():
    tts = RefuseOnce()
    h, u = await spec_room(tts, {SRC: "مرحبًا سيدة دمير."})
    h.p.spec.on_text(u, SRC, "de", slot="live")
    await until(lambda: h.p.spec._voices)
    marks: dict = {}
    player = asyncio.create_task(play(h, "مرحبًا سيدة دمير.", marks=marks))  # the end of turn claims it ...
    await asyncio.sleep(0.01)
    tts.gate.set()  # ... then the service refuses its background request
    assert await player == 3 and tts.refused == 1
    assert marks == {"voice_spec": 1}  # the same voice, made again as live, not a second request later
    assert [kw for e, _, _, kw in tts.log if e == "start"] == [{"background": False}]
    await h.p.drain()


async def test_a_clause_that_arrives_while_one_plays_is_made_before_its_turn():
    # ops: the lane looked for the next clause only when a clause started; one queued mid-playback was made
    # only when its turn came (a gap of one first-audio time)
    tts = Slow(n=3, chunk_s=0.01)
    h, u = await spec_room(tts, {})
    played: list[float] = []

    async def sink(lang, chunk, rate):
        await asyncio.sleep(0.03)  # plays slower than it is made
        played.append(time.monotonic())

    h.p.tts_sink = sink
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait("Erster Satz.")
    marks: dict = {}
    lane = asyncio.create_task(h.p._speak_stream("ar", q, time.monotonic(), None, marks, utt=u.id))
    await until(lambda: played)  # the first clause is playing
    q.put_nowait("Zweiter Satz.")
    q.put_nowait(None)
    await lane
    assert tts.at("start", "Zweiter Satz.") < played[2]  # made while the first still played
    assert tts.at("start", "Zweiter Satz.") >= tts.at("end", "Erster Satz.")  # one request at a time
    assert marks.get("voice_ahead") == 1 and len(played) == 6
    await h.p.drain()
