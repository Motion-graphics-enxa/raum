"""Misheard glossary terms (the GX10 "Medformin" red): the room glossary as whisper's prompt (sz / kz), the
fuzzy medication match in the fact guard, and the glossary spelling in the final transcript."""

from __future__ import annotations

import httpx
import numpy as np
from pipeline_helpers import DOC, PAT, Harness
from test_pipeline_real_engines import client, form_fields

from raum.events import Control, ParticipantInfo
from raum.glossary import ASR_PROMPT_MAX_CHARS, ASR_PROMPT_MAX_TERMS, Glossary, Term
from raum.modes import get_mode
from raum.pipeline.asr import WhisperCppASR
from raum.pipeline.base import AsrResult, TurnContext
from raum.verify.guard import check, correct_medications, extract

MED = Glossary.load("glossaries/medical-de.yaml")
SZ = get_mode("sprechzimmer")
HEARD = "Sie dürfen das Medformin ab morgen nicht mehr nehmen."  # whisper.cpp on the GX10, Anna's voice
SAID = "Sie dürfen das Metformin ab morgen nicht mehr nehmen."
EN = "You may no longer take Metformin from tomorrow on."


def med(*names: str) -> list[Term]:
    return [Term(de=n, kind="medication", keep=True) for n in names]


def sz_check(src: str, sl: str, tgt: str, tl: str, glossary: Glossary = MED):
    return check(src, sl, tgt, tl, kinds=SZ.guard.kinds, confirm_on=SZ.guard.confirm_on, glossary=glossary)


def med_rows(r) -> list[tuple[str | None, str | None, bool]]:
    return [(f.src, f.tgt, f.match) for f in r.facts if f.kind == "medication"]


# ---------------------------------------------------------------- Fix 2: the glossary as whisper's prompt


async def test_whisper_sends_the_turn_prompt_after_the_configured_one():
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(form_fields(request))
        return httpx.Response(200, json={"text": "Metformin.", "language": "de"})

    audio = np.zeros(1600, dtype=np.float32)
    turn = TurnContext(room="sz-1", mode="sprechzimmer", speaker="a-1", role="a", utt="u-0001",
                       asr_prompt="Metformin, Insulin.")
    asr = WhisperCppASR("http://w:1", client=client(handler), options={"prompt": "Arztgespräch."})
    await asr.transcribe(audio, lang_hint="de", partial=True, turn=turn)
    await asr.transcribe(audio, lang_hint="de", partial=False, turn=turn)
    assert [s["prompt"] for s in seen] == ["Arztgespräch. Metformin, Insulin."] * 2  # partials and finals
    plain = TurnContext(room="sz-1", mode="sprechzimmer", speaker="a-1", role="a", utt="u-0002")
    await asr.transcribe(audio, lang_hint="de", partial=False, turn=plain)
    assert seen[-1]["prompt"] == "Arztgespräch."  # no glossary hints: the configured prompt alone
    await WhisperCppASR("http://w:1", client=client(handler)).transcribe(audio, lang_hint="de", partial=False,
                                                                        turn=plain)
    assert "prompt" not in seen[-1]
    await WhisperCppASR("http://w:1", client=client(handler)).transcribe(audio, lang_hint="de", partial=False,
                                                                        turn=turn)
    assert seen[-1]["prompt"] == "Metformin, Insulin."


def order(prompt: str) -> list[str]:
    """Whisper's prompt lists the most important term LAST (nearest the audio): most important first here."""
    return prompt.rstrip(".").split(", ")[::-1] if prompt else []


CURATED = ["Metformin", "Penicillin", "Ibuprofen", "Amoxicillin", "Insulin"]


def test_prompt_lists_medications_first_in_the_speakers_language():
    de = order(MED.asr_prompt("de"))
    assert de[:7] == [*CURATED, "Metamizol", "Pantoprazol"]  # the room's drugs, then the long list by frequency
    assert len(de) == ASR_PROMPT_MAX_TERMS and "Blutdruck" not in de and "Prednisolon" in de
    assert MED.asr_prompt("de").endswith("Penicillin, Metformin.")
    en = order(MED.asr_prompt("en"))
    assert en[:6] == ["Metformin", "penicillin", "ibuprofen", "amoxicillin", "insulin", "metamizole"]
    assert "on an empty stomach" not in en  # a phrase of everyday words: no spelling to hint
    assert order(MED.asr_prompt("ru"))[:3] == ["Метформин", "пенициллин", "ибупрофен"]
    ar = order(MED.asr_prompt("ar"))
    assert ar[0] == "ميتفورمين" and "Ramipril" not in ar  # the long list has no Arabic spellings: no Latin hints
    # language unknown: the language-neutral German forms (drugs, names), never German everyday words
    for unknown in (None, "und", "auto", ""):
        u = order(MED.asr_prompt(unknown))
        assert u[:5] == CURATED and "Blutdruck" not in u and "Rezept" not in u
    legal = Glossary.load(get_mode("kanzlei").glossary)
    assert order(legal.asr_prompt("de"))[:3] == ["Klagefrist", "Kündigungsschutzklage", "Arbeitsvertrag"]
    assert legal.asr_prompt(None) == "" and Glossary().asr_prompt("de") == ""
    g = Glossary(terms=[Term(de="Rezept", kind="term"),  # file order: a term first ...
                        Term(de="Herr Yılmaz", kind="name"), *med("Ramipril")])
    assert order(g.asr_prompt("de")) == ["Ramipril", "Herr Yılmaz", "Rezept"]  # ... yet medications, then names lead


def test_prompt_is_capped():
    g = Glossary(terms=[Term(de=f"Wirkstoff{i:03d}", kind="medication", keep=True) for i in range(100)])
    p = g.asr_prompt("de")
    assert p.endswith(".") and len(p) <= ASR_PROMPT_MAX_CHARS
    assert order(p)[:2] == ["Wirkstoff000", "Wirkstoff001"] and len(p.split(", ")) < 100
    short = Glossary(terms=[Term(de=f"M{i:02d}", kind="medication", keep=True) for i in range(80)])
    assert len(short.asr_prompt("de").split(", ")) == ASR_PROMPT_MAX_TERMS
    assert g.asr_prompt("de", max_terms=3) == "Wirkstoff002, Wirkstoff001, Wirkstoff000."
    wide = Glossary(terms=[Term(de=f"W{i:03d}", kind="medication", keep=True, forms={"ru": [f"Препарат{i:03d}"]})
                           for i in range(100)])
    assert len(wide.asr_prompt("ru")) <= ASR_PROMPT_MAX_CHARS * 11 // 20  # Cyrillic: about twice the tokens


def test_prompt_is_cached_and_rebuilt_when_the_glossary_changes():
    g = Glossary.load("glossaries/medical-de.yaml")
    first = g.asr_prompt("de")
    assert g.asr_prompt("de") is first  # cached
    g.merge([Term(de="Sacubitril", kind="medication", keep=True)])  # an upload: before the long list
    assert order(g.asr_prompt("de"))[:6] == [*CURATED, "Sacubitril"]
    before = g.asr_prompt("en")
    g.add_correction("Metformin", "metformin hydrochloride", "en")  # a live correction rebuilds it
    assert order(g.asr_prompt("en"))[0] == "metformin hydrochloride" and g.asr_prompt("en") != before


class _RecordingASR:
    """Records the prompt of every call; always hears ``text``."""

    is_mock = True
    name = "recording"

    def __init__(self, text: str = SAID) -> None:
        self.text = text
        self.calls: list[tuple[bool, str | None]] = []

    async def transcribe(self, audio, *, lang_hint, partial, turn):
        self.calls.append((partial, turn.asr_prompt))
        # no engine scores: a stable partial is never reused as the final, so the final pass always runs
        return AsrResult(text=self.text, lang=lang_hint or "de", conf=0.95, ms=1.0, has_conf=False)


async def test_sprechzimmer_and_kanzlei_send_the_glossary_with_every_asr_call():
    assert SZ.asr.glossary_prompt and get_mode("kanzlei").asr.glossary_prompt
    assert not get_mode("verhandlungsraum").asr.glossary_prompt and not get_mode("gespraech").asr.glossary_prompt
    h = Harness()  # sprechzimmer: doctor de, patient ar
    asr = h.engines.asr = _RecordingASR()  # type: ignore[assignment]
    await h.say(DOC.id, seconds=1.2)
    await h.p.drain()
    assert {p for p, _ in asr.calls} == {True, False}  # partials and the final
    assert {pr for _, pr in asr.calls} == {MED.asr_prompt("de")}
    asr.calls.clear()
    await h.say(PAT.id, seconds=0.8)
    await h.p.drain()
    assert {pr for _, pr in asr.calls} == {MED.asr_prompt("ar")} and "ميتفورمين" in MED.asr_prompt("ar")
    asr.calls.clear()
    stranger = ParticipantInfo(id="b-x1", role="b", lang="und")  # language not known yet: language-neutral forms
    h.p.set_participants([DOC, stranger])
    await h.say(stranger.id, seconds=0.5)
    await h.p.drain()
    assert {pr for _, pr in asr.calls} == {MED.asr_prompt(None)}
    await h.p.aclose()


async def test_gespraech_sends_no_prompt():
    a = ParticipantInfo(id="a-1", role="a", lang="de")
    b = ParticipantInfo(id="b-1", role="b", lang="en")
    h = Harness(mode="gespraech", room="gs-1000", participants=[a, b])
    asr = h.engines.asr = _RecordingASR("Bis morgen.")  # type: ignore[assignment]
    await h.say(a.id, seconds=0.8)
    await h.p.drain()
    assert asr.calls and {pr for _, pr in asr.calls} == {None}
    await h.p.aclose()


async def test_a_term_merged_at_runtime_reaches_the_next_utterance():
    h = Harness()
    asr = h.engines.asr = _RecordingASR()  # type: ignore[assignment]
    await h.p.handle_control(Control(action="glossary", by="a",
                                     value={"terms": [{"de": "Sacubitril", "kind": "medication", "keep": True}]}))
    await h.say(DOC.id, seconds=0.6)
    await h.p.drain()
    assert asr.calls and all("Sacubitril" in (pr or "") for _, pr in asr.calls)
    await h.p.handle_control(Control(action="burn"))  # the session glossary is forgotten
    asr.calls.clear()
    await h.say(DOC.id, seconds=0.6)
    await h.p.drain()
    assert asr.calls and not any("Sacubitril" in (pr or "") for _, pr in asr.calls)
    await h.p.aclose()


# ---------------------------------------------------------------- Fix 3: fuzzy medications


def test_misheard_metformin_is_not_a_red():
    r = sz_check(HEARD, "de", EN, "en")
    assert med_rows(r) == [("Medformin", "Metformin", True)]
    assert not r.red, [(f.kind, f.src, f.tgt) for f in r.facts if not f.match]
    assert [(f.kind, f.value) for f in extract(HEARD, "de", kinds=["medication"], glossary=MED)] == [
        ("medication", "Metformin")]
    # either side: the translator copied the misheard name
    assert not sz_check("Sie dürfen das Metformin ab morgen nicht mehr nehmen.", "de",
                        "You may no longer take Medformin from tomorrow on.", "en").red
    # a misheard allergen is the glossary's allergen
    r = sz_check("Ich bin allergisch gegen Penicilin.", "de", "I am allergic to penicillin.", "en")
    assert not r.red and ("allergy", "Penicilin", "penicillin") in [(f.kind, f.src, f.tgt) for f in r.facts]


def test_the_transcript_gets_the_glossary_spelling():
    assert correct_medications(HEARD, "de", MED) == SAID
    assert correct_medications(SAID, "de", MED) == SAID  # nothing to correct: unchanged
    assert correct_medications("Medformin-Tabletten und Ibuprofen.", "de", MED) == "Metformin-Tabletten und Ibuprofen."
    assert correct_medications("Penicilin ist gut.", "en", MED) == "Penicillin ist gut."  # capital kept
    assert correct_medications("я принимаю медформин", "ru", MED) == "я принимаю Метформин"
    assert correct_medications(HEARD, "de", None) == HEARD and correct_medications(HEARD, "de", Glossary()) == HEARD
    assert correct_medications("", "de", MED) == ""


def test_a_tie_between_two_drugs_is_no_match():
    g = Glossary(terms=med("Novolin", "Novolog"))  # a real look-alike pair: "Novolig" is one edit from both
    r = check("Spritzen Sie Novolig.", "de", "Inject Novolin.", "en", glossary=g)
    assert r.red and med_rows(r) == [(None, "Novolin", False)]
    assert correct_medications("Spritzen Sie Novolig.", "de", g) == "Spritzen Sie Novolig."
    assert not check("Spritzen Sie Novolim.", "de", "Inject Novolin.", "en", glossary=g).red  # one is nearest


def test_a_word_that_is_a_glossary_term_itself_is_never_mapped():
    g = Glossary(terms=[*med("Insulin"), Term(de="Inulin", kind="term")])
    r = check("Nehmen Sie Inulin.", "de", "Take insulin.", "en", glossary=g)
    assert r.red and med_rows(r) == [(None, "insulin", False)]
    assert correct_medications("Nehmen Sie Inulin.", "de", g) == "Nehmen Sie Inulin."
    # a form in any language counts: "inulin" is the English form of a German term here
    g = Glossary(terms=[*med("Insulin"), Term(de="Ballaststoff", kind="term", forms={"en": ["inulin"]})])
    assert check("Nehmen Sie Inulin.", "de", "Take insulin.", "en", glossary=g).red
    # without that term the same word would be the drug
    assert not check("Nehmen Sie Inulin.", "de", "Take insulin.", "en", glossary=Glossary(terms=med("Insulin"))).red


def test_short_words_and_short_drug_names_need_a_closer_match():
    g = Glossary(terms=med("Lyrica"))
    assert check("Nehmen Sie Lyric.", "de", "Take Lyrica.", "en", glossary=g).red  # 5 letters: never fuzzy
    assert not check("Nehmen Sie Lyrika.", "de", "Take Lyrica.", "en", glossary=g).red  # 6 letters, one edit
    assert correct_medications("Nehmen Sie Lyric.", "de", g) == "Nehmen Sie Lyric."
    # below 8 letters one edit only: the islets of Langerhans are not insulin (two edits)
    r = sz_check("Die Langerhans-Inseln sind entzündet.", "de", "The islets of Langerhans are inflamed.", "en")
    assert not r.red and med_rows(r) == []
    assert not sz_check("Spritzen Sie Insolin.", "de", "Inject insulin.", "en").red  # one edit: the drug


def test_a_different_drug_stays_red():
    r = sz_check("Nehmen Sie Medformin.", "de", "Take ibuprofen.", "en")
    assert r.red and ("Medformin", "ibuprofen", False) in med_rows(r)
    r = sz_check("Nehmen Sie Metformin.", "de", "Take Penicilin.", "en")  # misspelt, but another glossary drug
    assert r.red and ("Metformin", "Penicilin", False) in med_rows(r)
    assert sz_check("Nehmen Sie Metformin.", "de", "Take ibuprofen.", "en").red
    # two glossary drugs one edit apart: the exact one wins, so swapping them stays red
    g = Glossary(terms=med("Hydroxin", "Hydroxen"))
    assert check("Nehmen Sie Hydroxin.", "de", "Take Hydroxen.", "en", glossary=g).red


async def test_the_room_shows_translates_and_checks_the_glossary_spelling():
    h = Harness(participants=[DOC, ParticipantInfo(id="b-en1", role="b", lang="en")])  # sprechzimmer, de -> en
    h.engines.asr = _RecordingASR(HEARD)  # type: ignore[assignment]
    seen: list[str] = []

    class Translator:
        name = "fixed"

        async def stream(self, req):
            seen.append(req.text)
            yield EN

    h.engines.translate = Translator()  # type: ignore[assignment]
    utt = await h.say(DOC.id, seconds=1.0)
    await h.p.drain()
    assert h.one("transcript.final", utt)["text"] == SAID
    assert seen and set(seen) == {SAID}  # the translator never saw "Medformin" (speculation included)
    facts = h.one("verify.facts", utt)
    assert facts["red"] is False
    assert {"kind": "medication", "src": "Metformin", "tgt": "Metformin", "match": True} in facts["facts"]
    assert all("Medformin" not in str(e.data) for e in h.events if e.type != "transcript.partial")
    await h.p.aclose()


# ---------------------------------------------------------------- the long drug list (ops: "Pregnisolon")


def test_common_drugs_are_corrected_and_checked_without_false_reds():
    fixed, _ = correct_medications("Nehmen Sie morgens 20 mg Pregnisolon.", "de", MED), None
    assert fixed == "Nehmen Sie morgens 20 mg Prednisolon."
    # listed in en / ru: strict; the translation keeping it is green, dropping it is red
    assert not sz_check("Nehmen Sie Ramipril weiter.", "de", "Keep taking ramipril.", "en").red
    assert not sz_check("Nehmen Sie Ramipril weiter.", "de", "Продолжайте принимать рамиприл.", "ru").red
    assert sz_check("Nehmen Sie Ramipril weiter.", "de", "Keep taking your tablets.", "en").red
    # not listed in ar / tr: another spelling is no evidence of a missing drug (no false red) ...
    assert not sz_check("Nehmen Sie Ramipril weiter.", "de", "يرجى الاستمرار في تناول راميبريل.", "ar").red
    assert not sz_check("Nehmen Sie Levothyroxin weiter.", "de", "Levotiroksin almaya devam edin.", "tr").red
    # ... while the hand-kept drugs stay strict there, as before
    assert sz_check("Nehmen Sie Metformin weiter.", "de", "يرجى الاستمرار في تناول الدواء.", "ar").red
    # German alternate names ("aka")
    assert med_rows(sz_check("Ich nehme L-Thyroxin.", "de", "I take levothyroxine.", "en")) == \
        [("L-Thyroxin", "levothyroxine", True)]


def test_the_long_list_stays_out_of_the_translate_prompt():
    lines = MED.prompt_lines("de", "ru")
    assert "Метформин" in lines and "Рамиприл" not in lines and len(lines) < 600
