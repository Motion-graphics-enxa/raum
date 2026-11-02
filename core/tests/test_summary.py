"""Bilingual summary (C-11, §20.4): deterministic path for the three rooms, model path, fallbacks."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import raum.summary as summary_mod
from raum.events import make_event, parse_event
from raum.modes import get_mode
from raum.pipeline.base import Engines
from raum.summary import build_summary, fill_prompt, format_amount


def records(turns: list[tuple[str, str, str, dict[str, str]]], room: str = "sz-1") -> list[dict[str, Any]]:
    out = []
    for i, (role, lang, text, tr) in enumerate(turns, 1):
        utt = f"u-{i:04d}"
        out.append(make_event("transcript.final", room, {"speaker": f"{role}-ab12", "lang": lang, "text": text,
                                                         "asr_conf": 0.9}, utt=utt).model_dump(mode="json"))
        for tl, tt in tr.items():
            out.append(make_event("translation.final", room, {"target_lang": tl, "text": tt},
                                  utt=utt).model_dump(mode="json"))
    return out


@pytest.fixture(params=["guard", "fallback"])
def guard_mode(request, monkeypatch):
    """Run every deterministic test with the fact guard as merged, and with its regex fallback."""
    if request.param == "fallback":
        def stub(*a, **k):
            raise NotImplementedError

        monkeypatch.setattr(summary_mod.guard, "extract", stub)
    return request.param


def section(s, key):
    return next(x for x in s.sections if x.key == key)


def texts(s, key, lang):
    return [i.text[lang] for i in section(s, key).items]


SZ = [
    ("b", "en", "I take 500 mg metformin twice a day, and I'm allergic to penicillin.",
     {"de": "Ich nehme zweimal täglich 500 mg Metformin, und ich bin allergisch gegen Penicillin."}),
    ("a", "de", "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.",
     {"en": "Come back on Thursday, 12 November, at 9 o'clock."}),
    ("b", "en", "Can I still drink coffee?", {"de": "Darf ich noch Kaffee trinken?"}),
]


async def test_sprechzimmer_demo_sentence(guard_mode):
    s = await build_summary(get_mode("sprechzimmer"), records(SZ), None, "de", "en")
    assert s.template == "sprechzimmer" and s.lang_a == "de" and s.lang_b == "en"
    assert [x.key for x in s.sections] == ["medications", "allergies", "next_appointment", "warning_signs",
                                          "open_questions"]
    meds = section(s, "medications").items
    assert len(meds) == 1 and "Metformin" in meds[0].text["de"] and "metformin" in meds[0].text["en"]
    assert texts(s, "allergies", "de") == [meds[0].text["de"]]  # one sentence, two sections (demo step 2)
    assert "Thursday" in texts(s, "next_appointment", "en")[0]
    assert texts(s, "open_questions", "en") == ["Can I still drink coffee?"]
    assert texts(s, "warning_signs", "de") == []
    assert section(s, "next_appointment").title == {"de": "Nächster Termin", "en": "Next appointment"}
    assert s.file_note and "Metformin" in s.file_note["de"] and s.file_note["de"].startswith("Gesprächsnotiz")
    assert s.generated_ms is not None and s.generated_ms < 10_000
    parse_event(make_event("summary.ready", "sz-1", s).to_json())  # valid protocol payload


async def test_uncertain_and_confirmed_items(guard_mode):
    recs = records(SZ[:1])
    recs.append(make_event("verify.result", "sz-1", {"target_lang": "de", "similarity": 0.5, "badge": "red"},
                           utt="u-0001").model_dump(mode="json"))
    s = await build_summary(get_mode("sprechzimmer"), recs, None, "de", "en")
    assert section(s, "medications").items[0].uncertain is True
    assert "(unsicher)" in s.file_note["de"]


KZ = [
    ("b", "tr", "İşverenim beni 3 Kasım'da işten çıkardı.", {"de": "Mein Arbeitgeber hat mir am 3. November gekündigt."}),
    ("b", "tr", "Orada beş yıldır çalışıyorum.", {"de": "Ich arbeite seit fünf Jahren dort."}),
    ("a", "de", "Die Klagefrist endet am 24. November.", {"tr": "Dava açma süresi 24 Kasım'da sona eriyor."}),
    ("a", "de", "Bringen Sie bitte Ihren Arbeitsvertrag und die letzten drei Lohnabrechnungen mit.",
     {"tr": "Lütfen iş sözleşmenizi ve son üç maaş bordronuzu getirin."}),
    ("a", "de", "Als nächstes reiche ich die Kündigungsschutzklage beim Arbeitsgericht ein.",
     {"tr": "Bir sonraki adımda iş mahkemesine işe iade davası açacağım."}),
]


async def test_kanzlei(guard_mode):
    s = await build_summary(get_mode("kanzlei"), records(KZ, "kz-1"), None, "de", "tr")
    assert [x.key for x in s.sections] == ["parties", "facts", "dates", "deadlines", "documents_requested",
                                          "next_steps"]
    assert any("Klagefrist" in t for t in texts(s, "deadlines", "de"))
    assert any("Arbeitsvertrag" in t for t in texts(s, "documents_requested", "de"))
    assert any("sözleşme" in t for t in texts(s, "documents_requested", "tr"))
    assert any("Kündigungsschutzklage" in t for t in texts(s, "next_steps", "de"))
    assert any("Arbeitgeber" in t for t in texts(s, "parties", "de"))
    assert "Ich arbeite seit fünf Jahren dort." in texts(s, "facts", "de")
    assert not any("Klagefrist" in t for t in texts(s, "dates", "de"))  # deadlines are not plain dates
    assert section(s, "deadlines").title["tr"] == "Süreler"
    assert s.file_note["de"].startswith("Aktennotiz")


VR = [
    ("a", "de", "Unser Angebot liegt bei 1,2 Millionen Euro.", {"en": "Our offer is 1.2 million euros."}),
    ("b", "en", "Can you deliver by March?", {"de": "Können Sie bis März liefern?"}),
    ("a", "de", "Einverstanden, wir vereinbaren 1,05 Millionen bei Lieferung bis März.",
     {"en": "Agreed, we settle on 1.05 million with delivery by March."}),
]


async def test_verhandlungsraum_offers(guard_mode):
    recs = records(VR, "vr-1")
    recs.insert(2, make_event("offer.update", "vr-1", {"amount": 1200000, "currency": "EUR", "by": "a", "ts": 1},
                              utt="u-0001").model_dump(mode="json"))
    recs.append(make_event("offer.update", "vr-1", {"amount": 1050000, "currency": "EUR", "by": "a", "ts": 2},
                           utt="u-0003").model_dump(mode="json"))
    s = await build_summary(get_mode("verhandlungsraum"), recs, None, "de", "en")
    offers = section(s, "offers").items
    assert [o.text["de"] for o in offers] == ["1.200.000 EUR (Geschäftsführung)", "1.050.000 EUR (Geschäftsführung)"]
    assert offers[0].text["en"] == "1,200,000 EUR (CEO)"
    assert any("Einverstanden" in t for t in texts(s, "agreed", "de"))
    assert texts(s, "open", "en") == ["Can you deliver by March?"]


async def test_offers_from_amounts_without_tracker(guard_mode):
    s = await build_summary(get_mode("verhandlungsraum"), records(VR[:1], "vr-1"), None, "de", "en")
    assert any("1,2 Millionen" in t for t in texts(s, "offers", "de"))


async def test_empty_session():
    s = await build_summary(get_mode("sprechzimmer"), [], None, "de", "ar")
    assert all(not x.items for x in s.sections)
    assert "Keine Einträge" in s.file_note["de"]
    assert section(s, "medications").title == {"de": "Medikamente", "ar": "الأدوية"}


def test_helpers():
    t = 'Write in {lang_a}.\n{transcript}\n{"medications": [{"name": ""}]}'
    out = fill_prompt(t, {"lang_a": "German", "transcript": "a (de): Hallo"})
    assert out == 'Write in German.\na (de): Hallo\n{"medications": [{"name": ""}]}'
    assert format_amount(1200000, "de") == "1.200.000"
    assert format_amount(1200000, "en") == "1,200,000"
    assert format_amount(1050.5, "de") == "1.050,50"


# ---------------------------------------------------------------- model path


class FakeVerify:
    name = "ollama:gpt-oss:120b"

    def __init__(self, result: Any = None, delay: float = 0.0, fail: bool = False) -> None:
        self.result, self.delay, self.fail = result, delay, fail
        self.prompts: list[str] = []

    async def back_translate(self, text, text_lang, into_lang, turn=None):
        return text

    async def complete_json(self, prompt, *, purpose):
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("ollama down")
        return self.result

    async def complete_text(self, prompt, *, purpose):
        return ""


class FakeTranslate:
    name = "ollama:gemma4:26b"

    def __init__(self) -> None:
        self.requests = []

    async def stream(self, req):
        self.requests.append(req)
        for tok in f"[{req.tgt_lang}] {req.text}".split(" "):
            yield tok + " "


def engines(verify) -> Engines:
    return Engines(denoise=None, asr=None, translate=FakeTranslate(), verify=verify, embed=None, tts=None,
                   names={"verify": verify.name})


MODEL_JSON = {
    "medications": [{"name": "Metformin", "dose": "500 mg", "frequency": "zweimal täglich", "duration": "",
                     "uncertain": False}],
    "allergies": [{"text": "Penicillin", "uncertain": False}],
    "next_appointment": {"text": "Donnerstag, 12. November, 9 Uhr", "uncertain": False},
    "warning_signs": [],
    "open_questions": [{"text": "Kaffee erlaubt?", "uncertain": True}],
    "file_note_de": "Patient nimmt Metformin 500 mg 2x tgl.; Penicillinallergie.",
}


async def test_model_path():
    v = FakeVerify(MODEL_JSON)
    e = engines(v)
    s = await build_summary(get_mode("sprechzimmer"), records(SZ), e, "de", "en")
    assert s.model_extra.get("source") == "model"
    prompt = v.prompts[0]
    assert "German" in prompt and "I take 500 mg metformin" in prompt and '"medications"' in prompt
    meds = section(s, "medications").items
    assert meds[0].text == {"de": "Metformin 500 mg, zweimal täglich", "en": "[en] Metformin 500 mg, zweimal täglich"}
    assert texts(s, "next_appointment", "de") == ["Donnerstag, 12. November, 9 Uhr"]
    assert section(s, "open_questions").items[0].uncertain is True
    assert s.file_note == {"de": MODEL_JSON["file_note_de"]}
    assert {r.tgt_lang for r in e.translate.requests} == {"en"}


async def test_model_failure_falls_back():
    s = await build_summary(get_mode("sprechzimmer"), records(SZ), engines(FakeVerify(fail=True)), "de", "en")
    assert s.model_extra.get("source") == "records"
    assert section(s, "medications").items


async def test_model_timeout_falls_back(monkeypatch):
    monkeypatch.setattr(summary_mod, "MODEL_TIMEOUT_S", 0.05)
    s = await build_summary(get_mode("sprechzimmer"), records(SZ), engines(FakeVerify(MODEL_JSON, delay=5)), "de",
                            "en")
    assert s.model_extra.get("source") == "records" and s.generated_ms < 2000


async def test_mock_verify_uses_records():
    v = FakeVerify(MODEL_JSON)
    v.name = "mock:mock-gpt-oss"
    s = await build_summary(get_mode("sprechzimmer"), records(SZ), engines(v), "de", "en")
    assert s.model_extra.get("source") == "records" and not v.prompts
