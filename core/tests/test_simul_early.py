"""Phase 2 of simultaneous mode (simul.py): a clause commits on ONE partial when whisper heard its boundary word
>= 250 ms before the end of the audio (and it holds no number, dose, negation or date); SVO phrase commits for
en / ru (never German); a candidate clause translated and voiced ahead, played only when the commit confirms the
same source; the catch-up pace decided only at sentence starts. Mock engines."""

from __future__ import annotations

import math

import pytest
from pipeline_helpers import not_implemented
from test_pipeline_simul import DOC_DE, DOC_EN, PAT_AR_HP, Voice, _SpyTranslator, idx, room, say, segs

from raum.events import ParticipantInfo
from raum.pipeline import simul
from raum.pipeline.base import AsrResult
from raum.pipeline.simul import _Lane, _Seg, svo_points, word_ends
from raum.pipeline.voice import VoiceArgs
from raum.verify import guard

DOC_RU = ParticipantInfo(id="a-doc1", role="a", lang="ru")


@pytest.fixture
def no_guard(monkeypatch):
    monkeypatch.setattr(guard, "check", not_implemented)
    monkeypatch.setattr(guard, "extract", not_implemented)


class ScriptASR:
    """Partials reveal ``text`` as the mock does (``word_s`` per word) with whisper-style word timings; or, with
    ``partials``, return those texts in turn. The final is ``final`` (or the whole text)."""

    name = "script"

    def __init__(self, text: str, lang: str, *, partials: list[str] | None = None, final: str | None = None,
                 word_s: float = 0.1, timed: bool = True) -> None:
        self.text, self.lang, self.partials, self.final = text, lang, list(partials or []), final or text
        self.word_s, self.timed, self.calls = word_s, timed, 0

    async def transcribe(self, audio, *, lang_hint, partial, turn):
        audio_s = len(audio) / 16000.0
        if not partial:
            text = self.final
        elif self.partials:
            text = self.partials[min(self.calls, len(self.partials) - 1)]
            self.calls += 1
        else:
            words = self.text.split()
            n = min(len(words), max(1, math.ceil(audio_s / self.word_s)))
            shown = words[:n]
            if n < len(words):
                shown[-1] = shown[-1].rstrip(".,!?")
            text = " ".join(shown)
        words = [{"word": f" {w}", "start": i * self.word_s, "end": (i + 1) * self.word_s, "probability": 0.95}
                 for i, w in enumerate(text.split())] if self.timed else []
        return AsrResult(text=text, lang=self.lang, conf=0.95, has_conf=True, words=words)


def partial_at(h, utt: str, k: int) -> int:
    """Event index of the k-th (0-based) transcript.partial of ``utt``."""
    return [i for i, e in enumerate(h.events) if e.type == "transcript.partial" and e.utt == utt][k]


# ---------------------------------------------------------------- one partial is enough (with timings)


def test_word_ends_spell_the_transcript_or_stop():
    toks = ["Hello,", "Mrs", "Demir."]
    whole = [{"word": " Hello,", "end": 0.4}, {"word": " Mrs", "end": 0.6}, {"word": " De", "end": 0.8},
             {"word": "mir.", "end": 1.0}]  # whisper.cpp pieces: a sub-word token without a leading space
    assert word_ends(whole, toks) == [0.4, 0.6, 1.0]
    assert word_ends([{"word": "Hello,", "end": 0.4}, {"word": "Mrs", "end": 0.6}], toks) == [0.4, 0.6, None]
    assert word_ends([{"word": " Hallo", "end": 0.4}], toks) == [None, None, None]  # not this transcript
    assert word_ends([], toks) == [None, None, None]


async def test_one_partial_commits_a_clause_whisper_heard_well_before_the_end(no_guard):
    """sz-01 (en): the first partial (0.7 s) shows "Hello, Mrs Demir." with "Demir." ending at 0.3 s: committed
    at once, not one partial later. Without word timings it waits for the second partial."""
    h = room([DOC_EN, PAT_AR_HP], partial_every_ms=700, word_times=True)
    utt = await say(h, DOC_EN.id, 1.5)
    await h.p.drain()
    first = idx(h, "translation.delta", utt, seg=0)
    assert first < partial_at(h, utt, 1)  # on the first partial
    assert segs(h, "translation.final", utt)[0]["text"] == "مرحبًا سيدة دمير."
    assert h.one("pipeline.timing", utt)["stages"]["simul.commit"]["once"] >= 1

    h = room([DOC_EN, PAT_AR_HP], partial_every_ms=700)  # no word timings: two partials must agree
    utt = await say(h, DOC_EN.id, 1.5)
    await h.p.drain()
    assert idx(h, "translation.delta", utt, seg=0) > partial_at(h, utt, 1)


async def test_a_clause_with_a_number_waits_for_two_partials(no_guard):
    """sz-05 (en): "Keep taking 500 mg of Metformin twice a day," heard well before the end of the first
    partial, but it holds a dose: committed only when the second partial agrees."""
    h = room([DOC_EN, PAT_AR_HP], partial_every_ms=1300, word_times=True)
    h.set_next_line("a", 2)
    utt = await say(h, DOC_EN.id, 2.6)
    await h.p.drain()
    assert idx(h, "translation.final", utt, seg=0) > partial_at(h, utt, 1)
    assert simul.risky("Keep taking 500 mg of Metformin twice a day,", "en")
    assert simul.risky("Do not take it", "en") and simul.risky("Nehmen Sie das ab Montag", "de")
    assert not simul.risky("Hello, Mrs Demir.", "en")


# ---------------------------------------------------------------- SVO phrases (en, ru; never German)


def test_svo_points_only_for_svo_languages_and_nothing_pending():
    en = ["I", "take", "my", "medicine", "with", "a", "glass", "of", "water"]
    assert svo_points(en, [], "en") == [4]
    assert svo_points(["I", "do", "not", "take", "my", "medicine", "with", "water"], [], "en") == []  # negation
    assert svo_points(["I", "take", "my", "medicine", "with", "2", "glasses"], [], "en") == []  # a number right after
    assert svo_points(["Я", "принимаю", "это", "лекарство", "с", "водой"], [], "ru") == [4]
    assert svo_points(["Ich", "nehme", "dieses", "Medikament", "mit", "Wasser"], [], "de") == []


@pytest.mark.parametrize(("who", "text", "chunk"), [
    (DOC_EN, "I take my medicine with a glass of water every morning.", "I take my medicine"),
    (DOC_RU, "Я принимаю это лекарство с водой каждое утро.", "Я принимаю это лекарство"),
    (DOC_DE, "Ich nehme dieses Medikament mit Wasser jeden Morgen.", None),
])
async def test_svo_phrase_commits_before_the_comma_in_en_and_ru_not_de(no_guard, who, text, chunk):
    h = room([who, PAT_AR_HP])
    h.engines.asr = ScriptASR(text, who.lang)  # type: ignore[assignment]
    utt = await say(h, who.id, 1.4)
    await h.p.drain()
    finals = segs(h, "translation.final", utt)
    if chunk is None:  # German: never a phrase without its clause end
        assert not [f for f in finals if f["text"] == "[ar] Ich nehme dieses Medikament"]
    else:
        assert finals[0]["text"] == f"[ar] {chunk}"
        assert idx(h, "translation.final", utt, seg=0) < idx(h, "utterance.end", utt)


# ---------------------------------------------------------------- a candidate's voice made ahead


async def test_candidate_voice_plays_when_the_commit_confirms_the_same_source(no_guard):
    """The first partial shows a complete clause with a dose (two partials needed): translated and voiced ahead.
    The second partial agrees: the commit replays that translation and plays that audio (made once)."""
    h = room([DOC_EN, PAT_AR_HP])
    spy = h.engines.translate = _SpyTranslator()  # type: ignore[assignment]
    h.engines.asr = ScriptASR("", "en", partials=["Take 500 mg twice a day, with",  # type: ignore[assignment]
                                                  "Take 500 mg twice a day, with food"],
                              final="Take 500 mg twice a day, with food.")
    voice = Voice(h)
    clause = "[ar] Take 500 mg twice a day,"
    utt = await say(h, DOC_EN.id, 1.0)
    await h.p.drain()
    assert h.p.spec.seg_hits == 1 and spy.texts.count("Take 500 mg twice a day,") == 1
    assert voice.spoken[0] == clause and voice.made.count(clause) == 1  # from the cache
    assert segs(h, "translation.final", utt)[0]["text"] == clause


async def test_candidate_voice_is_dropped_when_the_commit_differs(no_guard):
    """The second partial hears 50 mg, not 500: the candidate for "500 mg" is dropped (its audio too) and never
    plays; the committed "50 mg" clause is what the listener hears."""
    h = room([DOC_EN, PAT_AR_HP])
    h.engines.asr = ScriptASR("", "en", partials=["Take 500 mg twice a day, with",  # type: ignore[assignment]
                                                  "Take 50 mg twice a day, with food",
                                                  "Take 50 mg twice a day, with food."],
                              final="Take 50 mg twice a day, with food.")
    voice = Voice(h)
    utt = await say(h, DOC_EN.id, 1.5)
    await h.p.drain()
    stale = "[ar] Take 500 mg twice a day,"
    assert stale not in voice.spoken
    assert not [k for k in h.p.spec._voices if k[1] == stale.casefold()]  # forgotten, not left in the cache
    assert voice.spoken[0] == "[ar] Take 50 mg twice a day,"
    assert segs(h, "translation.final", utt)[0]["text"] == "[ar] Take 50 mg twice a day,"


# ---------------------------------------------------------------- catch-up only at a sentence start


async def test_catch_up_pace_changes_only_at_a_sentence_start():
    h = room([DOC_EN, PAT_AR_HP])
    h.engines.tts = None  # type: ignore[assignment]
    sim = h.p.simul
    rates: list[float | None] = []

    async def say_(lane, text, voice=None):
        rates.append(voice.rate if voice is not None else None)

    sim._say = say_  # type: ignore[method-assign]
    asked: list[int] = []

    def backlog(lane, i):  # 2 s behind everywhere except before units 0 and 3
        asked.append(i)
        return {1: 0.0, 4: 0.0}.get(i, 2.0)

    sim._backlog_s = backlog  # type: ignore[method-assign]
    lane = _Lane(utt="u-1", tgt="ar", src_lang="en", speaker=DOC_EN.id, role="a", turn=None,  # type: ignore[arg-type]
                 context="", voice_args=VoiceArgs(rate=1.0))
    texts = ["Alpha beta gamma,", "delta epsilon zeta.", "eta theta iota.", "kappa lambda,", "mu nu xi."]
    for i, t in enumerate(texts):
        seg = _Seg(index=i, src=t, text=t)
        seg.ready.set()
        lane.segs.append(seg)
    for i, seg in enumerate(lane.segs):
        await sim._say_group(lane, [seg], i + 1)
    # 2 s behind in the middle of a sentence: no change; at the next sentence start: 1.1 for that sentence
    assert rates == [1.0, 1.0, simul.CATCHUP_RATE, 1.0, 1.0]
    assert asked == [1, 3, 4]  # the backlog is looked at only where a sentence starts

    held = _Lane(utt="u-2", tgt="ar", src_lang="en", speaker=DOC_EN.id, role="a", turn=None,  # type: ignore[arg-type]
                 context="", voice_args=VoiceArgs(rate=1.0), held=True)
    seg = _Seg(index=0, src="omega psi.", text="omega psi.")
    seg.ready.set()
    held.segs.append(seg)
    sim._backlog_s = lambda lane, i: 5.0  # type: ignore[method-assign]
    rates.clear()
    await sim._say_group(held, [seg], 1)
    assert rates == [1.0]  # held lanes play after the speaker: never faster
