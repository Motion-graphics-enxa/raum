"""Echo control per device (C-14) and live participant changes, on RoomPipeline with mock engines."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from pipeline_helpers import DOC, PAT, Harness, not_implemented

from raum.events import Control, ParticipantInfo
from raum.pipeline.echo import ECHO_TAIL_S, EchoControl, paused_for
from raum.verify import guard

HP = ParticipantInfo(id="b-hp01", role="b", lang="ar", audio_out="headphones")
TR = ParticipantInfo(id="b-tr01", role="b", lang="tr")  # audio_out defaults to speaker
TABLE = ParticipantInfo(id="t-table", role="observer", lang="de", device="speakerphone", audio_out="room")


@pytest.fixture(autouse=True)
def no_guard(monkeypatch):
    monkeypatch.setattr(guard, "check", not_implemented)
    monkeypatch.setattr(guard, "extract", not_implemented)


class _Voice:
    """``seconds`` of audio per clause, in one chunk."""

    name = "fake-voice"
    sample_rate = 16000

    def __init__(self, seconds: float = 0.2) -> None:
        self.seconds = seconds

    async def synthesize(self, text, lang):
        yield np.zeros(int(self.seconds * self.sample_rate), dtype=np.int16)


def voiced(participants, seconds: float = 0.2, tail: float = 0.05) -> Harness:
    h = Harness(participants=participants)
    h.engines.tts = _Voice(seconds)  # type: ignore[assignment]
    h.p.echo.tail_s = tail
    played: list[str] = []

    async def sink(lang, pcm, rate):
        played.append(lang)

    h.p.tts_sink = sink
    return h


def states(h: Harness, lang: str | None = None) -> list[dict]:
    return [e.data for e in h.of("voice.state") if lang is None or e.data["target_lang"] == lang]


async def until(cond, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.005)):
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


def test_who_is_paused():
    people = [DOC, PAT, HP, TR]
    assert paused_for("ar", people) == [PAT.id]  # the Arabic speaker device; headphones never
    assert paused_for("tr", people) == [TR.id]
    assert paused_for("de", people) == [DOC.id]
    # a shared room speaker: everyone hears every voice, except the headphones wearer's mic stays open
    assert paused_for("ar", [*people, TABLE]) == [DOC.id, PAT.id, TR.id, TABLE.id]
    assert EchoControl().tail_s == ECHO_TAIL_S == 0.3


def test_echo_bookkeeping_queues_audio_per_language():
    e = EchoControl(tail_s=0.3)
    assert e.play("ar", 1.0, [DOC, PAT], now=10.0) is not None  # starts
    assert e.play("ar", 0.5, [DOC, PAT], now=10.2) is None  # queued behind: ends at 11.5
    assert e.remaining("ar", now=11.0) == pytest.approx(0.8)
    assert e.is_paused(PAT.id, now=11.7) and not e.is_paused(PAT.id, now=11.8)
    assert not e.is_paused(DOC.id, now=10.5)
    assert e.remaining("de") is None


async def test_pause_is_per_device_and_the_next_speaker_keeps_their_first_words():
    # repro from the box: with a room-wide pause, the doctor's next words were dropped while the
    # Arabic voice played on the patient's phone
    h = voiced([DOC, PAT, HP, TR])
    u1 = await h.say(DOC.id)
    await until(lambda: len(states(h)) >= 2)
    assert states(h, "ar")[0] == {"target_lang": "ar", "playing": True, "paused": [PAT.id]}
    assert states(h, "tr")[0] == {"target_lang": "tr", "playing": True, "paused": [TR.id]}
    assert not h.p.mic_open(PAT.id) and not h.p.mic_open(TR.id)
    assert h.p.mic_open(HP.id)  # headphones: never paused
    assert await h.p.speech_start(PAT.id) is None  # the voice from the patient's own speaker is echo

    assert h.p.mic_open(DOC.id)  # the doctor is not paused ...
    u2 = await h.say(DOC.id)  # ... and is heard from the first word
    assert u2 is not None and u2 != u1
    assert h.one("transcript.final", u2)["text"]

    await h.p.drain()
    assert states(h, "ar")[-1] == {"target_lang": "ar", "playing": False, "paused": []}
    assert states(h, "tr")[-1] == {"target_lang": "tr", "playing": False, "paused": []}
    assert h.p.mic_open(PAT.id) and await h.p.speech_start(PAT.id) is not None


async def test_room_speaker_pauses_every_mic_but_headphones():
    h = voiced([DOC, PAT, HP, TABLE])
    await h.say(DOC.id)
    await until(lambda: states(h, "ar"))
    assert states(h, "ar")[0]["paused"] == [DOC.id, PAT.id, TABLE.id]
    assert not h.p.mic_open(DOC.id) and h.p.mic_open(HP.id)
    await h.p.aclose()


async def test_an_utterance_that_started_before_the_pause_runs_to_its_end():
    h = voiced([DOC, PAT], seconds=0.3)
    pat = await h.p.speech_start(PAT.id)  # the patient is already talking ...
    for _ in range(10):
        await h.p.speech_audio(PAT.id, np.zeros(320, dtype=np.float32))
    await h.say(DOC.id)  # ... when the Arabic voice for the doctor's words starts
    await until(lambda: states(h, "ar"))
    assert PAT.id in states(h, "ar")[0]["paused"]
    assert h.p.mic_open(PAT.id)  # not cut: the running utterance keeps its audio
    before = h.p._active[PAT.id].samples
    for _ in range(10):
        await h.p.speech_audio(PAT.id, np.zeros(320, dtype=np.float32))
    assert h.p._active[PAT.id].samples == before + 3200
    await h.p.speech_end(PAT.id, None, 300)
    assert h.one("transcript.final", pat)["text"]
    # once it ended, the patient's mic is paused until the voice is over
    assert not h.p.mic_open(PAT.id) and await h.p.speech_start(PAT.id) is None
    await h.p.drain()
    assert h.p.mic_open(PAT.id)


async def test_switching_to_headphones_mid_playback_unpauses_and_announces_it():
    h = voiced([DOC, PAT])
    await h.say(DOC.id)
    await until(lambda: states(h, "ar"))
    assert not h.p.mic_open(PAT.id)
    h.p.set_participants([DOC, PAT.model_copy(update={"audio_out": "headphones"})])
    await until(lambda: len(states(h, "ar")) >= 2)
    assert states(h, "ar")[1] == {"target_lang": "ar", "playing": True, "paused": []}
    assert h.p.mic_open(PAT.id)
    await h.p.aclose()


async def test_off_record_closes_every_mic_and_no_voice_means_no_state():
    h = Harness()
    assert h.p.mic_open(DOC.id)
    await h.say(DOC.id)  # mock voice: no audio, no voice.state
    await h.p.drain()
    assert states(h) == []
    await h.p.handle_control(Control(action="off_record", value=True))
    assert not h.p.mic_open(DOC.id)


async def test_language_change_applies_from_the_next_utterance():
    h = Harness()
    hints: list[tuple[str, str | None]] = []
    transcribe = h.engines.asr.transcribe

    async def spy(audio, *, lang_hint, partial, turn):
        if not partial:
            hints.append((turn.speaker, lang_hint))
        return await transcribe(audio, lang_hint=lang_hint, partial=partial, turn=turn)

    h.engines.asr.transcribe = spy  # type: ignore[method-assign]
    u1 = await h.say(DOC.id)
    assert h.one("translation.final", u1)["target_lang"] == "ar"

    h.p.set_participants([DOC, PAT.model_copy(update={"lang": "tr"})])  # the patient taps Türkçe
    u2 = await h.say(DOC.id)
    assert h.one("translation.final", u2)["target_lang"] == "tr"  # target language
    u3 = await h.say(PAT.id)
    assert h.one("utterance.start", u3)["lang"] == "tr" and hints[-1] == (PAT.id, "tr")  # ASR hint
    assert h.one("translation.final", u3)["target_lang"] == "de"
    await h.p.drain()
