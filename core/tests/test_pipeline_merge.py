"""A sentence cut at a pause, merged again (raum.pipeline.merge). The GX10 run: "Sie dürfen das Metformin ab
morgen [800 ms] nicht mehr nehmen." ended at the pause and "You may take the Metformin?" was spoken. No LiveKit,
models or network."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import numpy as np
import pytest
from pipeline_helpers import DOC, Harness, not_implemented

from raum.events import CONTENT_EVENTS, ParticipantInfo
from raum.modes import get_mode
from raum.pipeline import room as room_mod
from raum.pipeline.base import AsrResult
from raum.summary import collect
from raum.verify import guard
from raum.verify.backtranslate import CheckResult

EN = ParticipantInfo(id="b-en01", role="b", lang="en")
SR = 16000
FIRST_S, REST_S = 1.0, 0.5
CUT = "Sie dürfen das Metformin ab morgen?"  # what the ASR made of the first half alone
REST = "Nicht mehr nehmen."
WHOLE = "Sie dürfen das Metformin ab morgen nicht mehr nehmen."
FRAME = np.full(320, 0.01, dtype=np.float32)  # 20 ms


@pytest.fixture(autouse=True)
def no_guard(monkeypatch):
    monkeypatch.setattr(guard, "check", not_implemented)
    monkeypatch.setattr(guard, "extract", not_implemented)


@pytest.fixture(autouse=True)
def slow_check(monkeypatch):
    """The second check takes a while and finds the cut half reversed (red): it must never reach the devices."""

    async def check(src, src_lang, tgt, tgt_lang, **_kw):
        await asyncio.sleep(0.15)
        return CheckResult(similarity=0.9 if "nicht" in src else 0.1, badge="green" if "nicht" in src else "red")

    monkeypatch.setattr(room_mod, "second_check", check)


class SentenceASR:
    """Finals by how much audio they heard: the first half, the rest alone, or both halves at once."""

    is_mock = True
    name = "sentence-asr"

    def __init__(self) -> None:
        self.finals: list[int] = []

    async def transcribe(self, audio, *, lang_hint, partial, turn):
        await asyncio.sleep(0.002)
        if partial:
            return AsrResult(text="", lang="de", conf=0.95)
        self.finals.append(len(audio))
        text = {int(FIRST_S * SR): CUT, int(REST_S * SR): REST, int((FIRST_S + REST_S) * SR): WHOLE}.get(len(audio), "")
        return AsrResult(text=text, lang=lang_hint or "de", conf=0.95, ms=2.0)


class Voice:
    """``clause_s`` of audio per clause in 0.1 s chunks, each chunk tagged with its clause number."""

    is_mock = False
    simulated = False
    sample_rate = SR

    def __init__(self, clause_s: float = 0.2) -> None:  # short: drain() waits for the echo pause to lift
        self.said: list[tuple[str, str]] = []
        self.chunks = max(1, round(clause_s / 0.1))
        self.size = min(1600, int(clause_s * SR))

    async def synthesize(self, text, lang):
        self.said.append((lang, text))
        tag = len(self.said)
        for _ in range(self.chunks):
            await asyncio.sleep(0.001)
            yield np.full(self.size, tag, dtype=np.int16)


class Sink:
    """The worker's sink. ``chunk_s``: like its AudioSource with a full queue, a chunk takes that long to go in."""

    def __init__(self, chunk_s: float = 0.0) -> None:
        self.chunk_s = chunk_s
        self.log: list[tuple[str, int, float]] = []  # (lang, clause tag, monotonic when handed in)

    async def __call__(self, lang, pcm, rate):
        self.log.append((lang, int(pcm[0]), time.monotonic()))
        await asyncio.sleep(self.chunk_s)


class Eot:
    """The end-of-turn model: ends the turn at every pause (the cut)."""

    unsure_s = 1.0
    fast = 0.1

    async def audio_p(self, audio):
        return 0.9

    async def text_p(self, text):
        return 0.0

    async def verdict(self, text, lang, audio=None):
        return SimpleNamespace(wait_s=0.0, timing=lambda: {"why": "test"})

    async def aclose(self):
        pass


def room(mode: str = "sprechzimmer", *, window_ms: float | None = None, voice: Voice | None = None,
         sink: Sink | None = None, eot: bool = False,
         patient: ParticipantInfo = EN) -> tuple[Harness, SentenceASR, Voice | None, Sink, list[str]]:
    h = Harness(mode=mode, room=f"{get_mode(mode).prefix}-1000", participants=[DOC, patient], merge=True)
    if window_ms is not None:
        h.p.mode = h.p.mode.model_copy(deep=True)
        h.p.mode.pipeline.merge_continuation_ms = window_ms
    asr = SentenceASR()
    h.p.engines.asr = asr
    h.p.engines.tts = voice  # type: ignore[assignment]
    sink = sink or Sink()
    h.p.tts_sink = sink
    stops: list[str] = []
    h.p.tts_stop = stops.append
    if eot:
        h.p.eot = Eot()
    return h, asr, voice, sink, stops


async def speak(h: Harness, speaker: str, seconds: float) -> str | None:
    utt = await h.p.speech_start(speaker)
    for _ in range(int(seconds * 50)):
        await h.p.speech_audio(speaker, FRAME)
    return utt


async def say(h: Harness, speaker: str, seconds: float) -> str | None:
    utt = await speak(h, speaker, seconds)
    await h.p.speech_end(speaker, None, 300.0)
    return utt


async def until(cond, timeout: float = 2.0) -> None:
    t = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < t, "timed out"
        await asyncio.sleep(0.003)


async def playing(sink: Sink, lang: str = "en") -> None:
    """Until the first sentence's voice is on the track."""
    await until(lambda: any(x == lang for x, _, _ in sink.log))


def after(h: Harness, env_type: str, utt: str) -> list:
    """Events for ``utt`` sent after the first ``env_type`` event."""
    i = next(i for i, e in enumerate(h.events) if e.type == env_type)
    return [e for e in h.events[i + 1:] if e.utt == utt]


def statements(h: Harness) -> list:
    """What the summary sees: the content events the session store keeps."""
    utts, _ = collect([e.model_dump(mode="json") for e in h.events if e.type in CONTENT_EVENTS])
    return utts


# ---------------------------------------------------------------- the merge


async def test_cut_sentence_is_merged_its_voice_stopped_and_spoken_whole():
    h, asr, voice, sink, stops = room(voice=Voice(0.5), sink=Sink(chunk_s=0.03), eot=True)  # mid-write at the merge
    await speak(h, DOC.id, FIRST_S)  # "Sie dürfen das Metformin ab morgen"
    await h.p.speech_pause(DOC.id, None, 300.0)  # the end-of-turn model ends the turn at the pause
    await playing(sink)  # "You may take the Metformin?" is playing
    assert h.one("transcript.final", "u-0001")["text"] == CUT
    said_before, t_merge = len(voice.said), time.monotonic()

    utt = await speak(h, DOC.id, REST_S)  # "... nicht mehr nehmen." 800 ms later
    assert utt == "u-0002"
    assert h.one("utterance.continued", "u-0001") == {"speaker": DOC.id, "into": "u-0002"}
    assert stops == ["en"]  # the worker drops what tts-en still holds
    await h.p.speech_pause(DOC.id, None, 300.0)
    await h.p.drain()

    # the whole sentence, transcribed once from both halves of audio
    assert asr.finals[-1] == int((FIRST_S + REST_S) * SR)
    assert h.one("transcript.final", "u-0002")["text"] == WHOLE
    assert "nicht mehr nehmen" in h.one("translation.final", "u-0002")["text"]
    # the cut half's voice stopped at once: none of its clauses went on after the merge
    late = [tag for lang, tag, t in sink.log if t >= t_merge and tag <= said_before]
    assert late == []
    spoken = [text for lang, text in voice.said[said_before:] if lang == "en"]
    assert spoken and "nicht mehr nehmen" in " ".join(spoken)
    # nothing more of the cut half reaches the devices: no late check, no red badge, no flag, no timing
    assert after(h, "utterance.continued", "u-0001") == []
    assert h.of("verify.result", "u-0001") == [] and h.of("flag", "u-0001") == []
    assert h.one("verify.result", "u-0002")["badge"] == "green"
    # one statement in the memory and in the summary
    assert [m["utt"] for m in h.p.memory()] == ["u-0002"]
    assert [(u.utt, u.text) for u in statements(h)] == [("u-0002", WHOLE)]
    assert not h.p._voice_lanes["en"].locked()


async def test_the_cut_turn_stops_but_speech_end_returns():
    h, _, _, _, _ = room(voice=None)
    await speak(h, DOC.id, FIRST_S)
    task = asyncio.create_task(h.p.speech_end(DOC.id, None, 300.0))  # the worker runs it as a task
    await asyncio.sleep(0)
    assert await speak(h, DOC.id, REST_S) == "u-0002"  # at once: the first turn has not even started
    assert h.of("utterance.continued", "u-0001")
    await task  # returns normally: the merge stops the turn, never the worker's task
    await h.p.speech_end(DOC.id, None, 300.0)
    await h.p.drain()
    assert h.of("transcript.final", "u-0001") == []
    assert h.one("transcript.final", "u-0002")["text"] == WHOLE


async def test_a_repeat_back_goes_to_the_merged_sentence():
    from raum.events import Control

    h, _, _, _, _ = room(voice=None)
    await say(h, DOC.id, FIRST_S)  # the instruction
    await h.p.handle_control(Control(action="repeat_back", by="a"))
    await say(h, EN.id, FIRST_S)  # the patient's restatement, cut at a pause ...
    await say(h, EN.id, REST_S)  # ... and went on
    await h.p.drain()
    assert h.one("utterance.continued", "u-0002")["into"] == "u-0003"
    assert h.one("repeat_back.result", "u-0003")["instruction_utt"] == "u-0001"
    assert [u.utt for u in statements(h)] == ["u-0001", "u-0003"]


async def test_simultaneous_segments_of_the_cut_turn_stop_too():
    """A headphone listener hears a long turn in segments while it runs (simul.py); its lanes outlive the turn
    and stop with it."""
    from test_pipeline_simul import DOC_DE, PAT_AR_HP, segs
    from test_pipeline_simul import room as simul_room
    from test_pipeline_simul import say as simul_say

    h = simul_room([DOC_DE, PAT_AR_HP])  # simultaneous from the first clause
    h.p.mode.pipeline.merge_continuation_ms = 1200
    voice, sink, stops = Voice(0.5), Sink(chunk_s=0.03), []
    h.p.engines.tts, h.p.tts_sink, h.p.tts_stop = voice, sink, stops.append  # type: ignore[assignment]
    await simul_say(h, DOC_DE.id, 1.5)
    assert segs(h, "translation.final")
    await playing(sink, "ar")
    lanes = list(h.p.merge._ended[DOC_DE.id].lanes.values())
    assert any(lane.voice is not None and not lane.voice.done() for lane in lanes)  # still speaking segments
    utt = await h.p.speech_start(DOC_DE.id)
    assert lanes and h.one("utterance.continued", "u-0001")["into"] == utt and stops == ["ar"]
    said_before, t_merge = len(voice.said), time.monotonic()
    await h.p.speech_end(DOC_DE.id, None, 300.0)
    await h.p.drain()
    assert all(lane.closed and all(t is None or t.done() for t in (lane.worker, lane.voice, lane.done))
               for lane in lanes)
    assert [tag for _, tag, t in sink.log if t >= t_merge and tag <= said_before] == []
    assert after(h, "utterance.continued", "u-0001") == []
    assert h.of("translation.final", utt)  # the merged sentence goes out instead


async def test_no_voice_at_all_still_merges():
    h, _, _, _, stops = room(voice=None)  # no voice engine: nothing to stop, the transcript still merges
    await say(h, DOC.id, FIRST_S)
    await say(h, DOC.id, REST_S)
    await h.p.drain()
    assert h.one("utterance.continued", "u-0001")["into"] == "u-0002" and stops == []
    assert h.one("transcript.final", "u-0002")["text"] == WHOLE
    assert [u.utt for u in statements(h)] == ["u-0002"]


# ---------------------------------------------------------------- no merge


def two_statements(h: Harness, asr: SentenceASR) -> None:
    assert h.of("utterance.continued") == []
    assert h.one("transcript.final", "u-0001")["text"] == CUT
    assert h.one("transcript.final", "u-0002")["text"] == REST
    assert asr.finals == [int(FIRST_S * SR), int(REST_S * SR)]
    assert [u.utt for u in statements(h)] == ["u-0001", "u-0002"]


@pytest.mark.parametrize("gap_s,merged", [(0.0, True), (0.16, False)])
async def test_no_merge_after_the_window(gap_s, merged):
    h, asr, _, sink, stops = room(window_ms=100, voice=Voice(0.3))  # the voice plays on past the window
    await say(h, DOC.id, FIRST_S)
    await playing(sink)
    await asyncio.sleep(gap_s)
    await say(h, DOC.id, REST_S)
    await h.p.drain()
    if merged:  # the control: the same voice, speech again at once
        assert h.one("transcript.final", "u-0002")["text"] == WHOLE and stops == ["en"]
        return
    two_statements(h, asr)
    assert stops == [] and h.of("verify.result", "u-0001")  # its check and voice ran to the end


@pytest.mark.parametrize("mode,merged", [("sprechzimmer", True), ("kanzlei", True), ("gespraech", False),
                                         ("verhandlungsraum", False)])
async def test_only_professional_rooms_merge(mode, merged):
    assert get_mode(mode).pipeline.merge_continuation_ms == (1200 if merged else 0)
    h, asr, _, sink, stops = room(mode, voice=Voice())
    await say(h, DOC.id, FIRST_S)
    await playing(sink)
    await say(h, DOC.id, REST_S)  # the voice of the first still plays
    await h.p.drain()
    if merged:
        assert h.one("transcript.final", "u-0002")["text"] == WHOLE and stops == ["en"]
        assert [u.utt for u in statements(h)] == ["u-0002"]
        return
    two_statements(h, asr)  # Gespraech, Verhandlungsraum: two short sentences stay two
    assert stops == []


async def test_no_merge_across_speakers():
    # on headphones, so the patient's mic stays open while the English voice plays (C-14)
    h, _, voice, sink, stops = room(voice=Voice(), patient=EN.model_copy(update={"audio_out": "headphones"}))
    await say(h, DOC.id, FIRST_S)
    await playing(sink)
    utt = await say(h, EN.id, REST_S)  # the patient answers at once
    await h.p.drain()
    assert utt == "u-0002" and h.of("utterance.continued") == [] and stops == []
    assert h.one("transcript.final", "u-0001")["text"] == CUT
    assert [u.utt for u in statements(h)] == ["u-0001", "u-0002"]
    assert any(lang == "en" for lang, _ in voice.said) and any(lang == "de" for lang, _ in voice.said)


async def test_no_merge_once_the_voice_has_played_out():
    h, asr, _, sink, stops = room(voice=Voice(clause_s=0.01))  # 10 ms per clause
    await say(h, DOC.id, FIRST_S)
    t_end = time.monotonic()
    await h.p.drain()
    await asyncio.sleep(0.05)  # everything of it has been heard
    assert sink.log and time.monotonic() - t_end < 1.0  # still within the 1.2 s window
    await say(h, DOC.id, REST_S)  # but the listener heard the first sentence already
    await h.p.drain()
    two_statements(h, asr)
    assert stops == []


async def test_burn_forgets_the_held_audio():
    h, asr, _, _, _ = room(voice=None)
    await say(h, DOC.id, FIRST_S)
    from raum.events import Control

    await h.p.handle_control(Control(action="burn"))
    await say(h, DOC.id, REST_S)
    await h.p.drain()
    assert h.of("utterance.continued") == [] and asr.finals[-1] == int(REST_S * SR)
