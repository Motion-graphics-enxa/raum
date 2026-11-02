"""Voice echo check (echo_check.py): the box hears its own voice back for the figures of a clause. A voice
that said "50 mg" for "500 mg" (the GX10 TTS bake-off) -> ``voice.echo`` on the listener's card, the clause once
more in the standard voice ("Повторяю: ..."), an audit line with the kind only, ``echo_ms`` in the timing."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import numpy as np
import pytest
from core_doubles import FakePublisher, install_security, make_config
from pipeline_helpers import DOC, Harness, fast_config
from test_pipeline_simul import config as simul_config
from test_pipeline_simul import room as simul_room
from test_pipeline_simul import say as simul_say
from test_pipeline_simul import segs, until

from raum.config import RaumConfig
from raum.events import ParticipantInfo, make_event
from raum.pipeline import echo_check as E
from raum.pipeline import texts as T
from raum.pipeline.base import AsrResult
from raum.pipeline.echo_check import compare, enabled_in, to_asr_input
from raum.pipeline.voice import VoiceArgs, VoiceRef
from raum.scheduler import Scheduler

RU = ParticipantInfo(id="b-ru01", role="b", lang="ru")
CLAUSE_RU = "Продолжайте принимать Метформин по 500 мг"  # sz-05's first spoken phrase in Russian
DOC_EN = ParticipantInfo(id="a-doc1", role="a", lang="en")
PAT_AR_HP = ParticipantInfo(id="b-k3x9", role="b", lang="ar", audio_out="headphones")


# ---------------------------------------------------------------- doubles


def encode(text: str) -> np.ndarray:
    """What the voice says, as int16 PCM (the UTF-8 bytes)."""
    raw = text.encode("utf-8")
    return np.frombuffer(raw + b"\0" * (len(raw) % 2), dtype="<i2").copy()


def decode(audio: np.ndarray) -> str:
    """The ASR side: 16 kHz float back to the words (exact: int16 / 32768 is exact in float32)."""
    pcm = np.round(np.asarray(audio, dtype=np.float32) * 32768.0).astype("<i2")
    return pcm.tobytes().rstrip(b"\0").decode("utf-8")


@dataclass
class Said:
    text: str  # what it was asked to say
    lang: str
    cloned: bool  # with the speaker's voice sample (else the service's standard voice)
    said: str  # what it actually said
    lane_held: bool  # the language's voice lane was held while it spoke


class TextVoice:
    """A cloning voice service whose PCM encodes what it says. ``slips`` = {written: said}: the cloned voice
    gets those wrong (Chatterbox: "50 мг" for "500 мг"); the standard voice says the text as written."""

    name = "text-voice"
    is_mock = False
    simulated = False
    uses_voice_ref = True
    sample_rate = 16000

    def __init__(self, h: Harness, slips: dict[str, str] | None = None) -> None:
        self.h = h
        self.slips = dict(slips or {})
        self.calls: list[Said] = []

    async def synthesize(self, text, lang, *, voice_ref=None, rate=None):
        said = text
        if voice_ref is not None:
            for a, b in self.slips.items():
                said = said.replace(a, b)
        lane = self.h.p._voice_lanes.get(lang)
        self.calls.append(Said(text, lang, voice_ref is not None, said, bool(lane and lane.locked())))
        for part in np.array_split(encode(said), 3):
            await asyncio.sleep(0.001)
            yield part


class EchoASR:
    """The room's ASR: live calls go to the mock (canned lines); an echo call (the box's own voice) decodes the
    voice's PCM, through ``hear`` (drop or change words). Records each echo call with the scheduler state."""

    is_mock = False
    name = "echo-asr"

    def __init__(self, h: Harness, hear=None, fail: bool = False, delay_s: float = 0.0) -> None:
        self.inner = h.engines.asr
        self.h = h
        self.hear = hear or (lambda text: text)
        self.fail = fail
        self.delay_s = delay_s
        self.echo: list[dict] = []
        self.live: list[float] = []

    def __getattr__(self, name):  # _cursor, reset, line_for: the mock's
        return getattr(self.inner, name)

    async def transcribe(self, audio, *, lang_hint, partial, turn):
        if turn.speaker != E.ECHO_SPEAKER:
            self.live.append(time.monotonic())
            return await self.inner.transcribe(audio, lang_hint=lang_hint, partial=partial, turn=turn)
        sched = self.h.p.sched
        self.echo.append({"lang": lang_hint, "partial": partial, "at": time.monotonic(),
                          "background": sched.running["background"], "dtype": str(audio.dtype),
                          "text": decode(audio)})
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("whisper down")
        return AsrResult(text=self.hear(decode(audio)), lang=lang_hint or "", conf=0.9)


def echo_cfg(on: bool = True) -> RaumConfig:
    cfg = fast_config()
    cfg.models.tts.options = {"first_audio_ms": 1, **({"echo_check": "facts"} if on else {})}
    return cfg


def echo_room(slips=None, hear=None, *, on: bool = True, participants=None, **asr_kw):
    """Doctor (de) -> Russian listener; the doctor's voice is cloned (a 4 s sample in memory)."""
    h = Harness(participants=participants or [DOC, RU], cfg=echo_cfg(on))
    voice = TextVoice(h, slips)
    asr = EchoASR(h, hear, **asr_kw)
    h.engines.tts = voice  # type: ignore[assignment]
    h.engines.asr = asr  # type: ignore[assignment]
    h.p.echo.tail_s = 0.01
    ref = VoiceRef(max_s=8.0)
    ref.add(np.full(4 * 16000, 0.01, dtype=np.float32))
    h.p._voice_refs[DOC.id] = ref
    state = {"writers": {}, "violations": [], "audio": []}

    async def sink(lang, pcm, rate):  # like AudioSource.capture_frame: one writer per language at a time
        w = state["writers"]
        if w.get(lang):
            state["violations"].append(lang)
        w[lang] = True
        try:
            await asyncio.sleep(0.001)
            state["audio"].append((lang, pcm))
        finally:
            w[lang] = False

    h.p.tts_sink = sink
    return h, voice, asr, state


def timing(h: Harness, utt: str, lang: str) -> dict:
    found = [e.data for e in h.of("pipeline.timing", utt) if e.data.get("target_lang") == lang]
    assert len(found) == 1, found
    return found[0]


# ---------------------------------------------------------------- the comparison (pure)


@pytest.mark.parametrize("lang,written,heard", [
    ("ru", CLAUSE_RU, "Продолжайте принимать метформин по 500 миллиграмм"),  # unit word vs symbol
    ("en", "Keep taking 500 mg of Metformin.", "Keep taking five hundred milligrams of metformin."),
    ("de", "Nehmen Sie 500 mg am 12. November um 9 Uhr.",
     "Nehmen Sie fünfhundert Milligramm am zwölften November um neun Uhr."),
    ("tr", "Günde iki kez 500 mg alın.", "Günde iki kez 500 miligram alın."),
    ("ar", "آخذ 500 ملغ من ميتفورمين.", "آخذ 500 ملغ من ميتفورمين"),
    ("en", "Take 2 tablets at 9:00.", "Take two tablets at nine."),
])
def test_same_figure_said_another_way_matches(lang, written, heard):
    v = compare(written, heard, lang)
    assert v.status == "match" and v.figures == v.heard > 0 and not v.mismatches


@pytest.mark.parametrize("lang,written,heard,kind,spoken", [
    ("ru", CLAUSE_RU, "Продолжайте принимать Метформин по 50 мг", "dose", "50 мг"),  # the bake-off
    ("ru", CLAUSE_RU, "Продолжайте принимать метформин по 50 миллиграмм", "dose", "50 миллиграмм"),
    ("en", "Keep taking 500 mg of Metformin.", "Keep taking fifty milligrams of metformin.", "dose", "fifty milligrams"),
    ("de", "Kommen Sie am 12. November wieder.", "Kommen Sie am 13. November wieder.", "date", "13. November"),
    ("ar", "خذ 400 ملغ.", "خذ 40 ملغ.", "dose", "40 ملغ"),
])
def test_a_figure_heard_with_another_value_is_a_mismatch(lang, written, heard, kind, spoken):
    v = compare(written, heard, lang)
    assert v.status == "mismatch"
    assert [(m.kind, m.spoken) for m in v.mismatches] == [(kind, spoken)]


@pytest.mark.parametrize("lang,written,heard", [
    ("ru", CLAUSE_RU, "Продолжайте принимать метформин"),  # the ASR dropped it
    ("ru", CLAUSE_RU, "Продолжайте принимать метформин по пятьсот миллиграмм"),  # words the guard cannot read
    ("ru", "Возьмите 25 мг.", "Возьмите двадцать пять миллиграмм."),  # read as "пять": only in part
    ("ar", "خذ 25 ملغ.", "خذ خمسة وعشرين ملغ."),
    ("en", "Take 2.5 mg.", "Take two point five milligrams."),
    ("de", "Kommen Sie am 6. November.", "Kommen Sie im November."),  # a month without its day
    ("de", "Kommen Sie am 6. November.", "Kommen Sie am Freitag."),  # a weekday for a date
])
def test_a_figure_not_heard_or_only_in_part_is_unclear_never_a_mismatch(lang, written, heard):
    v = compare(written, heard, lang)
    assert v.status == "unclear" and not v.mismatches and v.unclear


def test_extra_figures_heard_are_ignored():
    assert compare("Take 2 tablets.", "Take 2 tablets, 3 times.", "en").status == "match"


def test_option_values():
    assert enabled_in({"echo_check": "facts"}) and enabled_in({"echo_check": True})  # YAML "on"
    assert not enabled_in({}) and not enabled_in({"echo_check": "off"}) and not enabled_in({"echo_check": False})


def test_voice_pcm_becomes_the_asrs_16_khz_float_input():
    t = np.arange(24000) / 24000.0
    pcm = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)  # 1 s at 24 kHz (the http voice)
    audio = to_asr_input(np.array_split(pcm, 7), 24000)
    assert audio.dtype == np.float32 and abs(len(audio) - 16000) <= 2
    assert 0.4 < float(np.max(np.abs(audio))) <= 0.6
    crossings = np.count_nonzero(np.diff(np.signbit(audio[100:-100])))
    assert abs(crossings - 2 * 440 * (len(audio) - 200) / 16000) < 6  # still 440 Hz
    same = to_asr_input([encode("500 мг")], 16000)
    assert decode(same) == "500 мг"  # 16 kHz input: no resampling


def test_repeat_lines_in_the_five_languages():
    assert T.REPEAT == {"de": "Ich wiederhole:", "en": "I repeat:", "ru": "Повторяю:", "tr": "Tekrar ediyorum:",
                        "ar": "أكرر:"}


def test_events_and_profiles(monkeypatch):
    env = make_event("voice.echo", "sz-1", {"target_lang": "ru", "kind": "dose", "spoken": "50 мг",
                                            "written": "500 мг", "clause": 0, "text": CLAUSE_RU}, utt="u-0001")
    assert env.payload().spoken == "50 мг"
    make_event("pipeline.timing", "sz-1", {"echo_ms": 212.5, "echo_check": "mismatch"})
    from raum.config import load_config

    monkeypatch.setenv("RAUM_PROFILE", "spark")
    assert enabled_in(load_config().models.tts.options)  # the GX10: facts
    monkeypatch.setenv("RAUM_PROFILE", "mock")
    assert not enabled_in(load_config().models.tts.options)  # mock: off (and a mock voice is never checked)


# ---------------------------------------------------------------- the pipeline (consecutive)


async def test_mismatch_marks_the_card_repeats_once_in_the_standard_voice_and_times_it():
    h, voice, asr, state = echo_room(slips={"500 мг": "50 мг"})
    h.set_next_line("a", 2)  # sz-05: "Nehmen Sie zweimal täglich 500 mg Metformin weiter, ..."
    utt = await h.say(DOC.id)
    await h.p.drain()

    echo = h.one("voice.echo", utt)
    assert echo == {"target_lang": "ru", "kind": "dose", "spoken": "50 мг", "written": "500 мг", "clause": 0,
                    "text": CLAUSE_RU}
    first = voice.calls[0]
    assert first.text == CLAUSE_RU and first.cloned and first.said.endswith("50 мг")
    repeats = [c for c in voice.calls if c.text.startswith("Повторяю: ")]
    assert len(repeats) == 1  # once
    assert repeats[0].text == f"Повторяю: {CLAUSE_RU}" and not repeats[0].cloned  # the standard voice
    assert repeats[0].said.endswith("500 мг") and repeats[0].lane_held  # through the language's voice lane
    assert [a["text"] for a in asr.echo] == [first.said]  # one check (the clause with a figure); the repeat: none
    assert asr.echo[0]["lang"] == "ru" and asr.echo[0]["partial"] is False and asr.echo[0]["dtype"] == "float32"
    assert state["violations"] == []  # one writer on tts-ru at any time
    heard = b"".join(np.asarray(pcm, "<i2").tobytes() for lang, pcm in state["audio"] if lang == "ru")
    assert f"Повторяю: {CLAUSE_RU}" in heard.replace(b"\0", b"").decode("utf-8")  # the listener heard the repeat
    t = timing(h, utt, "ru")
    assert t["echo_check"] == "mismatch" and isinstance(t["echo_ms"], float) and t["echo_ms"] >= 0
    assert h.p.echo_check.repeats == 1
    assert not h.p._voice_lanes["ru"].locked()


async def test_the_voice_saying_it_right_changes_nothing():
    h, voice, asr, _ = echo_room()
    h.set_next_line("a", 2)
    utt = await h.say(DOC.id)
    await h.p.drain()
    assert len(asr.echo) == 1 and not h.of("voice.echo")
    assert not any(c.text.startswith("Повторяю") for c in voice.calls)
    t = timing(h, utt, "ru")
    assert t["echo_check"] == "match" and t["echo_ms"] is not None


async def test_a_figure_the_asr_did_not_hear_is_unclear_not_repeated():
    h, voice, asr, _ = echo_room(slips={"500 мг": "50 мг"}, hear=lambda text: text.split(" по ")[0])
    h.set_next_line("a", 2)
    utt = await h.say(DOC.id)
    await h.p.drain()
    assert len(asr.echo) == 1 and not h.of("voice.echo")
    assert not any(c.text.startswith("Повторяю") for c in voice.calls)
    assert timing(h, utt, "ru")["echo_check"] == "unclear"


async def test_option_off_never_calls_the_asr():
    h, voice, asr, _ = echo_room(slips={"500 мг": "50 мг"}, on=False)
    h.set_next_line("a", 2)
    utt = await h.say(DOC.id)
    await h.p.drain()
    assert voice.calls and asr.echo == [] and not h.of("voice.echo")
    t = timing(h, utt, "ru")
    assert "echo_ms" not in t or t["echo_ms"] is None
    assert t.get("echo_check") is None


async def test_mock_or_simulated_voice_is_never_checked():
    h, _, asr, _ = echo_room()
    h.engines.tts.simulated = True  # type: ignore[union-attr]
    h.set_next_line("a", 2)
    await h.say(DOC.id)
    await h.p.drain()
    assert asr.echo == [] and not h.p.echo_check.enabled


async def test_clauses_without_figures_are_never_transcribed():
    h, voice, asr, _ = echo_room()
    utt = await h.say(DOC.id)  # sz-01: "Guten Tag, Frau Demir. Ich bin Dr. Weber. ..." (no figure)
    await h.p.drain()
    assert voice.calls and asr.echo == []
    assert timing(h, utt, "ru").get("echo_check") is None


async def test_asr_failing_or_busy_skips_silently(monkeypatch):
    h, voice, asr, _ = echo_room(slips={"500 мг": "50 мг"}, fail=True)
    h.set_next_line("a", 2)
    utt = await h.say(DOC.id)
    await h.p.drain()
    assert len(asr.echo) == 1 and not h.of("voice.echo")
    assert timing(h, utt, "ru")["echo_check"] == "skipped"
    assert not any(c.text.startswith("Повторяю") for c in voice.calls)

    monkeypatch.setattr(E, "ECHO_WAIT_S", 0.05)
    h2, _, asr2, _ = echo_room(slips={"500 мг": "50 мг"})
    h2.p.scheduler = Scheduler(live_slots=1, check_slots=1, background_slots=1)
    await h2.p.sched.acquire("background")  # something else holds the background slot meanwhile
    h2.set_next_line("a", 2)
    utt2 = await h2.say(DOC.id)
    await h2.p.drain()
    h2.p.sched.release("background")
    assert asr2.echo == [] and not h2.of("voice.echo")
    assert timing(h2, utt2, "ru")["echo_check"] == "skipped"


async def test_live_asr_is_never_queued_behind_the_echo_check():
    """The echo transcription takes the background slot: while a final ASR waits for a live slot it does not
    start, and the clause's voice never waits for it."""
    h, _, asr, _ = echo_room(slips={"500 мг": "50 мг"})
    sched = h.p.scheduler = Scheduler(live_slots=1, check_slots=1, background_slots=1)
    order: list[str] = []
    await sched.acquire("live")  # a final ASR running

    async def final_asr():  # and the next one waiting for its live slot
        async with sched.slot("live"):
            order.append("live")
            await asyncio.sleep(0.01)

    waiting = asyncio.create_task(final_asr())
    await asyncio.sleep(0)
    marks: dict[str, float] = {}
    h.p.echo_check.track(marks, "u-0042", "ru")
    t0 = time.monotonic()
    cloned = VoiceArgs(ref=h.p._voice_refs[DOC.id])
    await h.p._speak("ru", CLAUSE_RU, t0, cloned, marks)  # the voice: done at once, its check queued
    spoke_s = time.monotonic() - t0
    await asyncio.sleep(0.05)
    assert asr.echo == [] and sched.stats()["background"]["running"] == 0  # not before the live work
    sched.release("live")
    await waiting
    order.append("echo")
    await until(lambda: asr.echo, timeout=2.0)
    assert order == ["live", "echo"] and asr.echo[0]["background"] == 1  # in the background slot
    assert spoke_s < 0.05  # the clause was not held up by its check
    out = await h.p.echo_check.settle(marks)
    assert out["echo_check"] == "mismatch"
    await h.p.drain()


async def test_repeat_shares_the_lane_with_the_next_utterance_one_writer():
    """Two utterances with a dose back to back into Russian: every repeat goes through the ru lane between
    the utterances' clauses, never beside them."""
    h, voice, asr, state = echo_room(slips={"500 мг": "50 мг"})
    h.set_next_line("a", 2)
    await asyncio.gather(h.say(DOC.id), asyncio.sleep(0))
    h.set_next_line("a", 2)
    await h.say(DOC.id)
    await h.p.drain()
    assert state["violations"] == []
    assert all(c.lane_held for c in voice.calls)  # every clause and repeat spoke from inside the lane
    assert len([c for c in voice.calls if c.text.startswith("Повторяю")]) == 2  # one per utterance
    assert len(h.of("voice.echo")) == 2 and len(asr.echo) == 2


async def test_burn_drops_a_pending_repeat():
    h, voice, asr, _ = echo_room(slips={"500 мг": "50 мг"}, delay_s=0.05)
    h.set_next_line("a", 2)
    await h.say(DOC.id)
    await until(lambda: asr.echo)
    h.p._burn()
    await h.p.drain()
    assert not h.of("voice.echo") and not any(c.text.startswith("Повторяю") for c in voice.calls)


async def test_audit_gets_the_kind_only_and_the_store_the_note(tmp_path, monkeypatch):
    from raum.sessions import SessionManager

    install_security(monkeypatch, tmp_path)
    sm = SessionManager(make_config(tmp_path), publisher=FakePublisher())
    room = sm.room("sz-1000")
    sm.ensure_session(room)
    h, _, _, _ = echo_room(slips={"500 мг": "50 мг"})
    emit = h.p._emit

    async def emit_and_ingest(env):
        await emit(env)
        await sm.ingest("sz-1000", env)

    h.p._emit = emit_and_ingest
    h.set_next_line("a", 2)
    await h.say(DOC.id)
    await h.p.drain()
    entries = [e for e in sm.audit.entries(session=room.session.id) if e["event"] == "voice.echo_mismatch"]
    assert [e["meta"] for e in entries] == [{"kind": "dose", "target_lang": "ru"}]
    line = json.dumps(entries)
    assert "мг" not in line and "Метформин" not in line  # no figure, no words
    stored = [r for r in room.session.store.read_all() if r.get("type") == "voice.echo"]
    assert len(stored) == 1 and stored[0]["data"]["spoken"] == "50 мг"  # content: encrypted store only


# ---------------------------------------------------------------- simultaneous mode


async def test_simultaneous_segment_with_a_wrong_figure_is_repeated_and_timed():
    cfg = simul_config()
    cfg.models.tts.options = {**cfg.models.tts.options, "echo_check": "facts"}
    h = simul_room([DOC_EN, PAT_AR_HP], cfg=cfg)
    h.p.mode.guard.confirm_on = []  # no confirm prompt holding the segment's voice
    voice = TextVoice(h, slips={"500 ملغ": "50 ملغ"})
    asr = EchoASR(h)
    h.engines.tts = voice  # type: ignore[assignment]
    h.engines.asr = asr  # type: ignore[assignment]
    h.p.tts_sink = _quiet_sink
    ref = VoiceRef(max_s=8.0)
    ref.add(np.full(4 * 16000, 0.01, dtype=np.float32))
    h.p._voice_refs[DOC_EN.id] = ref
    h.set_next_line("a", 2)  # sz-05: "Keep taking 500 mg of Metformin twice a day, in the morning ..."
    utt = await simul_say(h, DOC_EN.id, 2.0)
    await h.p.drain()
    seg0 = segs(h, "translation.final", utt)[0]["text"]
    assert "500 ملغ" in seg0
    echo = h.one("voice.echo", utt)
    assert echo["spoken"] == "50 ملغ" and echo["written"] == "500 ملغ" and echo["text"].startswith(seg0[:20])
    repeats = [c for c in voice.calls if c.text.startswith("أكرر: ")]
    assert len(repeats) == 1 and not repeats[0].cloned and repeats[0].lane_held
    t = h.one("pipeline.timing", utt)
    assert t["simultaneous"] is True and t["echo_check"] == "mismatch" and t["echo_ms"] is not None


async def _quiet_sink(lang, pcm, rate):
    await asyncio.sleep(0)


async def test_a_merged_cut_utterance_is_never_repeated():
    """merge.py stopped the cut half's voice: its check (still running) and any repeat must not say it again."""
    h, voice, asr, _ = echo_room(slips={"500 мг": "50 мг"}, delay_s=0.05)
    h.set_next_line("a", 2)
    utt = await h.say(DOC.id)
    await until(lambda: asr.echo)  # the check is running
    h.p.echo_check.drop(utt)
    await h.p.drain()
    assert not any(c.text.startswith("Повторяю") for c in voice.calls)
    assert h.p.echo_check.repeats == 0
