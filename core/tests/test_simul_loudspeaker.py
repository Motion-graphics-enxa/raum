"""Simultaneous mode for listeners on the phone's speaker (``pipeline.loudspeaker``), the final-revision guard,
German comma commits, the pause not waiting for a queued partial (P2) and partial speculation only on a new
clause (P3). Mock engines, the harness of test_pipeline_simul.py."""

from __future__ import annotations

import numpy as np
import pytest
from pipeline_helpers import Harness, not_implemented
from test_pipeline_simul import (
    DOC_DE,
    DOC_EN,
    PAT_AR_HP,
    PAT_AR_SPK,
    Eot,
    Voice,
    _CountingASR,
    idx,
    line,
    pause,
    room,
    say,
    segs,
    speak,
    until,
    until_ended,
)

from raum.events import Control, ParticipantInfo
from raum.verify import guard


@pytest.fixture
def no_guard(monkeypatch):
    monkeypatch.setattr(guard, "check", not_implemented)
    monkeypatch.setattr(guard, "extract", not_implemented)


PAT_AR_ROOM = ParticipantInfo(id="b-room1", role="b", lang="ar", audio_out="room")  # a speakerphone the room hears
SZ05_AR = ["استمري في تناول 500 ملغ من ميتفورمين مرتين يوميًا،", "صباحًا ومساءً مع الطعام."]
SZ05_850 = "Keep taking 850 mg of Metformin twice a day, in the morning and in the evening with food."
NEW0 = "[ar] Keep taking 850 mg of Metformin twice a day,"  # the mock translator on text it does not know


def held_audio(h: Harness) -> list:
    """Cached audio of held segments (speculate.ahead: not tied to an utterance id)."""
    return [v for v in h.p.spec._voices.values() if v.utt is None]


async def test_loudspeaker_live_hears_the_voice_while_the_doctor_speaks(no_guard):
    """``pipeline.loudspeaker: live`` (gs / sz / kz): a listener on the phone's speaker is served like one on
    headphones: the segments' text and voice while the doctor still speaks."""
    h = room([DOC_EN, PAT_AR_SPK], loudspeaker="live")
    voice = Voice(h)
    h.set_next_line("a", 2)  # sz-05: "Keep taking 500 mg of Metformin twice a day, in the morning ..."
    utt = await say(h, DOC_EN.id, 2.0)
    await h.p.drain()
    assert [f["text"] for f in segs(h, "translation.final", utt)] == SZ05_AR
    assert idx(h, "translation.final", utt, seg=0) < idx(h, "utterance.end", utt)  # text during speech
    assert voice.spoken == SZ05_AR and voice.at[0] < idx(h, "utterance.end", utt)  # voice during speech
    t = h.one("pipeline.timing", utt)
    assert t["simultaneous"] is True and "held" not in t
    assert "stages" in t  # stats.py stages, as consecutive timing has them


async def test_loudspeaker_is_held_where_the_speakers_own_mic_would_hear_it(no_guard):
    """A room speakerphone (echo.py pauses every non-headphone mic for it): the voice never plays over the doctor.
    The text still shows during speech; the voice is made ahead, then played from the cache after the end."""
    h = room([DOC_EN, PAT_AR_SPK, PAT_AR_ROOM], loudspeaker="live")
    assert h.p.simul.plan(DOC_EN.id, "a", "en") == {"ar": True}
    voice = Voice(h)
    h.set_next_line("a", 2)
    utt = await speak(h, DOC_EN.id, 2.0)
    await until(lambda: SZ05_AR[0] in voice.made)  # voiced ahead while the doctor speaks
    assert segs(h, "translation.final", utt) and not voice.spoken  # text shown, nothing played over them
    await h.p.speech_end(DOC_EN.id, None, 300.0)
    await h.p.drain()
    assert voice.spoken == SZ05_AR and voice.made.count(SZ05_AR[0]) == 1  # from the cache: made once
    assert all(at > idx(h, "utterance.end", utt) for at in voice.at)
    t = h.one("pipeline.timing", utt)
    assert t["held"] is True and t["voice_clauses"]["spec"] >= 1 and "revised" not in t


async def test_held_segment_the_final_rewrote_is_voiced_in_the_finals_version(no_guard):
    """The final-revision guard: the final transcript says 850 mg where the committed segment said 500 mg. The
    audio made ahead for "500 mg" never plays; the final's version is translated again and voiced; every
    segment once, in order; the screen gets the new text for that segment."""
    h = room([DOC_EN, PAT_AR_SPK], loudspeaker="held")
    h.set_next_line("a", 2)
    h.engines.asr = _CountingASR(h.engines.asr, final_text=SZ05_850)  # type: ignore[assignment]
    voice = Voice(h)
    utt = await speak(h, DOC_EN.id, 2.4)
    await until(lambda: SZ05_AR[0] in voice.made)  # "500 mg" voiced ahead
    await h.p.speech_end(DOC_EN.id, None, 400.0)  # 400 ms: the final ASR runs (and hears 850)
    await h.p.drain()
    assert h.one("transcript.final", utt)["text"] == SZ05_850
    last = segs(h, "translation.final", utt)[-1]
    assert last["seg"] == 1 and last["segs"] == 2
    assert voice.spoken == [NEW0, last["text"]]  # the stale "500 mg" never reaches the ear; each once, in order
    assert SZ05_AR[0] not in voice.spoken and SZ05_AR[0] in voice.made  # made ahead, never played
    seg0 = [f["text"] for f in segs(h, "translation.final", utt) if f["seg"] == 0]
    assert seg0 == [SZ05_AR[0], NEW0]  # shown at once, then corrected (the app keys segments by seg)
    t = h.one("pipeline.timing", utt)
    assert t["revised"] == 1 and t["held"] is True
    assert not [f for f in h.of("flag", utt) if f.data["id"].endswith("-correction")]  # nothing heard stale
    assert not held_audio(h)  # the stale audio was forgotten, not left in the cache


async def test_live_segment_spoken_before_the_final_rewrote_it_is_corrected_not_repeated(no_guard):
    h = room([DOC_EN, PAT_AR_SPK], loudspeaker="live")
    h.set_next_line("a", 2)
    h.engines.asr = _CountingASR(h.engines.asr, final_text=SZ05_850)  # type: ignore[assignment]
    voice = Voice(h)
    utt = await speak(h, DOC_EN.id, 2.4)
    await until(lambda: SZ05_AR[0] in voice.spoken)  # heard while the doctor spoke
    await h.p.speech_end(DOC_EN.id, None, 400.0)
    await h.p.drain()
    last = segs(h, "translation.final", utt)[-1]
    assert voice.spoken == [SZ05_AR[0], last["text"]]  # each segment once: nothing is said twice
    seg0 = [f["text"] for f in segs(h, "translation.final", utt) if f["seg"] == 0]
    assert seg0[-1] == NEW0  # the screen has the final's version
    flag = next(f.data for f in h.of("flag", utt) if f.data["id"] == f"{utt}-ar-correction")
    assert flag["needs_confirm"] is True  # the listener heard the old one: they are told to check
    assert h.one("pipeline.timing", utt)["revised"] == 1


async def test_no_usable_final_never_plays_held_voice(no_guard):
    h = room([DOC_EN, PAT_AR_SPK], loudspeaker="held")
    voice = Voice(h)
    h.set_next_line("a", 2)
    utt = await speak(h, DOC_EN.id, 2.0)
    await until(lambda: SZ05_AR[0] in voice.made)
    h.p.simul.release(utt)  # the final ASR failed / low confidence: nothing checked it against a final
    await h.p.drain()
    assert not voice.spoken and not held_audio(h)


async def test_burn_drops_held_voice_before_it_plays(no_guard):
    h = room([DOC_EN, PAT_AR_SPK], loudspeaker="held")
    voice = Voice(h)
    h.set_next_line("a", 2)
    utt = await speak(h, DOC_EN.id, 2.0)
    await until(lambda: SZ05_AR[0] in voice.made)
    lanes = list(h.p.simul.lanes(utt).values())
    await h.p.handle_control(Control(action="burn"))
    await h.p.drain()
    assert not voice.spoken and not h.p.spec._voices and not h.p.simul._held
    assert lanes and all(lane.voice.done() for lane in lanes)


async def test_german_commits_at_a_comma_after_a_verb_complete_clause(no_guard):
    """sz-05 in German: "Nehmen Sie zweimal täglich 500 mg Metformin weiter," goes out while the doctor speaks
    (before: German waited for the sentence end, the measured 3-4 s lag)."""
    h = room([DOC_DE, PAT_AR_HP], complete_clauses=False)  # no turn model: the incomplete.py rules decide
    h.set_next_line("a", 2)
    utt = await say(h, DOC_DE.id, 2.2)
    await h.p.drain()
    finals = segs(h, "translation.final", utt)
    assert finals and finals[0]["seg"] == 0
    first = h.of("translation.final", utt)[0]
    assert idx(h, "translation.final", utt, seg=0) < idx(h, "utterance.end", utt)
    assert first.data["text"] == SZ05_AR[0]


# ---------------------------------------------------------------- the pause, speculation (P2, P3)


async def test_pause_does_not_wait_for_a_partial_still_queued_for_its_slot(no_guard):
    """asr.settle waits only for a partial that is transcribing. One still queued for the background slot (box:
    behind speculative MT) is cancelled and the final goes ahead at once."""
    h = room([DOC_DE, PAT_AR_SPK], complete_clauses=False, partial_latency_ms=30)
    h.p.eot = Eot(waits=[0.0])
    utt = await speak(h, DOC_DE.id, 1.5)
    sched = h.p.sched
    n = sched.slots["background"]
    for _ in range(n):  # the background slots are busy elsewhere
        await sched.acquire("background")
    try:
        for _ in range(25):  # 0.5 s more: the next partial is spawned and queues
            await h.p.speech_audio(DOC_DE.id, np.zeros(320, dtype=np.float32))
        u = h.p._active[DOC_DE.id]
        task = u.partial_task
        assert task is not None and not task.done() and u.partial_asr is not task
        await pause(h, DOC_DE.id)
        await until_ended(h, utt)
        assert task.cancelled()
    finally:
        for _ in range(n):
            sched.release("background")
    await h.p.drain()
    assert h.one("transcript.final", utt)["text"] == line("sz-01").text["de"]
    settle = h.one("pipeline.timing", utt)["stages"]["asr.settle"]
    assert settle["queued"] == 1 and settle["wait_ms"] < 50


async def test_growing_partials_speculate_only_on_a_new_clause(no_guard, monkeypatch):
    """10+ growing partials of one turn: speculated when a clause is added (or every 2 s), not every time."""
    from raum.pipeline import speculate

    def turn(every: float) -> tuple[list[int], Harness]:
        monkeypatch.setattr(speculate, "SPEC_EVERY_S", every)
        h = room([DOC_EN, PAT_AR_SPK], partial_every_ms=200)  # sz-05, ~17 words: a partial every 2 words
        h.set_next_line("a", 2)
        n = [0]
        run = h.p.spec._run

        def counted(*a, **kw):
            n[0] += 1
            return run(*a, **kw)

        monkeypatch.setattr(h.p.spec, "_run", counted)
        return n, h

    n, h = turn(2.0)
    utt = await say(h, DOC_EN.id, 2.2)
    await h.p.drain()
    assert len(h.of("transcript.partial", utt)) >= 10
    assert 1 <= n[0] <= 4, n[0]
    assert h.one("translation.final", utt)["text"] == line("sz-05").text["ar"]

    before, h2 = turn(0.0)  # the old behaviour: every complete-looking partial translated again
    await say(h2, DOC_EN.id, 2.2)
    await h2.p.drain()
    assert before[0] >= n[0] + 3, (before[0], n[0])  # 7 vs 3 here
