"""Live draft translation text (draft.py): the stable part of the partials translated while the speaker talks,
``translation.draft`` in place, never voiced. Mock engines, no LiveKit, GPU or network.

Two ways in: the room's real partial path (speech fed in 20 ms frames, every partial awaited, as in
test_pipeline_simul.py), and ``p.draft.on_partial`` fed by hand with a gated translator, so the order of partials
and MT completions is exact."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import itertools

import pytest
from pipeline_helpers import Harness, fake_check, fake_extract, not_implemented
from test_pipeline_simul import DOC_DE, PAT_AR_HP, PAT_AR_SPK, Voice, config, idx, room, say, speak, until

from raum.config import PipelineConfig
from raum.events import ParticipantInfo, make_event
from raum.pipeline import draft as draft_mod
from raum.pipeline import voicegate
from raum.pipeline.base import AsrResult
from raum.pipeline.draft import Drafts, stable_prefix
from raum.pipeline.room import SPEC_DEFER_S
from raum.pipeline.translate import MockTranslator
from raum.scheduler import Scheduler
from raum.verify import guard


@pytest.fixture
def no_guard(monkeypatch):
    monkeypatch.setattr(guard, "check", not_implemented)
    monkeypatch.setattr(guard, "extract", not_implemented)


_prio: contextvars.ContextVar[str | None] = contextvars.ContextVar("prio", default=None)


class _Spy:
    """The mock translator, recording each request (and the scheduler slot it ran in) and how many ran at once."""

    name = "spy"

    def __init__(self, h: Harness) -> None:
        self.h = h
        self.inner = MockTranslator({"first_token_ms": 1, "token_ms": 0})
        self.reqs: list = []
        self.partials_at: list[int] = []  # transcript.partial events out when each request started
        self.ended_at: list[int] = []  # utterance.end events out when each request started (0: while speaking)
        self.prios: list[str | None] = []

    async def stream(self, req):
        self.reqs.append(req)
        self.partials_at.append(len(self.h.of("transcript.partial")))
        self.ended_at.append(len(self.h.of("utterance.end")))
        self.prios.append(_prio.get())
        async for d in self.inner.stream(req):
            yield d


class _Gated:
    """Each call waits for the test to ``release`` it, then answers ``<source>``; counts calls in flight."""

    name = "gated"

    def __init__(self) -> None:
        self.calls: list[tuple[str, asyncio.Event]] = []
        self.running = 0
        self.max_running = 0
        self.gate = True

    async def stream(self, req):
        ev = asyncio.Event()
        if not self.gate:
            ev.set()
        self.calls.append((req.text, ev))
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            await ev.wait()
            yield f"<{req.text}>"
        finally:
            self.running -= 1

    def release(self, i: int) -> None:
        self.calls[i][1].set()


def drafts(h: Harness, utt: str | None = None) -> list[dict]:
    return [e.data for e in h.of("translation.draft", utt)]


def harness(participants=None, **pipeline) -> Harness:
    """Consecutive (``simultaneous: off``) unless asked; drafts unspaced (the speech here runs ~7x real time)."""
    cfg = config()
    for k, v in {"draft_every_ms": 0.0, "simultaneous": "off", **pipeline}.items():
        setattr(cfg.pipeline, k, v)
    return Harness(participants=participants or [DOC_DE, PAT_AR_SPK], cfg=cfg)


async def open_utt(h: Harness, speaker: str = DOC_DE.id):
    await h.p.speech_start(speaker)
    return h.p._active[speaker]


def feed(h: Harness, u, text: str, speech_s: float = 2.0, conf: float = 0.95) -> None:
    h.p.draft.on_partial(u, AsrResult(text=text, lang="de", conf=conf, has_conf=True), speech_s)


# ---------------------------------------------------------------- the stable part


def test_stable_part_is_the_word_aligned_common_prefix_of_the_last_two_partials():
    assert stable_prefix(None, ["Guten", "Tag", "Frau"]) == ["Guten", "Tag"]  # one partial: minus its last word
    assert stable_prefix(None, ["Guten"]) == []
    assert stable_prefix(["Guten", "Tag", "Frau", "Demir"], ["Guten", "Tag", "Frau", "Demir", "ich"]) == \
        ["Guten", "Tag", "Frau", "Demir"]
    # word-aligned: a word that gained its punctuation (or was revised) ends the stable part
    assert stable_prefix(["Guten", "Tag", "Frau"], ["Guten", "Tag,", "Frau", "Demir."]) == ["Guten"]
    assert stable_prefix(["Sie", "nehmen", "50"], ["Sie", "nehmen", "500", "mg"]) == ["Sie", "nehmen"]
    assert stable_prefix(["a", "b"], ["a", "b"]) == ["a", "b"]


def test_draft_event_shape_and_defaults():
    env = make_event("translation.draft", "sz-1", {"target_lang": "ar", "text": "..", "seq": 1}, utt="u-0001")
    assert env.payload().seg is None
    assert make_event("translation.draft", "sz-1", {"target_lang": "ar", "text": "..", "seq": 2, "seg": 1}).data["seg"] == 1
    c = PipelineConfig()
    assert c.draft_text is True and c.draft_after_ms == draft_mod.DRAFT_AFTER_MS == 1500.0
    assert c.draft_every_ms == draft_mod.DRAFT_EVERY_MS == 600.0
    from raum.events import CONTENT_EVENTS
    assert "translation.draft" not in CONTENT_EVENTS  # transient, like the partials: never stored


# ---------------------------------------------------------------- the room's partial path, mock engines


async def test_drafts_start_after_1_5_s_of_speech_and_go_out_in_place(no_guard):
    cfg = config(word_ms=200)  # sz-01, 14 words in 2.8 s: each partial (every 0.5 s) reveals more of it
    cfg.pipeline.draft_every_ms = 0.0
    cfg.pipeline.speculate = False  # only the drafts and the turn translate
    cfg.pipeline.simultaneous = "off"  # consecutive: the draft is the whole stable text
    h = Harness(participants=[DOC_DE, PAT_AR_SPK], cfg=cfg)
    spy = h.engines.translate = _Spy(h)  # type: ignore[assignment]
    utt = await say(h, DOC_DE.id, 3.0)
    await h.p.drain()
    out = drafts(h, utt)
    assert out, "no drafts"
    assert spy.partials_at[0] == 3  # the first draft MT: on the partial at 1.5 s of speech, not at 0.5 / 1.0 s
    assert [d["seq"] for d in out] == list(range(1, len(out) + 1))  # in order, replaced in place by the app
    assert {d["target_lang"] for d in out} == {"ar"} and all("seg" not in d or d["seg"] is None for d in out)
    assert out[0]["text"] == "مرحبًا سيدة دمير. أنا"  # the mock line's span: "Guten Tag, Frau Demir. Ich"
    end = idx(h, "utterance.end", utt)
    assert all(i < end for i, e in enumerate(h.events) if e.type == "translation.draft")  # never after the end
    assert h.one("translation.final", utt)["text"]  # the final translation replaces the draft
    assert h.p.draft._utts == {}


@pytest.mark.parametrize("off", ["box", "mode", "delay", None])
async def test_the_switch_means_no_draft_mt_at_all(no_guard, off):
    """raum.yaml or the mode's ``pipeline.draft_text: false`` (or a start delay longer than the turn): not one MT
    call while the speaker talks, no draft event; the turn itself is unchanged."""
    h = harness(speculate=False, **({"draft_text": False} if off == "box" else {}),
                **({"draft_after_ms": 10_000.0} if off == "delay" else {}))
    if off == "mode":
        h.p.mode = h.p.mode.model_copy(deep=True)
        h.p.mode.pipeline.draft_text = False
    spy = h.engines.translate = _Spy(h)  # type: ignore[assignment]
    utt = await say(h, DOC_DE.id, 2.0)
    await h.p.drain()
    while_speaking = [r.text for r, ended in zip(spy.reqs, spy.ended_at, strict=True) if not ended]
    if off is None:
        assert drafts(h, utt) and while_speaking  # the same room with the defaults: drafts
    else:
        assert not drafts(h, utt) and while_speaking == [], off
        assert [r.text for r in spy.reqs] == [h.one("transcript.final", utt)["text"]]  # the turn's MT only
    assert h.one("translation.final", utt)["text"]


async def test_low_confidence_partials_are_never_drafted(no_guard):
    h = harness()
    u = await open_utt(h)
    feed(h, u, "Guten Tag Frau Demir", conf=0.3)
    feed(h, u, "Guten Tag Frau Demir ich", conf=0.3)
    await h.p.drain()
    assert not drafts(h)


# ---------------------------------------------------------------- newest wins, in order, spacing


async def test_one_mt_in_flight_the_newest_text_wins_and_a_stale_draft_never_goes_out(no_guard):
    h = harness()
    gated = h.engines.translate = _Gated()  # type: ignore[assignment]
    u = await open_utt(h)
    feed(h, u, "Guten Tag Frau", speech_s=1.5)  # the first partial: minus its last word
    await until(lambda: len(gated.calls) == 1)
    assert gated.calls[0][0] == "Guten Tag"
    feed(h, u, "Guten Tag Frau Demir", speech_s=2.0)  # stable "Guten Tag Frau": waits
    feed(h, u, "Guten Tag Frau Demir ich", speech_s=2.5)  # stable "Guten Tag Frau Demir": replaces it
    await asyncio.sleep(0.02)
    assert len(gated.calls) == 1  # one draft MT in flight per (utt, target)
    gated.release(0)
    await until(lambda: len(gated.calls) == 2)
    assert gated.calls[1][0] == "Guten Tag Frau Demir"  # the newest; "Guten Tag Frau" was never translated
    assert drafts(h) == [{"target_lang": "ar", "text": "<Guten Tag>", "seq": 1, "seg": None}]
    # the ASR revises an earlier word while that MT runs: its draft is stale and never goes out
    feed(h, u, "Guten Morgen Frau Demir ich bin", speech_s=3.0)  # stable "Guten"
    gated.release(1)
    await until(lambda: len(gated.calls) == 3)
    assert gated.calls[2][0] == "Guten"
    gated.release(2)
    await until(lambda: len(drafts(h)) == 2)
    assert drafts(h)[1] == {"target_lang": "ar", "text": "<Guten>", "seq": 2, "seg": None}
    # the same stable text again: nothing new
    feed(h, u, "Guten Morgen Frau Demir ich bin", speech_s=3.5)  # identical partials: all of it is stable
    await until(lambda: len(gated.calls) == 4)
    gated.release(3)
    await until(lambda: len(drafts(h)) == 3)
    feed(h, u, "Guten Morgen Frau Demir ich bin", speech_s=4.0)
    await asyncio.sleep(0.02)
    assert len(gated.calls) == 4 and gated.max_running == 1
    seqs = [d["seq"] for d in drafts(h)]
    assert seqs == sorted(seqs) == [1, 2, 3]


async def test_draft_mts_of_one_utterance_and_language_are_spaced(no_guard):
    h = harness(draft_every_ms=150.0)
    gated = h.engines.translate = _Gated()  # type: ignore[assignment]
    gated.gate = False
    starts: list[float] = []
    inner = gated.stream

    def stream(req):
        starts.append(asyncio.get_running_loop().time())
        return inner(req)

    gated.stream = stream  # type: ignore[method-assign]
    u = await open_utt(h)
    words = ["Guten", "Tag", "Frau", "Demir", "ich", "bin", "Doktor", "Weber", "was"]
    for n in range(3, len(words) + 1):
        feed(h, u, " ".join(words[:n]), speech_s=1.5 + n / 10)
        await asyncio.sleep(0.03)  # partials every 30 ms: faster than the spacing
    await h.p.drain()
    assert len(starts) >= 2
    assert all(b - a >= 0.14 for a, b in itertools.pairwise(starts)), starts
    assert len(starts) < len(words) - 2  # spaced: some texts were superseded before their turn
    assert drafts(h)[-1]["text"] == "<" + " ".join(words[:-1]) + ">"  # the newest stable text came last


# ---------------------------------------------------------------- end, drop, priority, voice


async def test_no_draft_after_the_utterance_ends_and_an_mt_in_flight_is_cancelled(no_guard):
    h = harness()
    gated = h.engines.translate = _Gated()  # type: ignore[assignment]
    u = await open_utt(h)
    feed(h, u, "Guten Tag Frau Demir")
    await until(lambda: len(gated.calls) == 1)
    gated.gate = False  # the turn's own translation runs freely
    await h.p.speech_end(DOC_DE.id, None, 300.0)
    gated.release(0)
    await h.p.drain()
    assert not drafts(h) and h.p.draft._utts == {}
    assert h.of("translation.final")  # the final came, the draft in flight never did
    feed(h, u, "Guten Tag Frau Demir ich")  # a late partial of the closed utterance: nothing
    await h.p.drain()
    assert not drafts(h)


@pytest.mark.parametrize("stop", ["off_record", "burn", "session_end"])
async def test_off_the_record_burn_and_session_end_stop_the_drafts(no_guard, stop):
    from raum.events import Control, SessionState

    h = harness()
    gated = h.engines.translate = _Gated()  # type: ignore[assignment]
    u = await open_utt(h)
    feed(h, u, "Guten Tag Frau Demir")
    await until(lambda: len(gated.calls) == 1)
    if stop == "burn":
        await h.p.handle_control(Control(action="burn"))
    elif stop == "off_record":
        h.p._set_off_record(True)
    else:
        h.p.set_state(SessionState(off_record=False, sealed=False, participants=[], mode="sprechzimmer", status="ended"))
    gated.release(0)
    await asyncio.sleep(0.02)
    assert not drafts(h) and h.p.draft._utts == {}


async def test_drafts_use_the_background_slot_after_the_voice_gate(no_guard, monkeypatch):
    h = harness()
    spy = h.engines.translate = _Spy(h)  # type: ignore[assignment]
    sched = h.p.scheduler = Scheduler(live_slots=1, check_slots=1, background_slots=1)
    orig = sched.slot
    order: list[str] = []

    @contextlib.asynccontextmanager
    async def slot(prio):
        async with orig(prio):
            order.append(f"slot:{prio}")
            tok = _prio.set(prio)
            try:
                yield
            finally:
                _prio.reset(tok)

    async def quiet(max_s):
        order.append(f"quiet:{max_s}")
        return 0.0

    sched.slot = slot  # type: ignore[method-assign]
    monkeypatch.setattr(voicegate, "quiet", quiet)
    u = await open_utt(h)
    feed(h, u, "Guten Tag Frau Demir")
    await until(lambda: len(drafts(h)) == 1)
    assert spy.prios == ["background"]
    assert order == [f"quiet:{SPEC_DEFER_S}", "slot:background"]  # live voice first, then a background slot


@pytest.mark.parametrize("listener,simultaneous", [(PAT_AR_SPK, "off"), (PAT_AR_HP, "auto"), (PAT_AR_SPK, "auto")])
async def test_drafts_are_never_voiced_or_checked(monkeypatch, listener, simultaneous):
    """The same utterance with drafts on and off: the same clauses voiced, the same texts fact-checked, the same
    flags (consecutive; simultaneous on headphones; simultaneous live on the loudspeaker)."""
    checked: list[str] = []

    def check(src, src_lang, tgt, tgt_lang, **kw):
        checked.append(tgt)
        return fake_check(src, src_lang, tgt, tgt_lang, **kw)

    monkeypatch.setattr(guard, "check", check)
    monkeypatch.setattr(guard, "extract", fake_extract)
    runs = []
    for on in (True, False):
        checked.clear()
        h = room([DOC_DE, listener], word_ms=200)
        h.cfg.pipeline.draft_text, h.cfg.pipeline.draft_every_ms = on, 0.0
        h.cfg.pipeline.simultaneous = simultaneous
        h.cfg.pipeline.speculate = False  # no voice made ahead from partials: deterministic
        voice = Voice(h)
        utt = await say(h, DOC_DE.id, 3.0)
        await h.p.drain()
        assert bool(drafts(h, utt)) is on
        runs.append((sorted(voice.made), sorted(checked), [e.data.get("kind") for e in h.of("flag", utt)]))
    assert runs[0] == runs[1] and runs[0][0], runs


# ---------------------------------------------------------------- simultaneous mode


async def test_with_committed_segments_the_draft_is_only_the_uncommitted_tail(no_guard):
    h = room([DOC_DE, PAT_AR_HP])  # headphones: simultaneous, every clause complete (sentence ends commit)
    h.cfg.pipeline.draft_every_ms = 0.0
    spy = h.engines.translate = _Spy(h)  # type: ignore[assignment]
    utt = await speak(h, DOC_DE.id, 1.5)  # still talking: the lanes stay open (no drain before the end)
    lane = h.p.simul.lanes(utt)["ar"]
    assert [s.src for s in lane.segs] == ["Guten Tag, Frau Demir.", "Ich bin Dr. Weber."]
    # the 1.5 s round came while segment 1 was being translated (the live slot): skipped, no gap on screen
    assert not drafts(h, utt) and h.p.draft.skipped
    await until(lambda: all(s.text for s in lane.segs))
    u = h.p._active[DOC_DE.id]
    feed(h, u, h.of("transcript.partial", utt)[-1].data["text"])  # the next partial (the same words): a round
    await until(lambda: bool(drafts(h, utt)))
    d = drafts(h, utt)[-1]
    assert d["seg"] == 2 and d["text"] == "ما الذي أتى بكِ إليّ اليوم؟"  # the tail after the committed sentences
    req = spy.reqs[-1]
    assert req.text == "Was führt Sie heute zu mir?" and req.segment  # a segment request, history behind it
    assert req.earlier == [(s.src, s.text) for s in lane.segs]
    await h.p.speech_end(DOC_DE.id, None, 300.0)
    await h.p.drain()
    finals = [e.data for e in h.of("translation.final", utt)]
    assert finals[-1].get("segs") is not None  # the segments and the tail closed the utterance as usual
    assert all(i < idx(h, "utterance.end", utt) for i, e in enumerate(h.events) if e.type == "translation.draft")


async def test_voice_lock_turns_drafts_off(no_guard):
    from types import SimpleNamespace

    h = harness()
    d = Drafts(h.p)
    assert d.enabled
    h.p.voicelock = SimpleNamespace(enabled=True)  # type: ignore[assignment]  F-10: admitted after the final ASR
    assert not d.enabled


async def test_drafts_come_last_among_background_work(no_guard):
    """Two background slots, one busy (a partial ASR, a speculation): the draft waits, so the next partial never
    queues behind a draft MT; it starts once the slot is free."""
    h = harness()
    spy = h.engines.translate = _Spy(h)  # type: ignore[assignment]
    sched = h.p.scheduler = Scheduler(live_slots=1, check_slots=1, background_slots=2)
    await sched.acquire("background")
    u = await open_utt(h)
    feed(h, u, "Guten Tag Frau Demir")
    await asyncio.sleep(0.1)
    assert not spy.reqs and sched.running["background"] == 1  # the last free slot stays for the next partial
    assert h.p.draft.skipped == {"background": 1}  # skipped, not queued
    sched.release("background")
    feed(h, u, "Guten Tag Frau Demir ich")  # the next partial: its round
    await until(lambda: len(drafts(h)) == 1)
    assert spy.prios == [None] and sched.running["background"] == 0


async def test_one_draft_mt_per_room_for_all_languages(no_guard):
    tr = ParticipantInfo(id="b-tr1", role="b", lang="tr")
    h = harness([DOC_DE, PAT_AR_SPK, tr])
    gated = h.engines.translate = _Gated()  # type: ignore[assignment]
    u = await open_utt(h)
    feed(h, u, "Guten Tag Frau Demir")
    await until(lambda: len(gated.calls) == 1)
    await asyncio.sleep(0.05)
    assert len(gated.calls) == 1 and h.p.draft.skipped.get("draft") == 1  # the other language skips its round
    gated.release(0)
    await until(lambda: len(drafts(h)) == 1)
    feed(h, u, "Guten Tag Frau Demir")  # the next partial: the language that waited
    await until(lambda: len(gated.calls) == 2)
    gated.release(1)
    await until(lambda: len(drafts(h)) == 2)
    assert {d["target_lang"] for d in drafts(h)} == {"ar", "tr"} and gated.max_running == 1


async def test_live_work_skips_the_round_and_preempts_a_draft_mt(no_guard):
    h = harness()
    gated = h.engines.translate = _Gated()  # type: ignore[assignment]
    sched = h.p.scheduler = Scheduler(live_slots=2, check_slots=1, background_slots=2)
    u = await open_utt(h)
    await sched.acquire("live")  # a committed segment's MT (or the final's)
    feed(h, u, "Guten Tag Frau Demir")
    await asyncio.sleep(0.05)
    assert not gated.calls and h.p.draft.skipped == {"live": 1}
    sched.release("live")
    feed(h, u, "Guten Tag Frau Demir ich")
    await until(lambda: len(gated.calls) == 1)
    await sched.acquire("live")  # live work starts while the draft MT runs: the draft MT is cancelled at once
    await until(lambda: gated.running == 0)
    assert h.p.draft.skipped.get("preempted.live") == 1 and sched.running["background"] == 0
    gated.release(0)
    await asyncio.sleep(0.02)
    assert not drafts(h)
    sched.release("live")


async def test_a_voice_or_speculation_in_flight_skips_the_round(no_guard, monkeypatch):
    from types import SimpleNamespace

    h = harness()
    spy = h.engines.translate = _Spy(h)  # type: ignore[assignment]
    u = await open_utt(h)
    h.p.spec._live = 1  # this room's live voice is being synthesised
    feed(h, u, "Guten Tag Frau Demir")
    await asyncio.sleep(0.02)
    h.p.spec._live = 0
    ahead = asyncio.ensure_future(asyncio.sleep(10))
    h.p.spec._cands[(u.id, "ar")] = SimpleNamespace(task=ahead)  # a segment translated / voiced ahead
    feed(h, u, "Guten Tag Frau Demir ich")
    await asyncio.sleep(0.02)
    ahead.cancel()
    del h.p.spec._cands[(u.id, "ar")]
    monkeypatch.setattr(voicegate, "busy", lambda now=None: True)  # a live voice in another room of the box
    feed(h, u, "Guten Tag Frau Demir ich bin")
    await asyncio.sleep(0.02)
    assert not spy.reqs and h.p.draft.skipped == {"voice": 2, "spec": 1}
    monkeypatch.setattr(voicegate, "busy", lambda now=None: False)
    feed(h, u, "Guten Tag Frau Demir ich bin Doktor")
    await until(lambda: len(drafts(h)) == 1)
