"""Voice lock (F-10): enrollment, the check, short utterances, latency (the embedding runs beside the ASR), the
engines, the reset control, and the privacy rule: the voice embedding never leaves memory and is gone after a
burn, the session end and close."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time

import numpy as np
import pytest
from core_doubles import FakePublisher, install_security, make_config
from pipeline_helpers import DOC, PAT, Harness, fast_config, not_implemented

from raum.config import EngineConfig, load_config
from raum.events import Control, ParticipantInfo, SessionState
from raum.pipeline import voicelock as VL
from raum.pipeline.base import AsrResult
from raum.pipeline.engines import build_engines
from raum.pipeline.voicelock import MockSpeakerEncoder, VoiceLock, Voiceprint, make_speaker_encoder
from raum.verify import guard


@pytest.fixture(autouse=True)
def no_guard(monkeypatch):
    monkeypatch.setattr(guard, "check", not_implemented)
    monkeypatch.setattr(guard, "extract", not_implemented)


def tone(seconds: float, freq: float = 220.0, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * 16000)) / 16000.0
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def noise(seconds: float, amp: float = 0.1, seed: int = 1) -> np.ndarray:
    return (np.random.default_rng(seed).standard_normal(int(seconds * 16000)) * amp).astype(np.float32)


class ScriptedASR:
    """Final ASR with a fixed latency; texts in order (then the last one again); records what it returned."""

    name = "scripted"
    is_mock = True

    def __init__(self, texts: list[str] | None = None, latency_s: float = 0.0, conf: float = 0.93) -> None:
        self.texts = list(texts or ["Guten Tag, wie geht es Ihnen heute?"])
        self.latency_s, self.conf = latency_s, conf
        self.n = 0
        self.done_at: dict[str, float] = {}

    async def transcribe(self, audio, *, lang_hint, partial, turn):
        if partial:
            return AsrResult(text="", lang=lang_hint or "de", conf=self.conf)
        await asyncio.sleep(self.latency_s)
        text = self.texts[min(self.n, len(self.texts) - 1)]
        self.n += 1
        self.done_at[turn.utt] = time.monotonic()
        return AsrResult(text=text, lang=lang_hint or "de", conf=self.conf)


class SpyTTS:
    name, sample_rate = "spy", 16000

    def __init__(self) -> None:
        self.said: list[str] = []

    async def synthesize(self, text, lang, *, voice_ref=None, rate=None):
        self.said.append(text)
        yield np.zeros(160, dtype=np.int16)


def locked(*, threshold: float = 0.6, enroll_s: float = 3.0, min_utt_s: float = 1.0, asr=None, tts=None,
           participants: list[ParticipantInfo] | None = None, **options) -> Harness:
    cfg = fast_config()
    cfg.models.voicelock = EngineConfig(engine="mock", options={"threshold": threshold, "enroll_s": enroll_s,
                                                               "min_utt_s": min_utt_s, **options})
    h = Harness(cfg=cfg, participants=participants)
    if asr is not None:
        h.engines.asr = asr
    if tts is not None:
        h.engines.tts = tts
        h.p.echo.tail_s = 0.01

        async def sink(lang, pcm, rate):
            pass

        h.p.tts_sink = sink
    return h


async def speak(h: Harness, speaker: str, audio: np.ndarray, vad_ms: float = 300.0) -> str | None:
    utt = await h.p.speech_start(speaker)
    for i in range(0, len(audio), 320):
        await h.p.speech_audio(speaker, audio[i:i + 320])
    await h.p.speech_end(speaker, audio, vad_ms)
    await h.p.drain()
    return utt


DROPPED = {"transcript.final", "translation.delta", "translation.final", "verify.facts", "verify.result",
           "pipeline.timing", "flag"}


# ---------------------------------------------------------------- units


def test_mock_fingerprint_is_deterministic_and_tells_tone_from_noise():
    enc = MockSpeakerEncoder()
    a = enc.embed(tone(2.0))
    assert np.array_equal(a, enc.embed(tone(2.0))) and a.dtype == np.float32
    assert abs(float(np.linalg.norm(a)) - 1.0) < 1e-5
    assert float(a @ enc.embed(tone(1.5, amp=0.05))) > 0.99  # the same "voice", quieter and shorter
    assert float(a @ enc.embed(tone(2.0, freq=311))) < 0.4
    assert float(a @ enc.embed(noise(2.0))) < 0.0
    assert not np.any(enc.embed(np.zeros(16000, np.float32)))  # silence: no voice at all


def test_voiceprint_is_a_weighted_mean_that_hides_and_wipes_itself():
    e1 = np.array([1.0, 0.0, 0.0], np.float32)
    e2 = np.array([0.0, 1.0, 0.0], np.float32)
    vp = Voiceprint()
    assert vp.score(e1) is None
    vp.add(e1, 1.0)
    vp.add(e2, 3.0)
    assert vp.seconds == 4.0 and vp.utterances == 2
    assert vp.score(e2) == pytest.approx(3 / np.sqrt(10), abs=1e-6)
    assert repr(vp) == "<Voiceprint 4.0 s>"
    held = (vp._sum, vp._vec)
    vp.clear()
    assert vp.seconds == 0 and vp.score(e1) is None and all(not np.any(a) for a in held)


def test_engine_selection_and_defaults(monkeypatch):
    assert make_speaker_encoder("off") is None and make_speaker_encoder("none") is None
    with pytest.raises(ValueError):
        make_speaker_encoder("magic")
    assert make_speaker_encoder("mock") is make_speaker_encoder("mock")  # one per agent process

    # ecapa without the optional extra: one log line, the lock stays off
    monkeypatch.setattr(VL, "_encoders", {})

    class Missing:
        def __init__(self, *a, **k):
            raise ImportError("No module named 'speechbrain'")

    monkeypatch.setattr(VL, "EcapaSpeakerEncoder", Missing)
    assert make_speaker_encoder("ecapa", None, {}) is None

    for profile in ("mock", "spark"):
        monkeypatch.setenv("RAUM_PROFILE", profile)
        cfg = load_config()
        assert cfg.models.voicelock.engine == "off" and "voicelock" not in cfg.engine_names()
    monkeypatch.delenv("RAUM_PROFILE")
    assert build_engines(fast_config()).voicelock is None

    cfg = fast_config()
    cfg.models.voicelock = EngineConfig(engine="mock")
    eng = build_engines(cfg)
    assert isinstance(eng.voicelock, MockSpeakerEncoder) and eng.names["voicelock"] == "mock"
    assert cfg.engine_names()["voicelock"] == "mock"
    cfg.models.voicelock = EngineConfig(engine="ecapa", model="speechbrain/spkrec-ecapa-voxceleb")
    eng = build_engines(cfg)  # extra missing (stubbed above): built without it, not listed as running
    assert eng.voicelock is None and "voicelock" not in eng.names
    cfg.models.voicelock = EngineConfig(engine="bogus")
    with pytest.raises(ValueError):
        build_engines(cfg)


def test_ecapa_never_downloads_into_the_working_directory(monkeypatch, tmp_path):
    """SpeechBrain's default savedir is ./pretrained_models (the repo); the engine always passes one, and loads
    from it offline once the model is there."""
    calls = []

    class FakeClassifier:
        @classmethod
        def from_hparams(cls, **kw):
            calls.append(kw)
            return cls()

    import sys
    import types

    torch = types.ModuleType("torch")
    torch.set_num_threads = lambda n: calls.append({"threads": n})  # type: ignore[attr-defined]
    sb = types.ModuleType("speechbrain")
    inf = types.ModuleType("speechbrain.inference")
    spk = types.ModuleType("speechbrain.inference.speaker")
    spk.EncoderClassifier = FakeClassifier  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "speechbrain", sb)
    monkeypatch.setitem(sys.modules, "speechbrain.inference", inf)
    monkeypatch.setitem(sys.modules, "speechbrain.inference.speaker", spk)

    enc = VL.EcapaSpeakerEncoder(None, {"model_dir": str(tmp_path / "ecapa")})
    assert enc.name == "ecapa:spkrec-ecapa-voxceleb"
    assert calls[0] == {"threads": 1}
    assert calls[1]["source"] == "speechbrain/spkrec-ecapa-voxceleb" and calls[1]["savedir"] == str(tmp_path / "ecapa")
    assert calls[1]["run_opts"] == {"device": "cpu"}
    (tmp_path / "ecapa").mkdir()
    (tmp_path / "ecapa" / "hyperparams.yaml").write_text("x")
    VL.EcapaSpeakerEncoder(None, {"model_dir": str(tmp_path / "ecapa")})
    assert calls[-1]["source"] == str(tmp_path / "ecapa")  # downloaded before: no network
    VL.EcapaSpeakerEncoder(None, {})
    assert str(VL.default_model_dir()) in calls[-1]["savedir"] and ".cache" in calls[-1]["savedir"]


@pytest.mark.skipif(not os.environ.get("RAUM_TEST_ECAPA"),
                    reason="set RAUM_TEST_ECAPA=<model_dir> (installed with --extra voicelock; downloads ~85 MB once)")
async def test_real_ecapa_on_the_pipeline_path():
    """Opt-in smoke test for the box: the real model, 192-d unit vectors, fast enough to hide behind the ASR."""
    cfg = fast_config()
    cfg.models.voicelock = EngineConfig(engine="ecapa", options={"model_dir": os.environ["RAUM_TEST_ECAPA"],
                                                                "enroll_s": 2.0})
    h = locked(asr=ScriptedASR(latency_s=0.25))
    h.engines.voicelock = h.p.voicelock.encoder = build_engines(cfg).voicelock
    assert h.p.voicelock.encoder is not None and h.p.voicelock.encoder.name == "ecapa:spkrec-ecapa-voxceleb"
    v = h.p.voicelock.encoder.embed(noise(3.0))
    assert v.shape == (192,) and abs(float(np.linalg.norm(v)) - 1.0) < 1e-4
    for _ in range(2):
        await speak(h, DOC.id, noise(3.0))
    u = await speak(h, DOC.id, noise(3.0, seed=2))
    lk = h.one("voice.lock", u)
    assert lk["ms"] < 250 and lk["waited_ms"] < 15, lk


async def test_check_in_flight_across_a_burn_is_never_learned():
    vl = VoiceLock(MockSpeakerEncoder(), {"enroll_s": 1.0})
    chk = vl.start(DOC.id, "u-0001", tone(3.0), 300.0)
    vl.clear()  # burn while the embedding runs
    assert await vl.admit(chk, clean=True) and vl.enrolled_s(DOC.id) == 0.0
    assert "u-0001" in repr(chk) and "array" not in repr(chk) and repr(vl) == "<VoiceLock mock 0 voices>"


# ---------------------------------------------------------------- the pipeline


async def test_off_by_default_changes_nothing():
    h = Harness()
    assert h.p.voicelock.enabled is False
    utt = await speak(h, DOC.id, tone(3.0))
    assert h.of("voice.lock") == [] and h.one("transcript.final", utt)["text"]


async def test_enrolls_from_the_owner_then_drops_other_voices():
    spy = SpyTTS()
    h = locked(tts=spy)
    u1 = await speak(h, DOC.id, tone(2.5))  # 1.7 s of speech: learning the doctor's voice
    lk = h.one("voice.lock", u1)
    assert lk["accepted"] and lk["status"] == "enrolling" and lk["score"] is None and lk["enrolled_s"] == 1.7
    assert h.one("transcript.final", u1)["text"] and h.of("translation.final", u1)

    u2 = await speak(h, DOC.id, noise(2.5))  # not like what was learned: passes, but is not learned
    assert h.one("voice.lock", u2)["status"] == "enrolling" and h.of("translation.final", u2)
    assert h.p.voicelock.enrolled_s(DOC.id) == pytest.approx(1.7)

    u3 = await speak(h, DOC.id, tone(2.5, amp=0.1))  # 3.4 s: the lock closes
    assert h.one("voice.lock", u3)["score"] > 0.99 and h.p.voicelock.is_enrolled(DOC.id)

    said = len(spy.said)
    for audio in (tone(2.5, freq=311), noise(3.0, seed=7)):  # the patient near the doctor's phone; noise
        u = await speak(h, DOC.id, audio)
        lk = h.one("voice.lock", u)
        assert lk == {**lk, "accepted": False, "status": "mismatch", "threshold": 0.6, "enroll_s": 3.0}
        assert lk["score"] < 0.6 and lk["enrolled_s"] == 3.4
        assert not DROPPED & set(h.types(u)), h.types(u)  # not transcribed, translated or checked
        assert "utterance.end" in h.types(u)
    assert len(spy.said) == said  # nothing spoken

    u6 = await speak(h, DOC.id, tone(3.0, amp=0.2))
    lk = h.one("voice.lock", u6)
    assert lk["accepted"] and lk["status"] == "match" and lk["score"] > 0.99
    assert h.of("translation.final", u6) and len(spy.said) > said
    remembered = [m["utt"] for m in h.p.memory()]
    assert remembered == [u1, u2, u3, u6]  # the dropped ones never reach the conversation memory


async def test_each_device_has_its_own_owner():
    h = locked(asr=ScriptedASR())
    await speak(h, DOC.id, tone(4.0))  # 3.2 s: the doctor's phone knows the doctor
    await speak(h, PAT.id, tone(4.0, freq=311))  # the patient's phone knows the patient
    assert h.p.voicelock.is_enrolled(DOC.id) and h.p.voicelock.is_enrolled(PAT.id)
    on_doc = await speak(h, DOC.id, tone(2.5, freq=311))  # the patient, heard by the doctor's phone
    on_pat = await speak(h, PAT.id, tone(2.5, freq=311))  # the same words on the patient's own phone
    assert h.one("voice.lock", on_doc)["accepted"] is False and not h.of("transcript.final", on_doc)
    assert h.one("voice.lock", on_pat)["status"] == "match" and h.of("translation.final", on_pat)


async def test_short_utterances_pass_unchecked_and_teach_nothing():
    h = locked()
    for _ in range(2):
        await speak(h, DOC.id, tone(2.5))
    assert h.p.voicelock.is_enrolled(DOC.id)
    u = await speak(h, DOC.id, tone(1.2, freq=311))  # "ja" from someone else: 0.6 s of speech, too short
    lk = h.one("voice.lock", u)
    assert lk["accepted"] and lk["status"] == "short" and lk["score"] is None and lk["ms"] is None
    assert h.of("translation.final", u)

    h2 = locked(min_utt_s=0.5)  # configurable
    for _ in range(2):
        await speak(h2, DOC.id, tone(2.5))
    u = await speak(h2, DOC.id, tone(1.2, freq=311))
    assert h2.one("voice.lock", u)["status"] == "mismatch"
    h3 = locked()
    await speak(h3, DOC.id, tone(1.2))
    assert h3.p.voicelock.enrolled_s(DOC.id) == 0.0  # too short to learn from either


async def test_only_clean_speech_is_learned():
    h = locked(asr=ScriptedASR(conf=0.3))  # mumbled: "please say that again", not learned
    u = await speak(h, DOC.id, tone(3.0))
    assert h.one("voice.lock", u)["status"] == "enrolling" and h.of("flag", u)
    assert h.p.voicelock.enrolled_s(DOC.id) == 0.0

    h = locked(asr=ScriptedASR())
    utt = await h.p.speech_start(DOC.id)
    h.p.echo.play("de", 5.0, h.p.participants.values())  # the German voice plays on the doctor's phone meanwhile
    audio = tone(3.0)
    for i in range(0, len(audio), 320):
        await h.p.speech_audio(DOC.id, audio[i:i + 320])
    await h.p.speech_end(DOC.id, audio, 300)
    await h.p.drain()
    assert h.one("transcript.final", utt)["text"] and h.p.voicelock.enrolled_s(DOC.id) == 0.0
    h.p.echo.clear()


async def test_shared_speakerphone_is_not_locked():
    box = ParticipantInfo(id="a-spk1", role="a", lang="de", device="speakerphone", audio_out="room")
    h = locked(participants=[box, PAT])
    for _ in range(3):
        await speak(h, box.id, tone(2.5))
    u = await speak(h, box.id, noise(2.5))
    assert h.of("voice.lock") == [] and h.of("translation.final", u)


async def test_reset_learns_the_voice_again():
    h = locked(asr=ScriptedASR())
    for _ in range(2):
        await speak(h, DOC.id, tone(2.5))
        await speak(h, PAT.id, tone(2.5, freq=311))
    assert h.p.voicelock.is_enrolled(DOC.id) and h.p.voicelock.is_enrolled(PAT.id)
    await h.p.handle_control(Control(action="voice_lock_reset", by=DOC.id))
    assert h.p.voicelock.enrolled_s(DOC.id) == 0.0 and h.p.voicelock.is_enrolled(PAT.id)
    u = await speak(h, DOC.id, noise(2.5))
    assert h.one("voice.lock", u)["status"] == "enrolling" and h.of("translation.final", u)
    await h.p.handle_control(Control(action="voice_lock_reset", by="b"))  # by role
    assert h.p.voicelock.enrolled_s(PAT.id) == 0.0 and h.p.voicelock.enrolled_s(DOC.id) > 0
    await h.p.handle_control(Control(action="voice_lock_reset"))  # everyone
    assert h.p.voicelock._prints == {}


# ---------------------------------------------------------------- latency


class SlowEncoder(MockSpeakerEncoder):
    def __init__(self, delay_s: float = 0.0, fail: bool = False) -> None:
        super().__init__()
        self.delay_s, self.fail = delay_s, fail

    def embed(self, audio):
        time.sleep(self.delay_s)  # on the worker thread: the event loop keeps running
        if self.fail:
            raise RuntimeError("model crashed")
        return super().embed(audio)


async def test_embedding_runs_beside_the_asr_and_adds_no_wait():
    asr = ScriptedASR(latency_s=0.08)
    h = locked(asr=asr, enroll_s=1.0)
    h.engines.voicelock = h.p.voicelock.encoder = SlowEncoder(delay_s=0.05)  # 50 ms model vs 80 ms ASR
    await speak(h, DOC.id, tone(3.0))
    t = time.monotonic()
    u = await speak(h, DOC.id, tone(3.0))
    lk = h.one("voice.lock", u)
    assert lk["status"] == "match" and lk["ms"] >= 50
    assert lk["waited_ms"] < 15, lk  # the verdict was ready when the transcription was
    assert time.monotonic() - t < 0.08 + 0.05 + 0.1  # concurrent, not 80 + 50 ms back to back plus the turn


class PauseEot:
    """Semantic end of turn stand-in (raum.pipeline.turn): a fixed wait per pause."""

    def __init__(self, waits: list[float]) -> None:
        self.waits, self.unsure_s = list(waits), 1.0

    async def audio_p(self, audio):
        return 0.9

    async def verdict(self, text, lang, audio=None, **kw):
        from raum.pipeline.turn import Verdict

        return Verdict(self.waits.pop(0), "test")


class CountingEncoder(SlowEncoder):
    def __init__(self, delay_s: float = 0.0) -> None:
        super().__init__(delay_s=delay_s)
        self.handed: list[np.ndarray] = []

    def embed(self, audio):
        v = super().embed(audio)
        self.handed.append(v)
        return v


async def test_with_semantic_end_of_turn_the_check_runs_beside_the_early_asr():
    h = locked(asr=ScriptedASR(latency_s=0.08), enroll_s=1.0)
    enc = CountingEncoder(delay_s=0.05)
    h.engines.voicelock = h.p.voicelock.encoder = enc
    h.p.eot = PauseEot([0.0, 0.0, 0.9, 0.0])  # the wait counts from the stop, 300 ms before the pause call

    async def pause(audio: np.ndarray, resume: bool = False) -> str | None:
        utt = await h.p.speech_start(DOC.id)
        for i in range(0, len(audio), 320):
            await h.p.speech_audio(DOC.id, audio[i:i + 320])
        await h.p.speech_pause(DOC.id, audio, 300.0)
        if not resume:
            await h.p.drain()
        return utt

    await pause(tone(3.0))
    u = await pause(tone(3.0, amp=0.2))  # the transcript of the early ASR is used: so is its embedding
    lk = h.one("voice.lock", u)
    assert lk["status"] == "match" and lk["ms"] >= 50 and lk["waited_ms"] < 15, lk
    assert h.one("pipeline.timing", u)["asr_early"] is True and len(enc.handed) == 2  # one embedding per turn

    u = await pause(tone(2.0), resume=True)  # a pause, then the speaker goes on within the wait
    await asyncio.sleep(0.15)
    assert await h.p.speech_start(DOC.id) == u
    more = tone(2.0, freq=311)
    for i in range(0, len(more), 320):
        await h.p.speech_audio(DOC.id, more[i:i + 320])
    await h.p.speech_pause(DOC.id, more, 300.0)
    await h.p.drain()
    assert len(enc.handed) == 4 and not np.any(enc.handed[2])  # the check on the shorter audio: replaced, wiped
    assert len(h.of("voice.lock", u)) == 1 and h.p.voicelock._pending == {}


async def test_a_slow_verdict_holds_the_output_until_it_is_in():
    asr = ScriptedASR(latency_s=0.0)
    h = locked(asr=asr, enroll_s=1.0)
    h.engines.voicelock = h.p.voicelock.encoder = SlowEncoder(delay_s=0.0)
    await speak(h, DOC.id, tone(3.0))
    h.p.voicelock.encoder = SlowEncoder(delay_s=0.15)
    seen: dict[str, float] = {}
    emit = h.p._emit

    async def stamp(env):
        seen.setdefault(env.type, time.monotonic())
        await emit(env)

    h.p._emit = stamp
    u = await speak(h, PAT.id, tone(3.0, freq=311))  # first utterance on the patient's phone: enrolling
    assert h.one("voice.lock", u)["waited_ms"] >= 100
    assert seen["transcript.final"] - asr.done_at[u] >= 0.1  # nothing published before the verdict
    h.p.voicelock.encoder = SlowEncoder(delay_s=0.15)
    u = await speak(h, DOC.id, tone(3.0, freq=311))
    assert h.one("voice.lock", u)["status"] == "mismatch" and not h.of("transcript.final", u)


async def test_late_or_failed_checks_pass_unchecked(caplog):
    h = locked(asr=ScriptedASR(), enroll_s=1.0, max_wait_ms=60)
    await speak(h, DOC.id, tone(3.0))
    h.p.voicelock.encoder = SlowEncoder(delay_s=0.3)
    u = await speak(h, DOC.id, noise(3.0))  # would be a mismatch, but the verdict is late
    lk = h.one("voice.lock", u)
    assert lk["accepted"] and lk["status"] == "unchecked" and 50 <= lk["waited_ms"] < 250
    assert h.of("translation.final", u) and "passes unchecked" in caplog.text
    await asyncio.sleep(0.3)  # let the abandoned embedding finish on the worker thread

    h.p.voicelock.encoder = SlowEncoder(fail=True)
    u = await speak(h, DOC.id, noise(3.0))
    assert h.one("voice.lock", u)["status"] == "unchecked" and h.of("translation.final", u)
    assert "model crashed" not in caplog.text  # the class name only


async def test_asr_failure_gives_no_prompt_for_someone_elses_voice():
    class Broken(ScriptedASR):
        async def transcribe(self, audio, *, lang_hint, partial, turn):
            if not partial and self.n >= 2:
                raise RuntimeError("whisper down")
            return await super().transcribe(audio, lang_hint=lang_hint, partial=partial, turn=turn)

    h = locked(asr=Broken(), enroll_s=1.0)
    await speak(h, DOC.id, tone(3.0))
    await speak(h, DOC.id, tone(3.0))
    u = await speak(h, DOC.id, noise(3.0))
    assert h.one("voice.lock", u)["accepted"] is False and not h.of("flag", u)
    u = await speak(h, DOC.id, tone(3.0))
    assert h.one("voice.lock", u)["accepted"] and h.one("flag", u)["kind"] == "uncertain"


def test_warm_up_and_cli(capsys, caplog, monkeypatch):
    import raum.config
    from raum.cli import main

    monkeypatch.setattr(raum.config, "get_config", fast_config)  # lock off, whatever this box's raum.yaml says
    caplog.set_level(logging.INFO)
    cfg = fast_config()
    cfg.models.voicelock = EngineConfig(engine="mock")
    VL.warm_voicelock(cfg)
    assert "voice lock ready (mock" in caplog.text
    VL.warm_voicelock(fast_config())  # off: nothing to load
    assert main(["voicelock", "check", "--engine", "mock"]) == 0
    out = capsys.readouterr().out
    assert "loaded mock" in out and "8 s of audio" in out
    assert main(["voicelock", "check"]) == 1  # the config has it off


# ---------------------------------------------------------------- privacy


class SpyEncoder(MockSpeakerEncoder):
    """Keeps a copy of every vector it hands out, so the test can look for them everywhere."""

    def __init__(self) -> None:
        super().__init__()
        self.vectors: list[np.ndarray] = []
        self.handed: list[np.ndarray] = []

    def embed(self, audio):
        v = super().embed(audio)
        self.vectors.append(v.copy())
        self.handed.append(v)
        return v


def needles(v: np.ndarray) -> set[str]:
    """Ways a vector could show up in text: floats at full and printed precision, raw bytes, JSON, numpy."""
    out = {v.tobytes()[:24].hex(), base64.b64encode(v.tobytes()[:24]).decode(), json.dumps(v[:3].tolist())[1:-1],
           np.array2string(v, precision=4)[1:28]}
    for x in v[:6]:
        if abs(float(x)) > 1e-3:
            out |= {repr(float(x)), f"{float(x):.6f}", str(x)}
    return {n for n in out if len(n) >= 8}


async def test_voice_embedding_never_reaches_events_logs_store_or_audit(tmp_path, monkeypatch, caplog):
    from raum.sessions import SessionManager

    caplog.set_level(logging.DEBUG)
    install_security(monkeypatch, tmp_path)
    sm = SessionManager(make_config(tmp_path), publisher=FakePublisher())
    room = sm.room("sz-1000")
    sm.ensure_session(room)

    asr = ScriptedASR(texts=["Guten Tag.", "Wie geht es Ihnen?", "Nehmen Sie 500 mg.",
                             "GEHEIM fremde Stimme 4711", "Bis morgen."])
    h = locked(asr=asr)
    spy = SpyEncoder()
    h.engines.voicelock = h.p.voicelock.encoder = spy
    emit = h.p._emit

    async def emit_and_ingest(env):
        await emit(env)
        await sm.ingest("sz-1000", env)

    h.p._emit = emit_and_ingest
    await speak(h, DOC.id, tone(2.5))
    await speak(h, DOC.id, tone(2.5, amp=0.2))
    await speak(h, DOC.id, tone(3.0))
    vp = h.p.voicelock._prints[DOC.id]
    owner = vp._vec.copy()
    dropped = await speak(h, DOC.id, tone(3.0, freq=311))  # someone else on the doctor's phone
    await speak(h, DOC.id, tone(3.0, amp=0.1))
    assert h.one("voice.lock", dropped)["accepted"] is False and len(spy.vectors) == 5

    found = set().union(*(needles(v) for v in [owner, *spy.vectors]))
    events = json.dumps([e.model_dump(mode="json") for e in h.events])
    assert '"voice.lock"' in events and '"score"' in events  # the glass box gets the score ...
    store = json.dumps(room.session.store.read_all())
    assert "transcript.final" in store and "voice.lock" not in store
    assert "GEHEIM" not in events + store and dropped not in store  # ... the dropped words go nowhere
    summary = await sm.end("sz-1000")
    assert "GEHEIM" not in summary.model_dump_json()  # not summarized
    audit = sm.cfg.audit_path().read_text(encoding="utf-8") if sm.cfg.audit_path().exists() else ""
    logs = caplog.text + "\n".join(str(r.args) for r in caplog.records)
    assert "not the owner's voice" in logs  # the drop was logged ...
    for hay, where in ((events, "events"), (store, "store"), (audit, "audit"), (logs, "logs")):
        for n in found:
            assert n not in hay, f"voice embedding leaked into the {where}: {n}"
    # ... and every vector handed out was wiped after its verdict; the reprs show seconds only
    assert all(not np.any(v) for v in spy.handed)
    assert repr(vp) == "<Voiceprint 3.4 s>" and repr(h.p.voicelock) == "<VoiceLock mock 1 voices>"

    # dropped on burn, session end, a new session and close; the held arrays are zeroed
    held = (vp._sum, vp._vec)
    await h.p.handle_control(Control(action="burn"))
    assert h.p.voicelock._prints == {} and all(not np.any(a) for a in held) and vp.seconds == 0

    def state(status: str, sid: str) -> SessionState:
        return SessionState(off_record=False, sealed=False, participants=[], mode="sprechzimmer", status=status,
                            session_id=sid)

    h.p.set_state(state("live", "s-1"))
    await speak(h, DOC.id, tone(2.5))
    assert h.p.voicelock.enrolled_s(DOC.id) > 0
    h.p.set_state(state("live", "s-2"))  # a new session in the same room: new people, no old voices
    assert h.p.voicelock._prints == {}
    await speak(h, DOC.id, tone(2.5))
    h.p.set_state(state("ended", "s-2"))  # session end
    assert h.p.voicelock._prints == {}
    await speak(h, DOC.id, tone(2.5))
    assert h.p.voicelock.enrolled_s(DOC.id) > 0
    await h.p.aclose()
    assert h.p.voicelock._prints == {}


async def test_core_forwards_the_reset_control(tmp_path, monkeypatch):
    from raum.sessions import SessionManager

    install_security(monkeypatch, tmp_path)
    sm = SessionManager(make_config(tmp_path), publisher=FakePublisher())
    sm.ensure_session(sm.room("sz-1000"))
    r = await sm.control("sz-1000", Control(action="voice_lock_reset", by=DOC.id))
    assert r["ok"] and r["forwarded"] is False  # no agent connected in this test: nothing to forward to
