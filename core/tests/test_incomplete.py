"""Deterministic unfinished-sentence rules (incomplete.py) and their place in the end of turn.

The P0 repro on the GX10 (recordings/pause-de.wav, not in the repo; ``say -v Anna``): "Sie dürfen das
Metformin ab morgen [800 ms pause] nicht mehr nehmen." Run 2 ended the turn at the pause with the early
transcript "Sie dürfen das Medformin abnorgen?" (asr_conf 0.77): "You may take the Metformin?" was spoken,
the opposite of the sentence.
"""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest
from pipeline_helpers import DOC, Harness

from raum.pipeline.base import AsrResult
from raum.pipeline.incomplete import unfinished
from raum.pipeline.turn import LK_METHOD, ST_METHOD, TurnDetector

UNFINISHED_DE = [
    ("Sie dürfen das Metformin ab morgen", "neg-scope"),
    ("Sie dürfen das Medformin abnorgen", "verb-final"),  # ASR garbage is not a closing infinitive
    ("Sie dürfen das Medformin abnorgen?", "verb-final"),
    ("Sie dürfen", "verb-final"),
    ("Sie dürfen das", "verb-final"),
    ("Nehmen Sie das Metformin ab morgen", "neg-scope"),
    ("Nehmen Sie das ab Montag", "neg-scope"),
    ("Wenn Sie Schmerzen haben, dürfen Sie das Ibuprofen", "verb-final"),
    ("Sie müssen das Medikament", "verb-final"),
    ("Sie sollten die Tabletten weiterhin", "neg-scope"),
    ("Ich möchte einen Termin.", "verb-final"),  # costs latency, never a cut
    ("Sie haben das", "aux-open"),  # [pause] "nicht genommen"
    ("Ich habe das nicht", "aux-open"),
    ("Das ist", "aux-open"),
    ("Haben Sie", "aux-open"),
    ("Ich habe das gestern", "aux-open"),
]
FINISHED_DE = [
    "Sie dürfen das Metformin ab morgen nicht mehr nehmen.",
    "Sie dürfen das Metformin nicht mehr einnehmen.",
    "Sie müssen zweimal täglich 500 mg Metformin nehmen.",
    "Ich habe das Medikament gestern genommen.",
    "Wir werden das Medikament absetzen.",
    "Sie sollten das Medikament weiter einnehmen.",
    "Das wurde gestern operiert.",
    "Sie dürfen das Pflaster nicht befeuchten.",
    "Das darf nicht passieren.",
    "Können Sie mir das erklären?",
    "Sie können nach Hause gehen.",
    "Ich weiß, dass Sie das nehmen dürfen.",
    "Wenn Sie wollen",
    "Ist das Fenster zu?",
    "Guten Tag, wie geht es Ihnen?",
    "Ich nehme das Metformin.",
    "Ja.",
    # haben / sein / werden with a predicate complement: finished (the model decides), ~0.4 s faster
    "Ich bin allergisch gegen Penicillin.",
    "Haben Sie Schmerzen?",
    "Das ist gut.",
    "Ich habe Kopfschmerzen.",
    "Wird das besser?",
    "Ich bin 45 Jahre alt.",
]


@pytest.mark.parametrize(("text", "why"), UNFINISHED_DE)
def test_german_unfinished(text, why):
    assert unfinished(text, "de") == why


@pytest.mark.parametrize("text", FINISHED_DE)
def test_german_no_opinion(text):
    assert unfinished(text, "de") is None


def test_english_trailing_auxiliary():
    assert unfinished("You may", "en") == "aux-final"
    assert unfinished("You must not", "en") == "aux-final"
    assert unfinished("I want to", "en") == "aux-final"
    assert unfinished("You may not take it.", "en") is None
    assert unfinished("Where is it?", "en") is None


def test_other_languages_have_no_rule():
    assert unfinished("Sie dürfen das Metformin ab morgen", "ru") is None
    assert unfinished("Sie dürfen das", None) is None
    assert unfinished("", "de") is None


# ---------------------------------------------------------------- TurnDetector


class Executor:
    """The text model says "finished" (it did on the box for the cut sentence)."""

    def __init__(self, p_text: float = 0.9) -> None:
        self.p_text = p_text
        self.texts: list[str] = []

    async def do_inference(self, method: str, data: bytes) -> bytes | None:
        if method == LK_METHOD:
            self.texts.append(json.loads(data)["chat_ctx"][0]["content"])
            return json.dumps({"eou_probability": self.p_text}).encode()
        assert method == ST_METHOD
        return json.dumps({"p": 0.9}).encode()


OPTS = {"fast": 0.1, "unsure_s": 1.0, "max_s": 1.6, "audio_min": 0.5}


async def test_rule_beats_a_confident_text_model():
    ex = Executor(0.9)
    d = TurnDetector(ex, OPTS)
    v = await d.verdict("Sie dürfen das Metformin ab morgen", "de", conf=0.95)
    assert (v.wait_s, v.why) == (1.6, "neg-scope")
    v = await d.verdict("Sie dürfen das Medformin abnorgen?", "de", conf=0.77)
    assert (v.wait_s, v.why) == (1.6, "verb-final")
    assert ex.texts == []  # decided before any model call (~0 ms)
    assert (await d.verdict("Sie dürfen das Metformin ab morgen nicht mehr nehmen.", "de")).wait_s == 0.0


async def test_aux_wait_is_configurable_and_rules_can_be_switched_off():
    d = TurnDetector(Executor(0.9), {**OPTS, "aux_s": 1.0})
    assert (await d.verdict("Sie haben das", "de")).wait_s == 1.0
    assert (await d.verdict("Sie dürfen das", "de")).wait_s == 1.6  # modals stay at max_s
    d = TurnDetector(Executor(0.9), {**OPTS, "rules": False})
    assert (await d.verdict("Sie dürfen das", "de")).wait_s == 0.0


async def test_low_confidence_question_mark_is_not_completeness():
    ex = Executor(0.9)
    d = TurnDetector(ex, OPTS)
    v = await d.verdict("Nehmen Sie das Metformin?", "de", conf=0.77)
    assert (v.wait_s, v.why) == (1.0, "low-conf-question")
    assert ex.texts == ["Nehmen Sie das Metformin"]  # the text model does not see whisper's "?"
    assert (await d.verdict("Nehmen Sie das Metformin?", "de", conf=0.9)).wait_s == 0.0
    assert (await d.verdict("Nehmen Sie das Metformin?", "de")).wait_s == 0.0  # no score: unchanged
    assert (await d.verdict("Nehmen Sie das Metformin.", "de", conf=0.5)).wait_s == 0.0


# ---------------------------------------------------------------- the pause recording, end to end


class PauseASR:
    """whisper on the box: the cut audio gives run 2's transcript, the whole audio the real sentence."""

    is_mock = False

    def __init__(self, cut_samples: int) -> None:
        self.cut = cut_samples

    async def transcribe(self, audio, *, lang_hint, partial, turn):
        await asyncio.sleep(0.005)
        if len(audio) <= self.cut:
            return AsrResult(text="Sie dürfen das Medformin abnorgen?", lang="de", conf=0.77, ms=5.0)
        return AsrResult(text="Sie dürfen das Metformin ab morgen nicht mehr nehmen.", lang="de", conf=0.93, ms=5.0)


async def _speak(h: Harness, seconds: float) -> None:
    await h.p.speech_start(DOC.id)
    for _ in range(int(seconds * 50)):
        await h.p.speech_audio(DOC.id, np.full(320, 0.01, dtype=np.float32))


async def test_pause_recording_waits_through_the_pause():
    """2.2 s of speech, VAD end at 250 ms of silence, speech again 800 ms after the stop."""
    h = Harness()
    h.p.engines.asr = PauseASR(cut_samples=int(2.2 * 16000))
    h.p.eot = TurnDetector(Executor(0.9), OPTS)  # the text model would have said "finished"
    await _speak(h, 2.2)
    await h.p.speech_pause(DOC.id, None, 250.0)
    await asyncio.sleep(0.8 - 0.25)  # the rest of the 800 ms pause
    assert h.of("utterance.end") == [] and h.of("translation.final") == []
    assert await h.p.speech_start(DOC.id) == "u-0001"  # the same utterance goes on
    for _ in range(50):
        await h.p.speech_audio(DOC.id, np.full(320, 0.01, dtype=np.float32))
    await h.p.speech_pause(DOC.id, None, 250.0)
    await h.p.drain()
    assert [e.utt for e in h.of("utterance.start")] == ["u-0001"]
    finals = h.of("transcript.final")
    assert [f.data["text"] for f in finals] == ["Sie dürfen das Metformin ab morgen nicht mehr nehmen."]
    timing = h.of("pipeline.timing")[0].data
    assert timing["eot"]["why"] == "complete"


# ---------------------------------------------------------------- ru / en / de: more rules (gate the fast path)

MORE = [
    ("ru", "Вам нельзя", "verb-final"),
    ("ru", "Вы должны", "verb-final"),
    ("ru", "Вам можно с завтрашнего дня", "neg-scope"),
    ("ru", "Вам можно не", "neg-open"),
    ("ru", "Я принимаю метформин и", "conj-open"),
    ("ru", "Я думаю, что", "conj-open"),
    ("ru", "У меня аллергия на", "conj-open"),
    ("ru", "Принимайте по 500", "number-open"),
    ("en", "Take 500 of the", "conj-open"),
    ("en", "I stopped because", "conj-open"),
    ("en", "I'm allergic to", "aux-final"),
    ("en", "I take it with", "conj-open"),
    ("en", "Take 500", "number-open"),
    ("de", "Ich bin allergisch gegen", "conj-open"),
    ("de", "Ich nehme Metformin und", "conj-open"),
    ("de", "Nehmen Sie 500", "number-open"),
]
NO_RULE = [
    ("ru", "Мне можно?"),
    ("ru", "Вам нельзя принимать метформин с завтрашнего дня."),
    ("ru", "Принимайте по 500 мг."),
    ("ru", "Простите, что?"),
    ("ru", "У меня аллергия на пенициллин."),
    ("ru", "Мне нужно лекарство."),
    ("en", "Where are you from?"),
    ("en", "Come in."),
    ("en", "Take 500 mg twice a day."),
    ("en", "I'm allergic to penicillin."),
    ("de", "Rufen Sie mich an."),
    ("de", "Für wen?"),
    ("de", "Nehmen Sie 500 mg."),
    ("de", "Ich habe 2 Kinder."),
]


@pytest.mark.parametrize(("lang", "text", "why"), MORE)
def test_more_rules(lang, text, why):
    assert unfinished(text, lang) == why


@pytest.mark.parametrize(("lang", "text"), NO_RULE)
def test_more_rules_no_opinion(lang, text):
    assert unfinished(text, lang) is None


# ---------------------------------------------------------------- fast-complete


class AudioExecutor(Executor):
    def __init__(self, p_text: float, p_audio: float = 0.9) -> None:
        super().__init__(p_text)
        self.p_audio = p_audio

    async def do_inference(self, method: str, data: bytes) -> bytes | None:
        if method == ST_METHOD:
            return json.dumps({"p": self.p_audio}).encode()
        return await super().do_inference(method, data)


async def fast(text, lang="de", conf=0.95, p_text=0.05, p_audio=0.9, opts=None):
    d = TurnDetector(AudioExecutor(p_text, p_audio), {**OPTS, **(opts or {})})
    audio = asyncio.ensure_future(d.audio_p(np.zeros(16000, dtype=np.float32)))
    return await d.verdict(text, lang, audio, conf=conf)


async def test_fast_complete_ends_at_045():
    for lang, text in (("de", "Ich nehme das Metformin."), ("en", "I take the metformin."),
                       ("ru", "Я принимаю метформин."), ("de", "Haben Sie Schmerzen?")):
        v = await fast(text, lang)
        assert (v.wait_s, v.why) == (0.45, "fast-complete"), text
    v = await fast("Ich nehme das Metformin.", p_text=0.5, p_audio=0.2)  # the audio veto is lifted too
    assert (v.wait_s, v.why) == (0.45, "fast-complete")
    v = await fast("Ich nehme das Metformin.", p_text=0.5)  # the "complete" tier stays at 0
    assert (v.wait_s, v.why) == (0.0, "complete")


@pytest.mark.parametrize(("text", "lang", "conf", "p_text"), [
    ("Ich nehme das Metformin.", "de", 0.89, 0.05),  # asr_conf below 0.90
    ("Ich nehme das Metformin.", "de", None, 0.05),  # no ASR scores
    ("Ich nehme Metformin", "de", 0.95, 0.05),  # no end punctuation
    ("Ich nehme Metformin,", "de", 0.95, 0.05),
    ("Ich nehme Metformin...", "de", 0.95, 0.05),
    ("Metformin, ja.", "de", 0.95, 0.05),  # < 3 words
    ("Nehmen Sie 500.", "de", 0.95, 0.05),  # SOFT rule: trailing number
    ("Принимайте по 500.", "ru", 0.95, 0.05),
    ("Ich nehme das Metformin.", "de", 0.95, 0.01),  # text model below the fast floor 0.02
    ("Ben metformin alıyorum.", "tr", 0.95, 0.05),  # not a fast language
])
async def test_fast_complete_negatives(text, lang, conf, p_text):
    v = await fast(text, lang, conf=conf, p_text=p_text)
    assert v.why != "fast-complete" and v.wait_s >= 1.0, (text, v)


async def test_fast_path_never_overrides_a_rule():
    for text, lang in (("Sie dürfen das Metformin ab morgen.", "de"), ("Вам нельзя.", "ru"),
                       ("Ich bin allergisch gegen.", "de"), ("You may.", "en")):
        v = await fast(text, lang, p_text=0.9)
        assert v.wait_s == 1.6 and v.why != "fast-complete", text


async def test_fast_options():
    v = await fast("Ich nehme das Metformin.", opts={"fast_s": 0.6})
    assert v.wait_s == 0.6
    v = await fast("Ich nehme das Metformin.", opts={"fast_langs": ["en"]})
    assert v.why == "unsure"


def test_commit_floor_is_lower_for_svo_languages():
    d = TurnDetector(Executor(), OPTS)
    assert d.commit_floor("en") == d.commit_floor("ru") == 0.02
    assert d.commit_floor("de") == 0.1  # verb-final: commit only what the model calls finished
    assert TurnDetector(Executor(), {**OPTS, "rules": False}).commit_floor("en") == 0.1


# ---------------------------------------------------------------- whisper's sentence end at a pause

JOINS = [
    ("en", "You should not. Take this medicine with alcohol.", "You should not take this medicine with alcohol."),
    ("en", "I'm allergic to... penicillin.", "I'm allergic to penicillin."),
    ("de", "Sie dürfen das Metformin ab morgen. Nicht mehr nehmen.",
     "Sie dürfen das Metformin ab morgen nicht mehr nehmen."),
    ("de", "Sie dürfen das Medformin abnorgen? Nicht mehr nehmen.", "Sie dürfen das Medformin abnorgen nicht mehr nehmen."),
    ("de", "Ich bin allergisch gegen. Penicillin.", "Ich bin allergisch gegen Penicillin."),  # the noun keeps its case
    ("de", "Ich habe das. Nicht genommen.", "Ich habe das nicht genommen."),
    ("ru", "Вам нельзя. Принимать метформин с алкоголем.", "Вам нельзя принимать метформин с алкоголем."),
]
KEEPS = [
    ("en", "Yes, I can. Thank you."),
    ("en", "Where are you from? I'm from Berlin."),
    ("en", "You may. I think so."),
    ("de", "Guten Tag. Wie geht es Ihnen?"),
    ("de", "Das ist gut. Danke."),
    ("de", "Sie dürfen. Danke."),
    ("de", "Nehmen Sie 500. Milligramm."),  # a SOFT reason never joins
    ("ru", "Мне можно? Спасибо."),
    ("tr", "Yapmamalısınız. Bu ilacı alkolle almayın."),  # no rules for the language
]


@pytest.mark.parametrize(("lang", "text", "joined"), JOINS)
def test_join_fragments(lang, text, joined):
    from raum.pipeline.incomplete import join_fragments

    assert join_fragments(text, lang) == joined


@pytest.mark.parametrize(("lang", "text"), KEEPS)
def test_join_fragments_keeps_real_sentences(lang, text):
    from raum.pipeline.incomplete import join_fragments

    assert join_fragments(text, lang) == text


class EnPauseASR:
    """The box's en-pause clip: whisper puts a full stop where the speaker paused."""

    is_mock = False

    async def transcribe(self, audio, *, lang_hint, partial, turn):
        await asyncio.sleep(0.005)
        return AsrResult(text="You should not. Take this medicine with alcohol.", lang="en", conf=0.93, ms=5.0)


async def test_whispers_full_stop_at_a_pause_is_not_a_sentence_end_for_the_translator():
    from pipeline_helpers import PAT

    h = Harness()
    h.p.engines.asr = EnPauseASR()
    await h.say(PAT.id, seconds=1.0)
    await h.p.drain()
    assert h.one("transcript.final")["text"] == "You should not take this medicine with alcohol."


def test_a_latin_phrase_the_final_dropped_comes_back_from_the_partials():
    # box, mix2-ru.wav: the partials had it; once the Russian went on, whisper dropped it from the final
    from raum.pipeline.codeswitch import restore

    partials = ["Доктор, я принимаю этот препарат каждый день.",
                "Доктор, я принимаю этот препарат каждый день. But I think",
                "Доктор, я принимаю этот препарат каждый день. But I think it is not working.",
                "Доктор, я принимаю этот препарат каждый день, поэтому мне нужно"]
    final = "Доктор, я принимаю этот препарат каждый день, поэтому мне нужна новая дозировка."
    assert restore(final, partials, "ru") == ("Доктор, я принимаю этот препарат каждый день, but I think it is not "
                                              "working, поэтому мне нужна новая дозировка.")
    kept = "Доктор, я принимаю этот препарат каждый день, but I think it is not working, поэтому мне нужна новая доза."
    assert restore(kept, partials, "ru") == kept  # the final has it: nothing doubled
    assert restore(final, partials, "de") == final  # a Latin-script language: no way to tell by the script
    assert restore("Совсем другой текст.", partials, "ru") == "Совсем другой текст."  # no anchor: no guess
    assert restore("Принимайте Metformin 500 mg.", ["Принимайте Metformin 500 mg утром"], "ru") == \
        "Принимайте Metformin 500 mg."  # one Latin word (a drug name, a unit): not a phrase
