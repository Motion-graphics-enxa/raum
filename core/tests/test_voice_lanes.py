"""Voice output with several languages at once (GX10 phone test, room vr-2060 / sz-2way): one writer
per ``tts-<lang>`` source, frames at the source's own rate, a failing clause or source never turns
the voice off for the session."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import ClassVar

import numpy as np
import pytest
from livekit import rtc
from pipeline_helpers import DOC, Harness

from raum.events import ParticipantInfo
from raum.pipeline import room as room_mod

# headphones: nobody's mic is paused for echo (C-14), so every overlapping utterance is real speech
DE = DOC.model_copy(update={"audio_out": "headphones"})
RU = ParticipantInfo(id="b-ru01", role="b", lang="ru", audio_out="headphones")
EN = ParticipantInfo(id="b-en01", role="b", lang="en", audio_out="headphones")


class ClauseVoice:
    """A voice engine: three 10 ms chunks per clause, records what it was asked to say."""

    is_mock = False
    simulated = False
    sample_rate = 16000

    def __init__(self, fail: int = 0) -> None:
        self.said: list[tuple[str, str]] = []
        self.fail = fail  # raise on this many calls first

    async def synthesize(self, text, lang):
        self.said.append((lang, text))
        if self.fail:
            self.fail -= 1
            raise RuntimeError("piper hung")
        for _ in range(3):
            await asyncio.sleep(0.002)
            yield np.ones(160, dtype=np.int16)


def three_language_room() -> tuple[Harness, ClauseVoice, dict]:
    h = Harness(participants=[DE, RU, EN])
    voice = ClauseVoice()
    h.engines.tts = voice  # type: ignore[assignment]
    state = {"writers": {}, "violations": [], "chunks": {}}

    async def sink(lang, pcm, rate):  # behaves like AudioSource.capture_frame: one writer at a time
        w = state["writers"]
        if w.get(lang):
            state["violations"].append(lang)
            raise RuntimeError("an RtcError occurred: InvalidState - failed to capture frame")
        w[lang] = True
        try:
            await asyncio.sleep(0.003)
            state["chunks"][lang] = state["chunks"].get(lang, 0) + 1
        finally:
            w[lang] = False

    h.p.tts_sink = sink
    return h, voice, state


async def test_three_languages_overlapping_never_share_a_writer():
    h, voice, state = three_language_room()
    # the German doctor and the Russian patient talk over each other, three times: every utterance
    # goes to two voices, and en gets both of them at once
    for _ in range(3):
        await asyncio.gather(h.say(DOC.id, seconds=0.6), h.say(RU.id, seconds=0.6))
    await h.p.drain()
    assert state["violations"] == []
    langs = {lang for lang, _ in voice.said}
    assert {"ru", "en", "de"} <= langs
    assert all(state["chunks"].get(lang, 0) > 0 for lang in ("ru", "en", "de"))
    assert not any(h.p._voice_paused(lang) for lang in ("ru", "en", "de"))
    assert len(h.of("translation.final")) == 3 * 2 * 2  # text for every utterance and target


async def test_utterances_into_one_language_speak_in_turn():
    h, voice, _ = three_language_room()
    a, b = await asyncio.gather(h.say(DOC.id, seconds=0.6), h.say(RU.id, seconds=0.6))
    await h.p.drain()
    finals = {e.utt: e.data["text"] for e in h.of("translation.final") if e.data["target_lang"] == "en"}
    assert set(finals) == {a, b} and finals[a] != finals[b]
    en = [text for lang, text in voice.said if lang == "en"]
    owners = [next(u for u, full in finals.items() if text in full) for text in en]
    # all clauses of one utterance, then all of the other's: never A B A B on one track
    runs = [u for i, u in enumerate(owners) if i == 0 or owners[i - 1] != u]
    assert sorted(runs) == sorted({a, b}) and len(runs) == 2
    assert not h.p._voice_lanes["en"].locked()


async def test_one_failed_clause_is_dropped_and_the_voice_carries_on():
    h, voice, state = three_language_room()
    voice.fail = 1
    await h.say(DOC.id, seconds=0.6)
    await h.p.drain()
    await h.say(DOC.id, seconds=0.6)
    await h.p.drain()
    assert len(voice.said) > 2 and sum(state["chunks"].values()) > 0
    assert not h.p._voice_paused("ru") and not h.p._voice_paused("en")


async def test_repeated_failures_pause_one_language_briefly_then_retry(monkeypatch):
    monkeypatch.setattr(room_mod, "VOICE_PAUSE_S", 0.2)
    h, voice, state = three_language_room()
    voice.fail = 10**6
    for _ in range(3):
        await h.say(DOC.id, seconds=0.6)
        await h.p.drain()
    assert h.p._voice_paused("ru") or h.p._voice_paused("en")
    n = len(voice.said)
    await h.say(DOC.id, seconds=0.6)
    await h.p.drain()
    assert len(h.of("translation.final")) == 4 * 2  # text never stops
    await asyncio.sleep(0.25)
    voice.fail = 0
    await h.say(DOC.id, seconds=0.6)
    await h.p.drain()
    assert len(voice.said) > n and sum(state["chunks"].values()) > 0  # the voice is back


async def test_a_cancelled_translation_frees_the_voice_lane():
    h, _, _ = three_language_room()
    q: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(h.p._speak_stream("en", q, 0.0))
    await asyncio.sleep(0.01)
    assert h.p._voice_lanes["en"].locked()
    q.put_nowait(None)
    await task
    assert not h.p._voice_lanes["en"].locked()


# ---------------------------------------------------------------- the worker's sink (livekit side)


class RtcError(Exception):
    pass


class FakeSource:
    made: ClassVar[list[FakeSource]] = []
    fail_on: ClassVar[int | None] = None  # fail this frame number once

    def __init__(self, rate, channels, queue_size_ms=0):
        self.sample_rate = rate
        self.frames: list[int] = []
        self.busy = False
        self.closed = False
        self.cleared = 0
        FakeSource.made.append(self)

    def clear_queue(self):
        self.cleared += 1

    async def capture_frame(self, f):
        if self.busy:
            raise RtcError("an RtcError occurred: InvalidState - failed to capture frame")
        if f.sample_rate != self.sample_rate or f.num_channels != 1:
            raise RtcError("an RtcError occurred: InvalidState - sample_rate and num_channels don't match")
        if FakeSource.fail_on is not None and len(self.frames) == FakeSource.fail_on:
            FakeSource.fail_on = None
            raise RtcError("an RtcError occurred: InvalidState - failed to capture frame")
        self.busy = True
        await asyncio.sleep(0.0005)
        self.busy = False
        self.frames.append(f.samples_per_channel)

    async def aclose(self):
        self.closed = True


@pytest.fixture
def agent(monkeypatch):
    from test_pipeline_agent import _agent

    FakeSource.made = []
    FakeSource.fail_on = None
    monkeypatch.setattr(rtc, "AudioSource", FakeSource)
    monkeypatch.setattr(rtc.LocalAudioTrack, "create_audio_track", staticmethod(lambda name, src: SimpleNamespace(name=name)))
    a, _ = _agent()
    published, unpublished = [], []

    async def publish_track(track, opts):
        published.append(track.name)
        return SimpleNamespace(sid=f"TR_{len(published)}")

    async def unpublish_track(sid):
        unpublished.append(sid)

    a.ctx.room.local_participant = SimpleNamespace(publish_track=publish_track, unpublish_track=unpublish_track)
    a._pub, a._unpub = published, unpublished
    return a


async def test_sink_serializes_writers_per_language(agent):
    pcm = np.ones(22050, dtype=np.int16)  # 1 s each, three clauses at once into de, one into ru
    await asyncio.gather(*(agent._tts_sink("de", pcm, 22050) for _ in range(3)), agent._tts_sink("ru", pcm, 22050))
    assert agent._pub == ["tts-de", "tts-ru"]  # one source and track per language
    de = agent.tts["de"].source
    assert sum(de.frames) == 3 * 22050


async def test_sink_resamples_to_the_sources_rate(agent):
    await agent._tts_sink("en", np.ones(2205, dtype=np.int16), 22050)
    await agent._tts_sink("en", np.ones(2400, dtype=np.int16), 24000)  # the http voice after Piper
    src = agent.tts["en"].source
    assert len(FakeSource.made) == 1 and src.sample_rate == 22050
    assert sum(src.frames) == pytest.approx(2 * 2205, abs=8)


async def test_sink_replaces_a_failed_source_and_the_next_clause_plays(agent):
    await agent._tts_sink("de", np.ones(441, dtype=np.int16), 22050)
    FakeSource.fail_on = 1
    with pytest.raises(RtcError, match="failed to capture frame"):
        await agent._tts_sink("de", np.ones(4410, dtype=np.int16), 22050)
    old = FakeSource.made[0]
    assert old.closed and agent._unpub == ["TR_1"] and "de" not in agent.tts
    await agent._tts_sink("de", np.ones(4410, dtype=np.int16), 22050)
    assert agent._pub == ["tts-de", "tts-de"] and sum(agent.tts["de"].source.frames) == 4410


async def test_sink_ignores_empty_chunks(agent):
    await agent._tts_sink("de", np.zeros(0, dtype=np.int16), 22050)
    assert agent._pub == [] and FakeSource.made == []


async def test_stop_cuts_a_voice_mid_write_and_keeps_one_writer(agent):
    """merge.py: the writer of a cut sentence's voice is cancelled mid-clause and the track's queue cleared; the
    frame in flight lands first, so the next clause (another writer) never captures beside it."""
    assert agent.pipeline.tts_stop == agent._tts_stop
    writer = asyncio.create_task(agent._tts_sink("en", np.ones(22050, dtype=np.int16), 22050))  # 1 s: 50 frames
    await asyncio.sleep(0.004)
    src = agent.tts["en"].source
    writer.cancel()  # what merge.py does: cancel the writer, then cut the track
    agent._tts_stop("en")
    nxt = asyncio.create_task(agent._tts_sink("en", np.ones(4410, dtype=np.int16), 22050))  # waits for the lock
    with pytest.raises(asyncio.CancelledError):
        await writer
    await nxt  # FakeSource raises "failed to capture frame" if two captures overlap
    assert src.cleared == 1 and FakeSource.made == [src] and agent._unpub == []
    assert 4410 < sum(src.frames) < 22050 + 4410  # the cut clause stopped early, the next one played whole
    agent._tts_stop("ru")  # no voice in that language yet: nothing to cut
