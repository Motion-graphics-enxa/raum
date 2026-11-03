"""Pass-through voice (room.py ``_pass_through``): the speaker switched language and said a whole utterance in the
listener's language. Nothing to translate, so ``_targets`` leaves that listener out; it is voiced as said."""

from __future__ import annotations

import numpy as np
import pytest
from pipeline_helpers import DOC, Harness, not_implemented

from raum.events import ParticipantInfo
from raum.pipeline.base import AsrResult
from raum.verify import guard

RU = ParticipantInfo(id="b-ru01", role="b", lang="ru")
EN = ParticipantInfo(id="a-en01", role="a", lang="en")
ENGLISH = "But I think it is not working, the tablets do not help me at all."


@pytest.fixture
def no_guard(monkeypatch):
    monkeypatch.setattr(guard, "check", not_implemented)
    monkeypatch.setattr(guard, "extract", not_implemented)


class _Voice:
    name = "voice"
    sample_rate = 16000
    simulated = False

    def __init__(self) -> None:
        self.said: list[tuple[str, str]] = []

    async def synthesize(self, text, lang):
        self.said.append((lang, text))
        yield np.zeros(320, dtype=np.int16)


def speaks(h: Harness, text: str, lang: str) -> None:
    """The ASR hears ``text`` in ``lang`` (partials and the final)."""

    async def transcribe(audio, *, lang_hint, partial, turn):
        return AsrResult(text=text, lang=lang, conf=0.95, ms=5.0)

    h.engines.asr.transcribe = transcribe  # type: ignore[method-assign]


def mt_spy(h: Harness) -> list[str]:
    seen: list[str] = []
    real = h.engines.translate.stream

    def stream(req):
        seen.append(req.tgt_lang)
        return real(req)

    h.engines.translate.stream = stream  # type: ignore[method-assign]
    return seen


def voiced(h: Harness) -> tuple[_Voice, list[str]]:
    v = _Voice()
    got: list[str] = []

    async def sink(lang, pcm, rate):
        got.append(lang)

    h.engines.tts = v  # type: ignore[assignment]
    h.p.tts_sink = sink
    return v, got


async def test_a_whole_utterance_in_the_listeners_language_is_voiced_as_said(no_guard):
    h = Harness(participants=[RU, EN])
    speaks(h, ENGLISH, "en")
    mt = mt_spy(h)
    v, got = voiced(h)
    utt = await h.say(RU.id, seconds=1.2)
    await h.p.drain()

    tr = h.one("translation.final", utt)
    assert tr["target_lang"] == "en" and tr["text"] == ENGLISH and tr["passthrough"] is True
    assert "".join(d.data["delta"] for d in h.of("translation.delta", utt)) == ENGLISH
    assert "en" not in mt  # no MT for it
    assert got and set(got) == {"en"}  # the listener hears it
    assert " ".join(t for _, t in v.said) == ENGLISH  # the whole sentence, in clauses
    res = h.one("verify.result", utt)
    assert res["badge"] == "green" and res["similarity"] == 1.0 and any("voiced as said" in r for r in res["reasons"])
    assert h.one("pipeline.timing", utt)["target_lang"] == "en"


async def test_a_speaker_without_a_device_language_switches_from_their_last_turn(no_guard):
    anon = ParticipantInfo(id="b-anon", role="b", lang="auto")
    h = Harness(participants=[anon, EN])
    speaks(h, "Мне больно уже три дня.", "ru")
    first = await h.say(anon.id)
    await h.p.drain()
    assert h.one("translation.final", first).get("passthrough") is None  # ru -> en, translated
    speaks(h, ENGLISH, "en")
    utt = await h.say(anon.id)
    await h.p.drain()
    assert h.one("translation.final", utt)["passthrough"] is True


async def test_same_language_room_and_other_listeners_unchanged(no_guard):
    # both speak English: nothing to voice, as before
    h = Harness(participants=[ParticipantInfo(id="b-en02", role="b", lang="en"), EN])
    speaks(h, ENGLISH, "en")
    utt = await h.say("b-en02")
    await h.p.drain()
    assert h.of("translation.final", utt) == []

    # a Russian speaker's English sentence: the German listener still gets MT, the English one the sentence
    h2 = Harness(participants=[RU, EN, DOC])
    speaks(h2, ENGLISH, "en")
    mt = mt_spy(h2)
    utt = await h2.say(RU.id)
    await h2.p.drain()
    finals = {e.data["target_lang"]: e.data for e in h2.of("translation.final", utt)}
    assert set(finals) == {"en", "de"} and "de" in mt and "en" not in mt
    assert finals["en"]["passthrough"] is True and finals["de"].get("passthrough") is None

    # the speaker's own language, as usual: translated, no pass-through
    h3 = Harness(participants=[RU, EN])
    speaks(h3, "Мне больно уже три дня.", "ru")
    utt = await h3.say(RU.id)
    await h3.p.drain()
    assert h3.one("translation.final", utt)["target_lang"] == "en"
    assert h3.one("translation.final", utt).get("passthrough") is None
