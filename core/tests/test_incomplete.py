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
