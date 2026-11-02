"""Mock engines, mock data and engine selection (plan §6.3, §6.7, §20.7)."""

from __future__ import annotations

import numpy as np
import pytest

from raum.config import load_config
from raum.pipeline import mockdata
from raum.pipeline import texts as T
from raum.pipeline._util import lang_code, render_prompt, sample_ms
from raum.pipeline.asr import MockASR, WhisperCppASR
from raum.pipeline.base import (
    ASR,
    TTS,
    Denoiser,
    Embedder,
    TranslateRequest,
    Translator,
    TurnContext,
    Verifier,
)
from raum.pipeline.denoise import PassthroughDenoiser, make_denoiser
from raum.pipeline.embed import MockEmbedder, OllamaEmbedder
from raum.pipeline.engines import build_engines
from raum.pipeline.translate import MockTranslator, OllamaTranslator
from raum.pipeline.tts import MockTTS
from raum.pipeline.verifier import MockVerifier, OllamaVerifier
from raum.verify.similarity import cosine

LANGS = {"de", "en", "tr", "ru", "ar"}
DEMO_SENTENCES = {  # plan §20.7
    "sprechzimmer": ["Ich nehme zweimal täglich 500 mg Metformin.", "Ich bin allergisch gegen Penicillin.",
                     "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder."],
    "kanzlei": ["Die Klagefrist endet am 24. November.",
                "Bringen Sie bitte Ihren Arbeitsvertrag und die letzten drei Lohnabrechnungen mit."],
    "verhandlungsraum": ["Unser Angebot liegt bei 1,2 Millionen Euro.",
                         "Wir können auf 1,05 Millionen gehen, wenn die Lieferung bis März erfolgt."],
}


def turn(utt: str, role: str = "a", mode: str = "sprechzimmer", room: str = "sz-1", speech_ms: float = 0.0):
    return TurnContext(room=room, mode=mode, speaker=f"{role}-x", role=role, utt=utt, speech_ms=speech_ms)


def req(text: str, src: str, tgt: str, mode: str = "sprechzimmer") -> TranslateRequest:
    return TranslateRequest(text=text, src_lang=src, tgt_lang=tgt, room_name="Consultation room", role_a="Doctor",
                            role_b="Patient", lang_a="de", lang_b="ar", turn=turn("u-0001", mode=mode))


@pytest.mark.parametrize("mode", sorted(DEMO_SENTENCES))
def test_mock_scripts(mode):
    lines = mockdata.load_script(mode).lines
    assert 8 <= len(lines) <= 10
    assert [ln.role for ln in lines] == ["a", "b"] * (len(lines) // 2)
    for ln in lines:
        assert set(ln.text) >= LANGS, ln.id
    de = [ln.text["de"] for ln in lines]
    for s in DEMO_SENTENCES[mode]:
        assert any(s in t for t in de), s
    wrong = [ln for ln in lines if ln.mistranslate]
    assert len(wrong) == 1
    assert not any(s in wrong[0].text["de"] for s in DEMO_SENTENCES[mode])  # never a headline line
    assert lines.index(wrong[0]) >= 4  # a later line
    low = [ln for ln in lines if ln.conf < 0.55]
    assert len(low) == 1 and lines.index(low[0]) >= 5
    for ln in lines:  # the mistranslation must change every target text
        if ln.mistranslate:
            assert all(ln.target(lang) != ln.text[lang] for lang in LANGS), ln.id


def test_mock_asr_cursor_per_room_and_role():
    asr = MockASR({"latency_ms": 0})

    async def final(utt, role, room="sz-1", lang="de"):
        return await asr.transcribe(np.zeros(1600, dtype=np.float32), lang_hint=lang, partial=False,
                                    turn=turn(utt, role, room=room))

    import asyncio

    async def run():
        a1 = await final("u-1", "a")
        b1 = await final("u-2", "b", lang="ar")
        a2 = await final("u-3", "a")
        other_room = await final("u-1", "a", room="sz-2")
        unknown_lang = await final("u-4", "b", lang="uk")
        return a1, b1, a2, other_room, unknown_lang

    a1, b1, a2, other, unk = asyncio.run(run())
    script = {ln.id: ln for ln in mockdata.load_script("sprechzimmer").lines}
    assert a1.text == script["sz-01"].text["de"] and a1.lang == "de" and a1.conf == pytest.approx(0.93)
    assert b1.text == script["sz-02"].text["ar"] and b1.lang == "ar"
    assert a2.text == script["sz-03"].text["de"]
    assert other.text == script["sz-01"].text["de"]  # per-room cursor
    assert unk.text == script["sz-04"].text["en"] and unk.lang == "en"  # fallback en


async def test_mock_asr_partials_reveal_words_by_speech_time():
    asr = MockASR({"latency_ms": 0, "word_ms": 400})
    t = turn("u-9", speech_ms=800)
    p1 = await asr.transcribe(np.zeros(10, dtype=np.float32), lang_hint="de", partial=True, turn=t)
    t.speech_ms = 1600
    p2 = await asr.transcribe(np.zeros(10, dtype=np.float32), lang_hint="de", partial=True, turn=t)
    fin = await asr.transcribe(np.zeros(10, dtype=np.float32), lang_hint="de", partial=False, turn=t)
    assert len(p1.text.split()) == 2 and len(p2.text.split()) == 4
    assert fin.text.startswith(p2.text) and len(fin.text) > len(p2.text)


async def test_mock_translator_streams_known_and_unknown_text():
    tr = MockTranslator({"first_token_ms": 0, "token_ms": 0})
    sz05 = next(ln for ln in mockdata.load_script("sprechzimmer").lines if ln.id == "sz-05")
    deltas = [d async for d in tr.stream(req(sz05.text["de"], "de", "ru"))]
    assert len(deltas) > 3 and "".join(deltas) == sz05.text["ru"]
    unknown = "".join([d async for d in tr.stream(req("Hallo Welt", "de", "ar"))])
    assert unknown == "[ar] Hallo Welt"
    # lookup is by text in any language, punctuation-insensitive
    back = "".join([d async for d in tr.stream(req(sz05.text["ar"].rstrip("."), "ar", "de"))])
    assert back == sz05.text["de"]


async def test_mock_translator_mistranslates_one_line_and_verifier_keeps_it():
    sz07 = next(ln for ln in mockdata.load_script("sprechzimmer").lines if ln.id == "sz-07")
    tr = MockTranslator({"first_token_ms": 0, "token_ms": 0})
    out = "".join([d async for d in tr.stream(req(sz07.text["de"], "de", "en"))])
    assert "40 mg" in out and "400 mg" not in out
    ver = MockVerifier({"latency_ms": 0})
    back = await ver.back_translate(out, "en", "de", turn("u-1"))
    assert "Ibuprofen 40 mg" in back
    ok = await ver.back_translate(sz07.text["en"], "en", "de", turn("u-1"))
    assert ok == sz07.text["de"]
    assert await ver.back_translate("[ar] Hallo Welt", "ar", "de") == "Hallo Welt"


async def test_mock_verifier_json_and_simple_version():
    ver = MockVerifier({"latency_ms": 0})
    j = await ver.complete_json("Instruction given by the doctor (de): 500 mg 2x\nPatient's restatement (ar): 50 mg",
                                purpose="repeat_back")
    assert j["understood"] == "no" and "500" in j["missing"]
    kz03 = next(ln for ln in mockdata.load_script("kanzlei").lines if ln.id == "kz-03")
    assert await ver.simple_version(kz03.text["de"], "de", "tr", "kanzlei") == kz03.simple["tr"]


async def test_mock_embedder_is_deterministic_and_meaningful():
    emb = MockEmbedder()
    a, b, c, d = await emb.embed(["Nehmen Sie 400 mg", "Nehmen Sie 400 mg", "Nehmen Sie 40 mg", "Guten Morgen Welt"])
    assert a == b and len(a) == 512
    assert cosine(a, b) == pytest.approx(1.0)
    assert cosine(a, c) > cosine(a, d)
    assert MockEmbedder().vector("x y z") == emb.vector("x y z")


async def test_mock_tts_yields_nothing():
    tts = MockTTS({"first_audio_ms": 1})
    assert [c async for c in tts.synthesize("Hallo", "de")] == []


async def test_build_engines_mock_profile():
    eng = build_engines(load_config(), seed=1)
    assert isinstance(eng.asr, MockASR) and isinstance(eng.translate, MockTranslator)
    assert isinstance(eng.verify, MockVerifier) and isinstance(eng.embed, MockEmbedder)
    assert isinstance(eng.tts, MockTTS) and isinstance(eng.denoise, PassthroughDenoiser)
    for obj, proto in [(eng.asr, ASR), (eng.translate, Translator), (eng.verify, Verifier), (eng.embed, Embedder),
                       (eng.tts, TTS), (eng.denoise, Denoiser)]:
        assert isinstance(obj, proto)
    assert eng.names["asr"] == "mock:mock-whisper"
    await eng.aclose()


async def test_build_engines_spark_and_lite(monkeypatch):
    monkeypatch.setenv("RAUM_PROFILE", "spark")
    eng = build_engines(load_config())
    assert isinstance(eng.asr, WhisperCppASR) and eng.asr.url == "http://127.0.0.1:8081"
    assert isinstance(eng.translate, OllamaTranslator) and eng.translate.model == "gemma4:e4b"
    assert isinstance(eng.verify, OllamaVerifier) and eng.verify.model == "gpt-oss:20b"
    assert isinstance(eng.embed, OllamaEmbedder)
    assert eng.denoise.name in ("deepfilternet", "passthrough")  # falls back without the optional package
    await eng.aclose()

    monkeypatch.setenv("RAUM_PROFILE", "lite")
    eng = build_engines(load_config())
    assert eng.verify is None and eng.embed is None and eng.tts is None
    assert eng.names["verify"] == "off" and eng.names["tts"] == "off"
    await eng.aclose()


def test_unknown_engine_is_an_error():
    cfg = load_config()
    cfg.models.translate.engine = "nope"
    with pytest.raises(ValueError, match="translate"):
        build_engines(cfg)
    with pytest.raises(ValueError):
        make_denoiser("nope")


def test_helpers():
    assert render_prompt('{"a": 1} {x} {y}', x="X") == '{"a": 1} X {y}'
    assert sample_ms(250) == 250 and 1 <= sample_ms([1, 2]) <= 2 and sample_ms(None, 7) == 7
    assert lang_code("German") == "de" and lang_code("de-DE") == "de" and lang_code(None) is None
    assert T.fact_text("frequency", "2/day", "de") == "zweimal täglich"
    assert T.fact_text("frequency", "3/day", "ru") == "3 раза в день"
    assert T.fact_text("amount", "1200000 EUR", "de") == "Betrag: 1.200.000 EUR"
    assert T.fact_text("amount", "1050000 EUR", "en") == "Amount: 1,050,000 EUR"
    assert T.fact_text("allergy", "Penicillin", "tr") == "Alerji: Penicillin"
    assert T.fact_text("dose", "500 mg", "ar") == "500 mg"
    assert T.format_number(1.05, "de") == "1,05"
