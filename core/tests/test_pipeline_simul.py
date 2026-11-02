"""Simultaneous mode (simul.py) on top of the semantic end of turn (turn.py), speculative translation
(speculate.py) and the stable-partial-as-final shortcut. Mock engines, no LiveKit, GPU or network.

Speech is fed in 20 ms frames and every partial ASR is awaited as soon as it is spawned, so partials cover
exactly 0.5 s, 1.0 s, ... of audio: the commit points are deterministic.
"""

from __future__ import annotations

import asyncio
import json
import time

import numpy as np
import pytest
from pipeline_helpers import Harness, fake_check, fake_extract, fast_config, not_implemented
from test_voice_lanes import agent  # noqa: F401  (fixture: the worker with a fake AudioSource)

from raum.config import PipelineConfig, RaumConfig, load_config, repo_root
from raum.events import Control, ParticipantInfo
from raum.pipeline import mockdata, simul
from raum.pipeline import texts as T
from raum.pipeline.base import TranslateRequest
from raum.pipeline.simul import align, boundary, commit_len, commit_points
from raum.pipeline.translate import MockTranslator, translate_messages
from raum.pipeline.turn import Verdict
from raum.verify import guard

DOC_DE = ParticipantInfo(id="a-doc1", role="a", lang="de")
DOC_EN = ParticipantInfo(id="a-doc1", role="a", lang="en")
PAT_AR_HP = ParticipantInfo(id="b-k3x9", role="b", lang="ar", audio_out="headphones")
PAT_AR_SPK = ParticipantInfo(id="b-k3x9", role="b", lang="ar", audio_out="speaker")


@pytest.fixture
def no_guard(monkeypatch):
    monkeypatch.setattr(guard, "check", not_implemented)
    monkeypatch.setattr(guard, "extract", not_implemented)


@pytest.fixture
def fake_guard(monkeypatch):
    monkeypatch.setattr(guard, "check", fake_check)
    monkeypatch.setattr(guard, "extract", fake_extract)


def line(line_id: str) -> mockdata.MockLine:
    return next(ln for ln in mockdata.load_script("sprechzimmer").lines if ln.id == line_id)


def config(**asr) -> RaumConfig:
    cfg = fast_config()
    cfg.models.asr.options = {"latency_ms": 2, "partial_latency_ms": 1, "partial_every_ms": 500, "word_ms": 100,
                              **asr}
    return cfg


class Eot:
    """Stand-in for turn.py's TurnDetector (``RoomPipeline.eot``): ``text_p`` rates a text as a finished
    utterance (``p_text`` vs ``fast``); ``verdict`` answers a pause with the next of ``waits`` (seconds)."""

    fast = 0.1
    unsure_s = 1.0

    def __init__(self, p_text=lambda text: 1.0, waits: list[float] | None = None) -> None:
        self.p_text = p_text
        self.waits = list(waits or [])

    async def text_p(self, text):
        return self.p_text(text)

    async def audio_p(self, audio):
        return 0.9

    async def verdict(self, text, lang, audio=None, **kw):
        return Verdict(self.waits.pop(0) if self.waits else 0.0, "test")

    async def aclose(self):
        pass


def room(participants, *, after_ms: float = 0.0, complete_clauses: bool = True, cfg: RaumConfig | None = None,
         **asr) -> Harness:
    """A Sprechzimmer harness. ``after_ms=0``: simultaneous from the first clause (the room default is 4 s);
    ``complete_clauses``: a turn model that rates every clause a finished utterance, so the punctuation rules
    alone decide (without one, only sentence ends commit)."""
    h = Harness(participants=participants, cfg=cfg or config(**asr))
    h.p.mode = h.p.mode.model_copy(deep=True)
    h.p.mode.pipeline.simultaneous_after_ms = after_ms
    if complete_clauses:
        h.p.eot = Eot()
    return h


FRAME_S = 0.003  # wall time per 20 ms frame: speech runs ~7x real time, the mock engines take 1-5 ms


async def speak(h: Harness, speaker: str, seconds: float, *, start: bool = True) -> str | None:
    utt = await h.p.speech_start(speaker) if start else None
    for _ in range(round(seconds * 50)):
        await h.p.speech_audio(speaker, np.zeros(320, dtype=np.float32))
        u = h.p._active.get(speaker)
        if u is not None and u.partial_task is not None and not u.partial_task.done():
            await u.partial_task
        await asyncio.sleep(FRAME_S)
    return utt


async def say(h: Harness, speaker: str, seconds: float, vad_ms: float = 300.0) -> str | None:
    utt = await speak(h, speaker, seconds)
    await h.p.speech_end(speaker, None, vad_ms)
    return utt


async def until(cond, timeout: float = 2.0) -> None:
    t = time.monotonic()
    while not cond():
        assert time.monotonic() - t < timeout, "timed out"
        await asyncio.sleep(0.002)


class Voice:
    """TTS double installed with a sink: ``spoken`` is what reached the listener (the sink), ``made`` what
    was synthesised (pre-synthesis included); ``*_at`` = how many events were out at that moment."""

    name = "voice"
    sample_rate = 16000
    simulated = False

    def __init__(self, h: Harness) -> None:
        self.h = h
        self.made: list[str] = []
        self.made_at: list[int] = []
        self.spoken: list[str] = []
        self.at: list[int] = []
        h.engines.tts = self  # type: ignore[assignment]
        h.p.tts_sink = self.sink

    async def synthesize(self, text, lang):
        self.made.append(text)
        self.made_at.append(len(self.h.events))
        yield np.full(160, len(self.made) - 1, dtype=np.int16)

    async def sink(self, lang, pcm, rate):
        self.spoken.append(self.made[int(pcm[0])])
        self.at.append(len(self.h.events))


def segs(h: Harness, type: str, utt: str = "u-0001") -> list[dict]:
    return [e.data for e in h.of(type, utt) if e.data.get("seg") is not None]


def idx(h: Harness, type: str, utt: str = "u-0001", **match) -> int:
    for i, e in enumerate(h.events):
        if e.type == type and e.utt == utt and all(e.data.get(k) == v for k, v in match.items()):
            return i
    raise AssertionError(f"no {type} {match}")


# ---------------------------------------------------------------- commit rules


def test_boundaries_skip_abbreviations_ordinals_and_ellipsis():
    assert boundary("Demir.") == "sentence" and boundary("mir?") == "sentence" and boundary("اليوم؟") == "sentence"
    assert boundary("Dr.") is None and boundary("12.") is None and boundary("z.B.") is None
    assert boundary(["das", "..."][-1]) is None and boundary("so…") is None
    assert boundary("day,") == "comma" and boundary("يوميًا،") == "comma" and boundary("gut;") == "clause"
    assert boundary('"Ja."') == "sentence" and boundary("1,2") is None and boundary("Weber") is None


def test_commit_needs_two_identical_partials_and_a_clause_boundary():
    a = ["Hello,", "Mrs", "Demir.", "I", "am", "Dr.", "Weber.", "What"]
    b = ["Hello,", "Mrs", "Demir.", "I", "am", "Dr.", "Weber.", "What", "brings", "you"]
    assert commit_points(a, b, "en") == [3, 7]  # "Hello," has 1 word; "Dr." is no sentence end
    assert commit_len(a, b, "en") == 7
    assert commit_len(a[:2], b, "en") == 0  # "Mrs" not seen twice yet: nothing stable past the comma
    assert commit_len(["Hello,", "Mrs", "Demir"], ["Hello,", "Mrs", "Demir.", "I"], "en") == 0  # "." new


def test_comma_needs_six_words_and_never_cuts_german():
    en = ["Keep", "taking", "500", "mg", "of", "Metformin", "twice", "a", "day,", "in", "the"]
    assert commit_len(en, en, "en") == 9
    short = ["Yes,", "well,", "I", "think", "so,", "and"]
    assert commit_len(short, short, "en") == 0  # every clause shorter than 6 words
    de = ["Nehmen", "Sie", "zweimal", "täglich", "500", "mg", "Metformin", "weiter,", "morgens", "und"]
    assert commit_len(de, de, "de") == 0  # verb-final subordinate clauses: German cuts at sentence ends only
    de2 = ["Guten", "Tag,", "Frau", "Demir.", "Ich", "bin", "Dr.", "Weber.", "Was"]
    assert commit_points(de2, de2, "de") == [4, 8]
    assert commit_len(["A", "b", "c", "d", "e", "f;", "g"], ["A", "b", "c", "d", "e", "f;", "g"], "en") == 6
    assert commit_len(["A", "b", "c", "d", "e", "f;", "g"], ["A", "b", "c", "d", "e", "f;", "g"], "de") == 0


def test_align_finds_the_uncommitted_rest_after_an_asr_revision():
    done = ["Guten", "Tag,", "Frau", "Demir."]
    assert align(done, ["Guten", "Tag,", "Frau", "Demir.", "Ich", "bin"]) == 4
    assert align(done, ["guten", "tag", "frau", "demir", "ich", "bin"]) == 4  # case and punctuation revised
    assert align(done, ["Guten", "Tag,", "Frau", "Damir.", "Ich", "bin"]) == 4  # one word revised
    assert align([], ["a", "b"]) == 0


async def test_clause_completeness_uses_the_turn_model_else_sentence_ends():
    h = room([DOC_DE, PAT_AR_HP], complete_clauses=False)
    c = h.p.simul.complete
    assert await c("Ich bin Dr. Weber.", "de") and await c("Was führt Sie?", "de")
    assert not await c("Ich bin Dr.", "de") and not await c("Keep taking 500 mg,", "en")
    assert not await c("Ich weiß nicht ...", "de") and not await c("", "de")
    h.p.eot = Eot(p_text=lambda t: 0.4 if t.endswith(",") else 0.01)
    assert await c("Keep taking 500 mg of Metformin twice a day,", "en")  # the model, not punctuation
    assert not await c("Ich bin Dr. Weber.", "de")  # the model says it goes on
    assert await c("مرحبًا سيدة دمير.", "ar")  # a language the text model lacks: sentence ends


async def test_svo_clauses_commit_at_the_lower_floor_once_the_rules_pass():
    from raum.pipeline.turn import TurnDetector

    h = room([DOC_DE, PAT_AR_HP], complete_clauses=False)
    c = h.p.simul.complete

    class Ex:
        async def do_inference(self, method, data):
            return json.dumps({"eou_probability": 0.05}).encode()  # between fast_floor 0.02 and fast 0.1

    h.p.eot = TurnDetector(Ex(), {"fast": 0.1})
    assert await c("I take metformin twice a day,", "en") and await c("Я принимаю метформин два раза в день,", "ru")
    assert not await c("Ich nehme Metformin zweimal täglich,", "de")  # verb-final German: the old floor
    assert not await c("I take 500 of the", "en") and not await c("Принимайте по 500", "ru")  # rules first


# ---------------------------------------------------------------- config


def test_pipeline_config_default_auto_and_yaml_off(tmp_path, monkeypatch):
    monkeypatch.delenv("RAUM_PROFILE", raising=False)
    assert load_config().pipeline.simultaneous == "auto"
    assert PipelineConfig().simultaneous == "auto" and PipelineConfig().speculate is True
    assert PipelineConfig.model_validate({"simultaneous": False}).simultaneous == "off"  # YAML `off`
    p = tmp_path / "raum.yaml"
    p.write_text("profile: mock\npipeline: { simultaneous: off }\n")
    cfg = load_config(p)
    assert cfg.pipeline.simultaneous == "off"
    for prof in sorted((repo_root() / "profiles").glob("*.yaml")):
        assert "simultaneous: auto" in prof.read_text(), prof.name
    from raum.modes import get_mode

    assert get_mode("sprechzimmer").pipeline.simultaneous_after_ms == 4000  # long turns only
    assert get_mode("kanzlei").pipeline.simultaneous_after_ms == 0


# ---------------------------------------------------------------- simultaneous mode


async def test_german_source_commits_sentences_while_speaking_and_the_tail_at_the_end(no_guard):
    h = room([DOC_DE, PAT_AR_HP])
    utt = await say(h, DOC_DE.id, 1.5)
    await h.p.drain()
    finals = segs(h, "translation.final", utt)
    sz01 = line("sz-01")
    # never at the comma after "Guten Tag", never at "Dr."
    assert [f["seg"] for f in finals] == [0, 1, 2]
    assert finals[0]["text"] == "مرحبًا سيدة دمير." and finals[1]["text"] == "أنا الدكتور فيبر."
    assert finals[2]["text"] == "ما الذي أتى بكِ إليّ اليوم؟" and finals[2]["segs"] == 3
    assert "segs" not in finals[0] or finals[0]["segs"] is None
    assert " ".join(f["text"] for f in finals) == sz01.text["ar"]
    # seg 0 was done while the doctor was still speaking (seg 1 was cut on the last partial); the tail after
    end = idx(h, "utterance.end", utt)
    assert idx(h, "translation.final", utt, seg=0) < end < idx(h, "translation.delta", utt, seg=2)
    assert h.one("transcript.final", utt)["text"] == sz01.text["de"]


async def test_event_order_with_seg_and_one_check_per_utterance(no_guard):
    h = room([DOC_DE, PAT_AR_HP])
    utt = await say(h, DOC_DE.id, 1.5)
    await h.p.drain()
    for s in range(3):
        deltas = [i for i, e in enumerate(h.events) if e.type == "translation.delta" and e.data["seg"] == s]
        assert deltas and [h.events[i].data["seq"] for i in deltas] == list(range(1, len(deltas) + 1))
        fin = idx(h, "translation.final", utt, seg=s)
        assert max(deltas) < fin < idx(h, "verify.facts", utt, seg=s)
        if s:
            assert idx(h, "verify.facts", utt, seg=s - 1) < min(deltas)  # segments strictly in order
    assert [d["seg"] for d in segs(h, "verify.facts", utt)] == [0, 1, 2]
    res = h.one("verify.result", utt)  # ONE second-model check, of the whole utterance: no seg
    assert res.get("seg") is None and res["badge"] == "green"
    assert idx(h, "verify.result", utt) > idx(h, "translation.final", utt, seg=2)
    t = h.one("pipeline.timing", utt)
    assert t["simultaneous"] is True and t["segs"] == 3 and t["target_lang"] == "ar"
    assert t["mt_first_ms"] < t["vad_ms"]  # first words before the endpoint (in real time: before the stop)
    assert h.types(utt)[-1] == "pipeline.timing"


async def test_voice_only_for_committed_segments_in_order(no_guard):
    h = room([DOC_EN, PAT_AR_HP])
    voice = Voice(h)
    h.set_next_line("a", 2)  # sz-05: "Keep taking 500 mg of Metformin twice a day, in the morning ..."
    utt = await say(h, DOC_EN.id, 2.0)
    await h.p.drain()
    texts = [f["text"] for f in segs(h, "translation.final", utt)]
    assert texts == ["استمري في تناول 500 ملغ من ميتفورمين مرتين يوميًا،", "صباحًا ومساءً مع الطعام."]
    assert voice.spoken == texts  # segments only, each once, in order: never a draft or a partial
    assert voice.at[0] < idx(h, "utterance.end", utt)  # the listener hears it while the doctor speaks
    assert h.one("pipeline.timing", utt)["tts_first_ms"] < h.one("pipeline.timing", utt)["vad_ms"]


async def test_stub_model_never_commits_an_incomplete_clause(no_guard):
    h = room([DOC_EN, PAT_AR_HP], complete_clauses=False)  # no turn model: only sentence ends commit
    h.set_next_line("a", 2)
    utt = await say(h, DOC_EN.id, 1.5)  # ends right after the last word: nothing stable at a sentence end
    await h.p.drain()
    assert not segs(h, "translation.final", utt)
    assert h.one("translation.final", utt)["text"] == line("sz-05").text["ar"]  # consecutive, as before


async def test_a_correct_dose_never_holds_the_voice_and_is_noted(fake_guard):
    h = room([DOC_EN, PAT_AR_HP])
    voice = Voice(h)
    h.set_next_line("a", 2)  # sz-05: 500 mg in the first segment (confirm_on: dose)
    utt = await say(h, DOC_EN.id, 2.0)
    await until(lambda: len(voice.spoken) >= 1)  # spoken at once: nothing waits for a tap
    assert not h.of("flag", utt)  # no prompt at segment time
    await h.p.drain()
    assert voice.spoken == [f["text"] for f in segs(h, "translation.final", utt)]
    flag = h.one("flag", utt)  # after the one utterance check (green): a quiet note for the doctor
    assert flag["id"] == f"{utt}-ar-noted" and flag["kind"] == "noted" and flag["needs_confirm"] is False
    assert flag["show_to"] == "a" and flag["texts"]["en"] == "500 mg"


async def test_red_mismatch_segment_is_never_spoken_and_read_back_after(fake_guard):
    from raum.pipeline import readback

    h = room([DOC_EN, PAT_AR_HP])
    voice = Voice(h)
    h.set_next_line("a", 3)  # sz-07: 400 mg comes out as 40 mg in the first segment
    utt = await say(h, DOC_EN.id, 2.0)
    await h.p.drain()  # nothing waits for a tap
    facts = segs(h, "verify.facts", utt)
    assert facts[0]["red"] is True and {"kind": "dose", "src": "400 mg", "tgt": "40 mg", "match": False} in facts[0]["facts"]
    flag = h.one("flag", utt)  # one read-back per utterance, no correction flag on top
    assert flag["id"] == f"{utt}-ar-mismatch" and flag["kind"] == "mismatch" and flag["needs_confirm"] is True
    assert flag["text"] == "للتأكد فقط: 400 mg؟" and flag["spoken"] is True and flag["confirm_by"] == "b"
    tail = segs(h, "translation.final", utt)[-1]["text"]
    # the wrong dose never reaches the listener's ear; the read-back of what was said follows the utterance
    assert voice.spoken == [tail, readback.spoken(flag["text"], "ar")] and "400 ملليغرام" in voice.spoken[-1]
    assert h.one("verify.result", utt)["badge"] == "red"  # guard red wins in the utterance check


class _FarEmbedder:
    name = "far"

    async def embed(self, texts):
        return [[1.0, 0.0], [0.0, 1.0]][: len(texts)]


async def test_red_utterance_check_sends_a_correction_flag(no_guard):
    h = room([DOC_DE, PAT_AR_HP])
    h.engines.embed = _FarEmbedder()  # type: ignore[assignment]
    utt = await say(h, DOC_DE.id, 1.5)
    await h.p.drain()
    res = h.one("verify.result", utt)
    assert res["badge"] == "red" and res.get("seg") is None
    flag = h.one("flag", utt)
    assert flag["id"] == f"{utt}-ar-correction" and flag["needs_confirm"] is True
    assert flag["text"] == T.CORRECTION["ar"] and set(flag["texts"]) >= set(T.LANGS)


async def test_nothing_left_at_the_end_resends_the_last_final_with_segs(no_guard):
    h = room([DOC_DE, PAT_AR_HP])
    utt = await say(h, DOC_DE.id, 2.5)  # still "speaking" after the last word: everything committed early
    await h.p.drain()
    finals = segs(h, "translation.final", utt)
    assert [f["seg"] for f in finals] == [0, 1, 2, 2]
    assert finals[-1]["segs"] == 3 and finals[-1]["text"] == finals[-2]["text"]


async def test_consecutive_when_a_listener_uses_a_speaker(no_guard):
    tr_hp = ParticipantInfo(id="b-tr01", role="b", lang="tr", audio_out="headphones")
    ar_room = ParticipantInfo(id="b-ar02", role="b", lang="ar", audio_out="room")
    h = room([DOC_DE, PAT_AR_HP, ar_room, tr_hp])  # one Arabic listener plays it aloud
    utt = await say(h, DOC_DE.id, 1.5)
    await h.p.drain()
    ar = [e.data for e in h.of("translation.final", utt) if e.data["target_lang"] == "ar"]
    tr = [e.data for e in h.of("translation.final", utt) if e.data["target_lang"] == "tr"]
    assert len(ar) == 1 and ar[0].get("seg") is None and ar[0]["text"] == line("sz-01").text["ar"]
    assert [f["seg"] for f in tr] == [0, 1, 2]  # the headphones language still goes simultaneous
    assert not [e for e in h.of("translation.delta", utt) if e.data["target_lang"] == "ar" and e.data.get("seg")
                is not None]

    h2 = room([DOC_DE, PAT_AR_SPK])
    utt = await say(h2, DOC_DE.id, 1.5)
    await h2.p.drain()
    assert not segs(h2, "translation.final", utt) and not segs(h2, "verify.facts", utt)
    assert "simultaneous" not in h2.one("pipeline.timing", utt)


async def test_off_in_config_or_mode_and_long_turns_only_in_the_sprechzimmer(no_guard):
    cfg = config()
    cfg.pipeline.simultaneous = "off"
    h = room([DOC_DE, PAT_AR_HP], cfg=cfg)
    utt = await say(h, DOC_DE.id, 1.5)
    assert not segs(h, "translation.final", utt)

    h = room([DOC_DE, PAT_AR_HP])
    h.p.mode.pipeline.simultaneous = "off"
    utt = await say(h, DOC_DE.id, 1.5)
    assert not segs(h, "translation.final", utt)

    # the room default: simultaneous only after 4 s of speech, at sentence ends (stub model)
    h = room([DOC_DE, PAT_AR_HP], after_ms=4000, complete_clauses=False, word_ms=380)
    utt = await say(h, DOC_DE.id, 3.5)  # a short turn: consecutive
    await h.p.drain()
    assert not segs(h, "translation.final", utt)
    h.set_next_line("a", 0)
    utt = await say(h, DOC_DE.id, 5.5)  # a long turn
    await h.p.drain()
    finals = segs(h, "translation.final", utt)
    assert [f["text"] for f in finals] == ["مرحبًا سيدة دمير. أنا الدكتور فيبر.", "ما الذي أتى بكِ إليّ اليوم؟"]
    assert idx(h, "translation.delta", utt, seg=0) < idx(h, "utterance.end", utt)


async def test_low_confidence_partials_never_commit(no_guard):
    h = room([DOC_EN, ParticipantInfo(id="b-k3x9", role="b", lang="en", audio_out="headphones"),
              ParticipantInfo(id="a-x", role="a", lang="ar", audio_out="headphones")])
    h.set_next_line("b", 3)  # sz-08, conf 0.4
    utt = await say(h, "b-k3x9", 2.0)
    await h.p.drain()
    assert not [t for t in h.types(utt) if t.startswith(("translation", "verify"))]


async def test_off_record_mid_utterance_stops_its_segments(no_guard):
    h = room([DOC_DE, PAT_AR_HP])
    Voice(h)
    utt = await speak(h, DOC_DE.id, 1.0)
    await until(lambda: segs(h, "translation.final", utt))
    await h.p.handle_control(Control(action="off_record", value=True))
    await h.p.speech_end(DOC_DE.id, None, 300)
    await h.p.drain()
    assert [f["seg"] for f in segs(h, "translation.final", utt)] == [0]
    assert not h.of("transcript.final", utt)


# ---------------------------------------------------------------- stable partial as final (both modes)


class _CountingASR:
    def __init__(self, inner, final_text: str | None = None, has_conf: bool = True) -> None:
        self.inner, self.final_text, self.has_conf = inner, final_text, has_conf
        self.name = "counting"
        self.finals = 0

    async def transcribe(self, audio, *, lang_hint, partial, turn):
        res = await self.inner.transcribe(audio, lang_hint=lang_hint, partial=partial, turn=turn)
        res.has_conf = self.has_conf
        if not partial:
            self.finals += 1
            if self.final_text:
                res.text = self.final_text
        return res


async def test_stable_partial_right_before_the_endpoint_is_the_final(no_guard):
    h = room([DOC_DE, PAT_AR_SPK])
    asr = h.engines.asr = _CountingASR(h.engines.asr)  # type: ignore[assignment]
    utt = await say(h, DOC_DE.id, 2.0)  # last two partials (1.5 s, 2.0 s) identical, the last at the endpoint
    await h.p.drain()
    assert asr.finals == 0
    final = h.one("transcript.final", utt)
    assert final["text"] == line("sz-01").text["de"] and final["asr_conf"] == pytest.approx(0.93)
    assert h.one("pipeline.timing", utt)["asr_ms"] < 5

    utt = await say(h, DOC_DE.id, 2.4)  # the last partial 400 ms before the endpoint: transcribe again
    assert asr.finals == 1
    asr.has_conf = False  # no real confidence from the engine: never reused
    await say(h, DOC_DE.id, 2.0)
    assert asr.finals == 2


async def test_whisper_reports_whether_it_had_scores():
    from raum.pipeline.asr import _has_scores

    assert _has_scores({"segments": [{"avg_logprob": -0.2}]}) is True
    assert _has_scores({"segments": [{"tokens": [{"text": "hi", "p": 0.6}]}]}) is True
    assert _has_scores({"text": "hi"}) is False and _has_scores({"segments": [{"text": "hi"}]}) is False


async def test_mock_partials_show_punctuation_once_the_next_word_is_heard():
    from raum.pipeline.asr import MockASR
    from raum.pipeline.base import TurnContext

    asr = MockASR({"latency_ms": 0, "word_ms": 100})
    t = TurnContext(room="r", mode="sprechzimmer", speaker="a-1", role="a", utt="u-1", speech_ms=200)
    p = await asr.transcribe(np.zeros(10, dtype=np.float32), lang_hint="de", partial=True, turn=t)
    assert p.text == "Guten Tag" and p.has_conf  # "Tag," is still being heard
    t.speech_ms = 300
    p = await asr.transcribe(np.zeros(10, dtype=np.float32), lang_hint="de", partial=True, turn=t)
    assert p.text == "Guten Tag, Frau"


# ---------------------------------------------------------------- with the semantic end of turn (turn.py)


async def pause(h: Harness, speaker: str, vad_ms: float = 20.0) -> None:
    await h.p.speech_pause(speaker, None, vad_ms)  # turn.py: starts the hold and returns


async def until_ended(h: Harness, utt: str) -> None:
    await until(lambda: h.of("utterance.end", utt))


async def test_a_pause_inside_a_simultaneous_turn_keeps_its_segments_going(no_guard):
    h = room([DOC_DE, PAT_AR_HP])
    h.p.eot.waits = [0.3, 0.0]  # the first pause is mid-utterance; the second ends the turn
    utt = await speak(h, DOC_DE.id, 1.1)  # "Guten Tag, Frau Demir. Ich bin Dr. Weber. Was führt" ... pause
    await pause(h, DOC_DE.id)
    await until(lambda: segs(h, "translation.final", utt))  # seg 0 done during the pause
    assert await h.p.speech_start(DOC_DE.id) == utt  # turn.py: the same utterance goes on
    await speak(h, DOC_DE.id, 0.5, start=False)
    await pause(h, DOC_DE.id)
    await until_ended(h, utt)
    await h.p.drain()
    finals = segs(h, "translation.final", utt)
    assert [f["seg"] for f in finals] == [0, 1, 2] and finals[-1]["segs"] == 3
    assert " ".join(f["text"] for f in finals) == line("sz-01").text["ar"]
    assert h.types(utt).count("utterance.start") == 1 and h.types(utt).count("utterance.end") == 1


async def test_pause_takes_the_running_partial_instead_of_a_new_asr_pass(no_guard):
    h = room([DOC_DE, PAT_AR_SPK], complete_clauses=False, partial_latency_ms=30)
    h.p.eot = Eot(waits=[0.0])
    asr = h.engines.asr = _CountingASR(h.engines.asr)  # type: ignore[assignment]
    utt = await speak(h, DOC_DE.id, 1.5)  # the 1.5 s partial is the whole sentence
    for _ in range(25):  # VAD hangover: the 2.0 s partial is still running when the pause comes
        await h.p.speech_audio(DOC_DE.id, np.zeros(320, dtype=np.float32))
    assert not h.p._active[DOC_DE.id].partial_task.done()
    await pause(h, DOC_DE.id)
    await until_ended(h, utt)
    await h.p.drain()
    assert asr.finals == 0 and h.one("transcript.final", utt)["text"] == line("sz-01").text["de"]
    assert h.one("pipeline.timing", utt)["asr_early"] is True


# ---------------------------------------------------------------- speculative translation


class _SpyTranslator:
    def __init__(self) -> None:
        self.inner = MockTranslator({"first_token_ms": 20, "token_ms": 1})
        self.name = "spy"
        self.texts: list[str] = []

    def stream(self, req):
        self.texts.append(req.text)
        return self.inner.stream(req)


async def test_speculation_on_a_complete_sentence_is_reused_when_the_final_matches(no_guard):
    h = room([DOC_DE, PAT_AR_SPK], complete_clauses=False)
    spy = h.engines.translate = _SpyTranslator()  # type: ignore[assignment]
    utt = await say(h, DOC_DE.id, 2.0)  # stable full sentence at 1.5 s and 2.0 s
    await h.p.drain()
    assert spy.texts == [line("sz-01").text["de"]] and h.p.spec.hits == 1  # one translation, made ahead
    deltas = h.of("translation.delta", utt)
    assert "".join(d.data["delta"] for d in deltas) == h.one("translation.final", utt)["text"]
    assert h.one("translation.final", utt)["text"] == line("sz-01").text["ar"]


async def test_speculation_is_discarded_when_the_final_differs(no_guard):
    h = room([DOC_DE, PAT_AR_SPK], complete_clauses=False)
    spy = h.engines.translate = _SpyTranslator()  # type: ignore[assignment]
    h.engines.asr = _CountingASR(h.engines.asr, final_text="Guten Tag, Frau Demir.")  # type: ignore[assignment]
    utt = await say(h, DOC_DE.id, 2.4)  # final ASR runs (gap 400 ms) and hears something else
    await h.p.drain()
    assert h.p.spec.hits == 0 and len(spy.texts) == 2
    assert h.one("translation.final", utt)["text"] == "مرحبًا سيدة دمير."
    assert "".join(d.data["delta"] for d in h.of("translation.delta", utt)) == "مرحبًا سيدة دمير."  # never speculative


async def test_pause_translates_and_pre_synthesises_while_the_end_of_turn_waits(no_guard):
    h = room([DOC_DE, PAT_AR_SPK], complete_clauses=False, final_reveal=True)
    h.p.eot = Eot(waits=[0.25])  # turn.py unsure: allow 250 ms of silence
    voice = Voice(h)
    utt = await speak(h, DOC_DE.id, 0.6)
    await pause(h, DOC_DE.id)
    await until_ended(h, utt)
    await h.p.drain()
    assert h.p.spec.hits == 1
    final = h.one("translation.final", utt)["text"]
    assert final == "مرحبًا سيدة دمير. أنا الدكتور"  # what was said: "Guten Tag, Frau Demir. Ich bin"
    assert voice.made[0] == "مرحبًا سيدة دمير." and voice.made_at[0] <= idx(h, "utterance.end", utt)
    assert voice.made == voice.spoken == ["مرحبًا سيدة دمير.", "أنا الدكتور"]  # played after the end, made once
    assert voice.at[0] > idx(h, "utterance.end", utt)
    t = h.one("pipeline.timing", utt)
    assert t["mt_first_ms"] - t["vad_ms"] < 15  # MT ~ 0 ms after the endpoint


class _PacedVoice(Voice):
    """A voice engine that takes the speaker's voice args (voice.py)."""

    async def synthesize(self, text, lang, *, voice_ref=None, rate=None):
        async for c in super().synthesize(text, lang):
            yield c


async def test_pre_synthesised_voice_plays_only_with_the_same_voice_args(no_guard):
    h = room([DOC_DE, PAT_AR_SPK], complete_clauses=False, final_reveal=True)
    h.p.eot = Eot(waits=[0.25])
    voice = _PacedVoice(h)
    utt = await speak(h, DOC_DE.id, 0.6)
    await pause(h, DOC_DE.id)
    await until_ended(h, utt)
    await h.p.drain()
    final = h.one("translation.final", utt)["text"]
    # made ahead with the speaker's voice args as known then; still the same at the live call: played as made
    assert voice.spoken == voice.made == ["مرحبًا سيدة دمير.", "أنا الدكتور"] and " ".join(voice.spoken) == final

    from raum.pipeline.speculate import _same_voice

    assert _same_voice({"voice_ref": None, "rate": None}, {}) and _same_voice({"rate": 1.0}, {"rate": 1.1})
    assert not _same_voice({"rate": 1.0}, {"rate": 1.3})  # the pace changed: synthesise again
    assert not _same_voice({"voice_ref": b"a"}, {"voice_ref": b"ab"})  # the voice sample grew


async def test_segments_and_another_utterance_into_one_language_speak_in_turn(no_guard):
    """Segments go through the room's voice lane: a second speaker's utterance into Arabic waits."""
    nurse = ParticipantInfo(id="a-nurse", role="a", lang="de")
    h = room([DOC_DE, nurse, PAT_AR_HP])
    voice = Voice(h)
    task = asyncio.create_task(say(h, DOC_DE.id, 1.5))  # simultaneous: segments while speaking
    await until(lambda: voice.spoken)
    h.set_next_line("a", 2)
    other = await say(h, nurse.id, 1.0)  # consecutive into the same language meanwhile
    await task
    await h.p.drain()
    doc = [f["text"] for f in segs(h, "translation.final", "u-0001")]
    theirs = h.of("translation.final", other)[-1].data["text"]
    i = voice.spoken.index(doc[-1])
    assert voice.spoken[:i + 1] == doc  # the doctor's segments, in order, not interleaved
    assert "".join(voice.spoken[i + 1:]).replace(" ", "") == theirs.replace(" ", "")


# ---------------------------------------------------------------- prompts, summary, core


def test_segment_request_keeps_the_fixed_system_prefix():
    base = {"src_lang": "de", "tgt_lang": "ar", "room_name": "Consultation room", "role_a": "Doctor",
            "role_b": "Patient", "lang_a": "de", "lang_b": "ar", "glossary": "Metformin = ميتفورمين",
            "context": "Names: Frau Demir"}
    plain = translate_messages("{src_lang} -> {tgt_lang}", TranslateRequest(text="Ich bin Dr. Weber.", **base))
    seg = translate_messages("{src_lang} -> {tgt_lang}", TranslateRequest(
        text="Ich bin Dr. Weber.", segment=True, earlier=[("Guten Tag, Frau Demir.", "مرحبًا سيدة دمير.")], **base))
    assert seg[:2] == plain[:2]  # byte-identical prefix: Ollama reuses its prompt cache
    assert seg[2]["role"] == "system" and "still talking" in seg[2]["content"]
    assert seg[3:] == [{"role": "user", "content": "Guten Tag, Frau Demir."},
                       {"role": "assistant", "content": "مرحبًا سيدة دمير."},
                       {"role": "user", "content": "Ich bin Dr. Weber."}]


def test_summary_joins_segments_in_order():
    from raum.summary import collect

    recs = [
        {"type": "transcript.final", "utt": "u-1", "ts": 1, "data": {"speaker": "a-1", "lang": "de",
                                                                     "text": "Guten Tag. Ich bin Dr. Weber.",
                                                                     "asr_conf": 0.9}},
        {"type": "translation.final", "utt": "u-1", "data": {"target_lang": "ar", "text": "B", "seg": 1}},
        {"type": "translation.final", "utt": "u-1", "data": {"target_lang": "ar", "text": "A", "seg": 0}},
        {"type": "translation.final", "utt": "u-1", "data": {"target_lang": "ar", "text": "B", "seg": 1, "segs": 2}},
    ]
    utts, _ = collect(recs)
    assert utts[0].translations == {"ar": "A B"}
    recs.append({"type": "translation.final", "utt": "u-1", "data": {"target_lang": "ar", "text": "A B C"}})
    assert collect(recs)[0][0].translations == {"ar": "A B C"}  # a merged utterance's final replaces it


def test_simul_constants_match_the_spec():
    assert simul.MIN_COMMA_WORDS == 6 and simul.REUSE_GAP_MS == 300 and simul.REUSE_MIN_CONF == 0.85
    assert "de" in simul.SENTENCE_ONLY_LANGS


@pytest.mark.filterwarnings("ignore:The HMAC key")
def test_core_forwards_confirm_to_the_agent(tmp_path, monkeypatch):
    """The agent must hear the confirm: it releases (or drops) the held segment's voice."""
    from core_doubles import FakePublisher, install_security, make_config
    from fastapi.testclient import TestClient

    from raum.api.server import create_app
    from raum.link import KEY_HEADER

    monkeypatch.delenv("RAUM_PROFILE", raising=False)
    install_security(monkeypatch, tmp_path)
    app = create_app(make_config(tmp_path))
    app.state.core.sessions.publisher = FakePublisher()
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        r = client.post("/api/join", json={"room": "sz-4821", "role": "b", "lang": "ar", "device": "phone",
                                           "consent": True})
        assert r.status_code == 200
        with client.websocket_connect("/api/internal/agent/sz-4821", headers={KEY_HEADER: "dev-internal"}) as agent:
            assert agent.receive_json()["kind"] == "state"
            r = client.post("/api/rooms/sz-4821/control", json={"action": "confirm", "by": "b",
                                                                "flag_id": "u-0001-ar-s0-confirm"})
            assert r.json()["forwarded"] is True
            msg = agent.receive_json()
            assert msg["kind"] == "control" and msg["control"]["action"] == "confirm"
            assert msg["control"]["flag_id"] == "u-0001-ar-s0-confirm" and msg["control"]["accepted"] is True


# ---------------------------------------------------------------- stop -> first audio (ops' latency round)


def test_voice_starts_on_a_first_phrase():
    from raum.pipeline.room import take_phrase

    assert take_phrase("Guten Tag, Frau Demir. Ich") == ("Guten Tag,", " Frau Demir. Ich")  # boundary first
    assert take_phrase("Ja. Gut") == ("Ja.", " Gut")  # a sentence end at once
    assert take_phrase("Ja, ich") == ("", "Ja, ich")  # a comma only from the 2nd word on
    assert take_phrase("Ich verschreibe Ihnen heute Ibuprofen gegen") == ("", "Ich verschreibe Ihnen heute Ibuprofen gegen")
    head, rest = take_phrase("Ich verschreibe Ihnen heute Ibuprofen gegen die Schmerzen in Ihrem rechten Knie und ")
    assert (head, rest) == ("Ich verschreibe Ihnen heute Ibuprofen gegen die Schmerzen in Ihrem rechten Knie", " und ")
    assert take_phrase("Nehmen Sie zweimal täglich 500 mg Metformin, immer")[0] == "Nehmen Sie zweimal täglich 500 mg Metformin,"
    assert take_phrase("Ich bin Dr. Weber und so")[0] == ""  # "Dr." is no boundary
    assert take_phrase("Ich bin") == ("", "Ich bin")  # "bin" not complete yet: still streaming


async def test_pause_speculation_runs_beside_the_verdict_and_plays_only_after_the_end(no_guard):
    h = room([DOC_DE, PAT_AR_SPK], complete_clauses=False, final_reveal=True)

    class SlowVerdict(Eot):
        async def verdict(self, text, lang, audio=None, **kw):
            await asyncio.sleep(0.15)  # the turn models take their time; MT runs meanwhile
            return Verdict(0.0, "test")

    h.p.eot = SlowVerdict()
    voice = Voice(h)
    utt = await speak(h, DOC_DE.id, 1.6)
    await pause(h, DOC_DE.id)
    await until_ended(h, utt)
    await h.p.drain()
    assert h.p.spec.hits == 1
    assert voice.made_at[0] <= idx(h, "utterance.end", utt) < voice.at[0]  # made during the verdict, played after
    t = h.one("pipeline.timing", utt)
    assert t["asr_from"] in ("partial", "early") and t["mt_first_ms"] - t["vad_ms"] < 15


async def test_resume_during_the_end_of_turn_wait_discards_the_speculation(no_guard):
    h = room([DOC_DE, PAT_AR_SPK], complete_clauses=False, final_reveal=True)
    h.p.eot = Eot(waits=[0.4, 0.0])
    voice = Voice(h)
    utt = await speak(h, DOC_DE.id, 0.6)  # "Guten Tag, Frau Demir. Ich bin" ... pause: translated ahead
    await pause(h, DOC_DE.id)
    await until(lambda: voice.made)  # its first phrase is already synthesised (not played)
    assert await h.p.speech_start(DOC_DE.id) == utt  # the speaker goes on
    assert not h.p.spec._specs and not voice.spoken
    await speak(h, DOC_DE.id, 1.0, start=False)
    await pause(h, DOC_DE.id)
    await until_ended(h, utt)
    await h.p.drain()
    assert h.one("translation.final", utt)["text"] == line("sz-01").text["ar"]
    assert "".join(voice.spoken).replace(" ", "") == line("sz-01").text["ar"].replace(" ", "")  # nothing stale


async def test_timing_says_which_transcript_and_when_the_voice_was_made_and_published(no_guard):
    h = room([DOC_DE, PAT_AR_SPK], complete_clauses=False)
    voice = Voice(h)
    published: list[float] = []

    async def sink(lang, pcm, rate):
        await voice.sink(lang, pcm, rate)
        await asyncio.sleep(0.01)  # the AudioSource queue
        published.append(time.monotonic())
        return published[-1]

    h.p.tts_sink = sink
    utt = await say(h, DOC_DE.id, 2.0)  # stable partial at the endpoint: no final ASR pass
    await h.p.drain()
    t = h.one("pipeline.timing", utt)
    assert t["asr_from"] == "partial"
    assert t["tts_synth_first_ms"] is not None and t["tts_publish_first_ms"] >= t["tts_synth_first_ms"] + 9
    utt = await say(h, DOC_DE.id, 2.4)
    await h.p.drain()
    assert h.one("pipeline.timing", utt)["asr_from"] == "final"


async def test_worker_sink_reports_when_the_first_frame_was_accepted(agent):  # noqa: F811  (the fixture)
    t = time.monotonic()
    took = await agent._tts_sink("de", np.ones(4410, dtype=np.int16), 22050)
    assert isinstance(took, float) and t <= took <= time.monotonic()
    assert await agent._tts_sink("de", np.zeros(0, dtype=np.int16), 22050) is None


def test_pre_synthesis_uses_the_pace_this_turn_will_report():
    from raum.pipeline.simul import speaker_voice
    from raum.pipeline.voice import SpeakingRate

    h = room([DOC_DE, PAT_AR_SPK])
    text = "Sind Sie gegen irgendwelche Medikamente allergisch?"
    ahead = speaker_voice(h.p, DOC_DE.id, text, 2.2).rate
    assert speaker_voice(h.p, DOC_DE.id).rate is None and DOC_DE.id not in h.p._rates  # nothing kept
    assert ahead == SpeakingRate(h.p.base_wps).update(text, 2.2)  # what the live _voice_args will compute


async def test_speculation_stopped_midway_finishes_with_the_live_engine():
    """A speculation cancelled after a few tokens: the turn's translation goes on with the live engine from where
    the speculation stopped, never ends there ("Продолжайте принимать Метформин по")."""
    from raum.pipeline.speculate import _Spec

    full = "Продолжайте принимать Метформин по 500 мг два раза в день."

    class Live:
        name = "live"

        async def stream(self, req):
            for w in full.split(" "):
                yield w if w == full.split(" ")[0] else " " + w

    h = room([DOC_DE, PAT_AR_SPK], complete_clauses=False)
    h.engines.translate = Live()  # type: ignore[assignment]
    req = TranslateRequest(text="x", src_lang="de", tgt_lang="ru", room_name="r", role_a="a", role_b="b", lang_a="de",
                           lang_b="ru")
    for sent in (["Продолжайте", " принимать", " Метформин", " по"], ["Продол"], []):
        spec = _Spec(key=("k",), speaker=DOC_DE.id, slot="background", text="x")
        spec.parts, spec.done, spec.failed = list(sent), True, True
        out = [d async for d in h.p.spec._replay(spec, req)]
        assert "".join(out) == full, sent
