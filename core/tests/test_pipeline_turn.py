"""Semantic end of turn (raum.pipeline.turn + RoomPipeline.speech_pause). No LiveKit, models or network."""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest
from pipeline_helpers import DOC, Harness, not_implemented

from raum.pipeline.base import AsrResult
from raum.pipeline.turn import LK_METHOD, ST_METHOD, TurnDetector, Verdict
from raum.verify import guard


@pytest.fixture(autouse=True)
def no_guard(monkeypatch):
    monkeypatch.setattr(guard, "check", not_implemented)
    monkeypatch.setattr(guard, "extract", not_implemented)


class CountingASR:
    """Final transcripts say how much audio they heard; records every call."""

    is_mock = True
    name = "counting-asr"

    def __init__(self) -> None:
        self.finals: list[int] = []

    async def transcribe(self, audio, *, lang_hint, partial, turn):
        if not partial:
            self.finals.append(len(audio))
        await asyncio.sleep(0.005)
        return AsrResult(text=f"Satz mit {len(audio) // 1600} Zehnteln.", lang=lang_hint or "de", conf=0.95, ms=5.0)


class FakeEot:
    def __init__(self, waits: list[float]) -> None:
        self.waits = list(waits)
        self.texts: list[str] = []
        self.unsure_s = 1.0

    async def audio_p(self, audio):
        return 0.9

    async def verdict(self, text, lang, audio=None):
        self.texts.append(text)
        return Verdict(self.waits.pop(0), "test")

    async def aclose(self):
        pass


async def speak(h: Harness, speaker: str, seconds: float) -> None:
    await h.p.speech_start(speaker)
    for _ in range(int(seconds * 50)):
        await h.p.speech_audio(speaker, np.full(320, 0.01, dtype=np.float32))


def harness(waits: list[float]) -> tuple[Harness, CountingASR, FakeEot]:
    h = Harness()
    asr, eot = CountingASR(), FakeEot(waits)
    h.p.engines.asr = asr
    h.p.eot = eot
    return h, asr, eot


async def test_complete_pause_ends_turn_with_early_asr():
    h, asr, _ = harness([0.0])
    await speak(h, DOC.id, 1.0)
    await h.p.speech_pause(DOC.id, None, 250.0)
    await h.p.drain()
    assert asr.finals == [16000]  # one final ASR: the early one is reused
    final = h.one("transcript.final")
    assert final["text"] == "Satz mit 10 Zehnteln."
    timing = h.of("pipeline.timing")[0].data
    assert timing["asr_early"] is True and timing["eot"]["why"] == "test"
    assert timing["vad_ms"] >= 250.0


async def test_resume_within_wait_continues_the_same_utterance():
    h, asr, _ = harness([0.8, 0.0])
    await speak(h, DOC.id, 1.0)
    await h.p.speech_pause(DOC.id, None, 250.0)  # "Ich habe gestern ..."
    await asyncio.sleep(0.05)  # early ASR done, verdict "wait"
    assert h.of("utterance.end") == []
    utt = await h.p.speech_start(DOC.id)  # "... den Arzt angerufen."
    assert utt == "u-0001"
    for _ in range(25):
        await h.p.speech_audio(DOC.id, np.full(320, 0.01, dtype=np.float32))
    await h.p.speech_pause(DOC.id, None, 250.0)
    await h.p.drain()
    assert [e.utt for e in h.of("utterance.start")] == ["u-0001"]
    assert len(h.of("utterance.end")) == 1
    assert h.one("transcript.final")["text"] == "Satz mit 15 Zehnteln."  # all 1.5 s, one turn
    assert asr.finals == [16000, 24000]


async def test_unsure_wait_then_end_without_second_asr():
    h, asr, _ = harness([0.35])
    await speak(h, DOC.id, 0.5)
    t = asyncio.get_running_loop().time()
    await h.p.speech_pause(DOC.id, None, 250.0)
    await h.p.drain()
    assert asyncio.get_running_loop().time() - t >= 0.09  # 0.35 s of silence, 0.25 s already passed
    assert asr.finals == [8000]
    assert h.of("pipeline.timing")[0].data["vad_ms"] >= 340.0


async def test_off_record_during_wait_drops_the_utterance():
    from raum.events import SessionState

    h, _, _ = harness([1.0])
    await speak(h, DOC.id, 0.5)
    await h.p.speech_pause(DOC.id, None, 250.0)
    await asyncio.sleep(0.03)
    h.p.set_state(SessionState(off_record=True, sealed=False, participants=[], mode="sprechzimmer"))
    await h.p.drain()
    assert h.of("transcript.final") == []
    assert len(h.of("utterance.end")) == 1


async def test_without_detector_pause_is_speech_end():
    h = Harness()
    await speak(h, DOC.id, 0.5)
    await h.p.speech_pause(DOC.id, None, 250.0)
    await h.p.drain()
    assert len(h.of("transcript.final")) == 1


# ---------------------------------------------------------------- TurnDetector tiers


class FakeExecutor:
    def __init__(self, p_text: float | None, p_audio: float | None = 0.9) -> None:
        self.p_text, self.p_audio = p_text, p_audio

    async def do_inference(self, method: str, data: bytes) -> bytes | None:
        if method == LK_METHOD:
            if self.p_text is None:
                raise RuntimeError("inference process gone")
            return json.dumps({"eou_probability": self.p_text}).encode()
        assert method == ST_METHOD and len(data) % 2 == 0
        return None if self.p_audio is None else json.dumps({"p": self.p_audio}).encode()


OPTS = {"fast": 0.1, "unsure_s": 1.0, "max_s": 1.6, "audio_min": 0.5}


async def verdict(p_text, p_audio=0.9, lang="de", text="Ich habe gestern"):
    d = TurnDetector(FakeExecutor(p_text, p_audio), OPTS)
    audio = asyncio.ensure_future(d.audio_p(np.zeros(16000, dtype=np.float32)))
    return await d.verdict(text, lang, audio)


async def test_tiers():
    assert (await verdict(0.5)).wait_s == 0.0  # complete
    assert (await verdict(0.5, p_audio=0.2)).why == "audio-veto"
    assert (await verdict(0.5, p_audio=None)).wait_s == 0.0  # no audio model: text decides
    assert (await verdict(0.05)).wait_s == 1.0  # unsure
    assert (await verdict(0.001)).wait_s == 1.6  # below the German floor 0.0062: unfinished
    assert (await verdict(0.004, lang="ru")).wait_s == 1.0  # Russian floor is 0.0032
    assert (await verdict(None)).why == "text-failed"
    assert (await verdict(0.9, text="  ")).wait_s == 0.0


async def test_language_without_text_model():
    assert (await verdict(0.9, lang="ar")).wait_s == 1.0  # no LLM judge: unsure

    class Judge:
        def __init__(self, yes):
            self.yes = yes

        async def finished(self, text, lang):
            return self.yes

    for yes, wait in ((True, 0.0), (False, 1.6)):
        d = TurnDetector(FakeExecutor(0.9), OPTS, llm=Judge(yes))  # type: ignore[arg-type]
        assert (await d.verdict("اتصلت", "ar")).wait_s == wait
