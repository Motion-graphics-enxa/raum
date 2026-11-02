"""Push-to-talk: a release ends the turn at once (RoomPipeline.end_turn) and the worker's raum.control handling.
No LiveKit, models or network."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import numpy as np
import pytest
from pipeline_helpers import DOC, PAT, Harness, not_implemented
from test_pipeline_turn import CountingASR, FakeEot, speak

from raum.events import CONTROL_TOPIC
from raum.verify import guard


@pytest.fixture(autouse=True)
def no_guard(monkeypatch):
    monkeypatch.setattr(guard, "check", not_implemented)
    monkeypatch.setattr(guard, "extract", not_implemented)


def harness(waits: list[float] | None = None) -> tuple[Harness, CountingASR, FakeEot]:
    h = Harness()
    asr, eot = CountingASR(), FakeEot(list(waits or []))
    h.p.engines.asr = asr
    h.p.eot = eot
    return h, asr, eot


def frame(n: int = 320) -> np.ndarray:
    return np.full(n, 0.01, dtype=np.float32)


async def test_release_ends_the_turn_at_once_without_an_end_of_turn_wait():
    h, asr, eot = harness([1.6])  # the turn model would have waited 1.6 s
    await speak(h, DOC.id, 1.0)
    t = time.monotonic()
    assert await h.p.end_turn(DOC.id, "ptt", tail_s=0.05) == "u-0001"
    assert len(h.of("utterance.end")) == 1
    assert time.monotonic() - t < 0.5  # the tail and the turn's own work, no silence wait
    assert eot.texts == []  # no verdict asked
    assert asr.finals == [16000]  # one final pass on all of it
    await h.p.drain()
    timing = h.of("pipeline.timing")[0].data
    assert timing["eot"]["why"] == "ptt" and 45.0 <= timing["vad_ms"] < 300.0  # release -> end


async def test_release_during_an_end_of_turn_wait_cancels_it():
    h, _asr, _eot = harness([1.6])
    await speak(h, DOC.id, 1.0)
    await h.p.speech_pause(DOC.id, None, 250.0)  # a pause: verdict "wait 1.6 s"
    await asyncio.sleep(0.03)
    assert h.of("utterance.end") == []
    t = time.monotonic()
    await h.p.end_turn(DOC.id, "ptt")
    assert time.monotonic() - t < 0.5
    await h.p.drain()
    assert len(h.of("utterance.end")) == 1 and len(h.of("transcript.final")) == 1
    assert h.of("pipeline.timing")[0].data["eot"]["why"] == "ptt"


async def test_the_vads_own_end_after_the_release_is_ignored():
    h, asr, _ = harness([0.0])
    await speak(h, DOC.id, 1.0)
    await h.p.end_turn(DOC.id, "ptt")
    late = np.concatenate([frame() for _ in range(50)])
    await h.p.speech_pause(DOC.id, late, 250.0)  # the VAD's END_OF_SPEECH for the same speech (turn model on)
    await h.p.speech_end(DOC.id, late, 250.0)  # ... and without it
    await h.p.speech_end(DOC.id, None, 0.0)  # the track ending
    await h.p.drain()
    assert [e.utt for e in h.of("utterance.start")] == ["u-0001"]  # no new utterance from it
    assert len(h.of("utterance.end")) == 1 and len(h.of("transcript.final")) == 1
    assert asr.finals == [16000]

    # the next speech start lifts it: the VAD ends that turn as usual
    await speak(h, DOC.id, 0.5)
    await h.p.speech_pause(DOC.id, None, 250.0)
    await h.p.drain()
    assert [e.utt for e in h.of("utterance.start")] == ["u-0001", "u-0002"]
    assert len(h.of("transcript.final")) == 2


async def test_no_audio_is_taken_after_the_release_plus_the_tail():
    h, asr, _ = harness()
    await speak(h, DOC.id, 1.0)  # 16000 samples
    ending = asyncio.create_task(h.p.end_turn(DOC.id, "ptt", tail_s=0.1))
    await asyncio.sleep(0)
    for _ in range(25):  # the tail: the last syllable still arriving
        await h.p.speech_audio(DOC.id, frame())
    await ending
    for _ in range(50):  # after release + tail: the muted mic, or anything else
        await h.p.speech_audio(DOC.id, frame())
    await h.p.drain()
    assert asr.finals == [16000 + 25 * 320]
    assert len(h.of("utterance.end")) == 1


async def test_a_release_with_nothing_said_ends_nothing():
    h, asr, _ = harness()
    assert await h.p.end_turn(DOC.id, "ptt", tail_s=0.01) is None
    await h.p.drain()
    assert h.of("utterance.end") == [] and asr.finals == []


async def test_hands_free_speakers_are_unaffected():
    h, _asr, _ = harness([0.0, 0.0])
    await speak(h, DOC.id, 1.0)
    await h.p.end_turn(DOC.id, "ptt")  # the doctor uses push-to-talk
    await speak(h, PAT.id, 0.5)  # the patient is hands-free: the VAD and the turn model end their turn
    await h.p.speech_pause(PAT.id, None, 250.0)
    await h.p.drain()
    assert len(h.of("transcript.final")) == 2
    pat = next(e for e in h.of("pipeline.timing") if e.utt == "u-0002").data
    assert pat["eot"]["why"] == "test" and pat["asr_early"] is True


# ---------------------------------------------------------------- the worker: raum.control and the VAD events


class FakeVadStream:
    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()

    def push_frame(self, f) -> None:
        pass

    def end_input(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        ev = await self.q.get()
        if ev is None:
            raise StopAsyncIteration
        return ev


class FakeAudioStream:
    def __init__(self, *a, **kw) -> None:
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def aclose(self) -> None:
        pass


def _vad(kind: str, n: int = 0, speaking: bool = True):
    from livekit import rtc
    from livekit.agents.vad import VADEventType

    pcm = np.full(320, 300, dtype=np.int16)
    frames = [rtc.AudioFrame(data=pcm.tobytes(), sample_rate=16000, num_channels=1, samples_per_channel=320)
              for _ in range(n)]
    return SimpleNamespace(type=getattr(VADEventType, kind), frames=frames, speaking=speaking,
                           samples_index=0, silence_duration=0.25)


def _packet(identity: str, msg: dict, topic: str = CONTROL_TOPIC):
    from livekit import rtc

    p = SimpleNamespace(identity=identity, kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD)
    return SimpleNamespace(topic=topic, participant=p, data=json.dumps(msg).encode())


@pytest.fixture
def agent(monkeypatch):
    from livekit import rtc
    from test_pipeline_agent import _agent

    monkeypatch.setattr(rtc, "AudioStream", FakeAudioStream)
    a, events = _agent()
    a.pipeline._emit = a.emit  # the pipeline's events into the same list
    vad = FakeVadStream()
    a.vad = SimpleNamespace(stream=lambda: vad)
    asr = CountingASR()
    a.pipeline.engines.asr = asr
    a.pipeline.eot = FakeEot([1.6] * 8)  # hands-free would wait 1.6 s at every pause
    a.pipeline.set_participants([DOC, PAT])
    return SimpleNamespace(a=a, events=events, vad=vad, asr=asr)


async def _run(x, *evs, settle: float = 0.02) -> None:
    for ev in evs:
        x.vad.q.put_nowait(ev)
    await asyncio.sleep(settle)


async def test_held_pause_goes_on_and_the_release_ends_the_turn(agent):
    x = agent
    loop = asyncio.create_task(x.a._track_loop(DOC.id, object()))
    x.a._on_data(_packet(DOC.id, {"type": "ptt.press", "at": 1}))
    await _run(x, _vad("START_OF_SPEECH", 25), _vad("INFERENCE_DONE", 25))
    await _run(x, _vad("END_OF_SPEECH", 50), _vad("INFERENCE_DONE", 10, speaking=False))  # a pause, still held
    assert x.a.pipeline._active[DOC.id].hold is None  # no end-of-turn decision while held
    await _run(x, _vad("START_OF_SPEECH", 30), _vad("INFERENCE_DONE", 25))  # goes on: START frames not twice
    x.a._on_data(_packet(DOC.id, {"type": "ptt.release", "at": 2, "tail_ms": 40}))
    await asyncio.sleep(0.01)
    await _run(x, _vad("INFERENCE_DONE", 5))  # the tail
    await asyncio.sleep(0.08)
    starts = [e for e in x.events if e.type == "utterance.start"]
    ends = [e for e in x.events if e.type == "utterance.end"]
    assert len(starts) == 1 and len(ends) == 1
    assert x.asr.finals == [(25 + 25 + 10 + 25 + 5) * 320]
    await _run(x, _vad("END_OF_SPEECH", 60))  # the VAD's own end of the released speech: nothing
    await x.a.pipeline.drain()
    assert len([e for e in x.events if e.type == "utterance.start"]) == 1
    assert len([e for e in x.events if e.type == "transcript.final"]) == 1
    timing = next(e for e in x.events if e.type == "pipeline.timing").data
    assert timing["eot"]["why"] == "ptt"

    # pressed again: a new utterance from the VAD's start, as usual
    x.a._on_data(_packet(DOC.id, {"type": "ptt.press", "at": 3}))
    await _run(x, _vad("START_OF_SPEECH", 20))
    assert [e.utt for e in x.events if e.type == "utterance.start"] == ["u-0001", "u-0002"]
    x.vad.q.put_nowait(None)
    loop.cancel()


async def test_pressed_again_within_the_tail_the_turn_goes_on(agent):
    x = agent
    loop = asyncio.create_task(x.a._track_loop(DOC.id, object()))
    x.a._on_data(_packet(DOC.id, {"type": "ptt.press"}))
    await _run(x, _vad("START_OF_SPEECH", 25))
    x.a._on_data(_packet(DOC.id, {"type": "ptt.release", "tail_ms": 80}))
    await asyncio.sleep(0.02)
    x.a._on_data(_packet(DOC.id, {"type": "ptt.press"}))  # a slip of the finger
    await asyncio.sleep(0.12)
    assert [e for e in x.events if e.type == "utterance.end"] == []
    await _run(x, _vad("INFERENCE_DONE", 25))
    x.a._on_data(_packet(DOC.id, {"type": "ptt.release", "tail_ms": 10}))
    await asyncio.sleep(0.06)
    assert len([e for e in x.events if e.type == "utterance.end"]) == 1
    assert x.asr.finals == [50 * 320]
    x.vad.q.put_nowait(None)
    loop.cancel()


async def test_without_ptt_messages_the_vad_and_turn_model_decide(agent):
    x = agent
    x.a.pipeline.eot = FakeEot([0.0])
    loop = asyncio.create_task(x.a._track_loop(PAT.id, object()))
    await _run(x, _vad("START_OF_SPEECH", 25), _vad("INFERENCE_DONE", 25), _vad("END_OF_SPEECH", 50))
    await x.a.pipeline.drain()
    assert len([e for e in x.events if e.type == "transcript.final"]) == 1
    timing = next(e for e in x.events if e.type == "pipeline.timing").data
    assert timing["eot"]["why"] == "test"  # the turn model's verdict, as before
    x.vad.q.put_nowait(None)
    loop.cancel()


async def test_control_messages_from_a_speakerphone_or_another_topic_are_ignored(agent):
    from test_pipeline_agent import _p

    x = agent
    meta = {"role": "a", "lang": "de", "device": "speakerphone", "path": "direct", "audio_out": "room"}
    x.a._on_participant_connected(_p("a-room", json.dumps(meta)))
    x.a._on_data(_packet("a-room", {"type": "ptt.press"}))
    assert not x.a.ptt.get("a-room") or not x.a.ptt["a-room"].held
    x.a._on_data(_packet(DOC.id, {"type": "ptt.press"}, topic="raum.events"))
    x.a._on_data(SimpleNamespace(topic=CONTROL_TOPIC, participant=_packet(DOC.id, {}).participant, data=b"{nope"))
    assert not x.a.ptt.get(DOC.id) or not x.a.ptt[DOC.id].held
    x.a._on_data(_packet(DOC.id, {"type": "ptt.press"}))
    assert x.a.ptt[DOC.id].held


async def test_a_mic_muted_while_held_is_a_release(agent):
    x = agent
    loop = asyncio.create_task(x.a._track_loop(DOC.id, object()))
    x.a._on_data(_packet(DOC.id, {"type": "ptt.press"}))
    await _run(x, _vad("START_OF_SPEECH", 25))
    x.a._on_track_muted(_packet(DOC.id, {}).participant)  # the release itself never arrived
    await asyncio.sleep(0.05)
    assert not x.a.ptt[DOC.id].held
    assert len([e for e in x.events if e.type == "utterance.end"]) == 1
    x.a._on_track_muted(_packet(DOC.id, {}).participant)  # the normal mute after a release: nothing more
    await asyncio.sleep(0.02)
    assert len([e for e in x.events if e.type == "utterance.end"]) == 1
    x.vad.q.put_nowait(None)
    loop.cancel()
