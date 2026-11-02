"""RoomPipeline (plan §6.4) with mock engines and a collecting emit. No LiveKit, GPU or network."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from pipeline_helpers import DOC, PAT, Harness, fake_check, fake_extract, not_implemented

from raum.events import Control, ParticipantInfo, SessionState
from raum.pipeline import mockdata
from raum.pipeline import room as room_mod
from raum.pipeline import texts as T
from raum.pipeline.translate import MockTranslator
from raum.verify import guard
from raum.verify.guard import Fact


@pytest.fixture
def no_guard(monkeypatch):
    monkeypatch.setattr(guard, "check", not_implemented)
    monkeypatch.setattr(guard, "extract", not_implemented)


@pytest.fixture
def fake_guard(monkeypatch):
    monkeypatch.setattr(guard, "check", fake_check)
    monkeypatch.setattr(guard, "extract", fake_extract)


def line(mode: str, line_id: str) -> mockdata.MockLine:
    return next(ln for ln in mockdata.load_script(mode).lines if ln.id == line_id)


async def test_turn_event_order_and_content(no_guard):
    h = Harness()
    utt = await h.say(DOC.id, seconds=1.2)
    await h.p.drain()
    assert utt == "u-0001"
    types = h.types(utt)
    assert types[0] == "utterance.start" and types[-1] == "pipeline.timing"
    assert "transcript.partial" in types
    order = ["utterance.end", "transcript.final", "translation.delta", "translation.final", "verify.facts",
             "verify.result", "pipeline.timing"]
    idx = [types.index(t) for t in order]
    assert idx == sorted(idx), types
    assert types.index("transcript.partial") < types.index("utterance.end")

    sz01 = line("sprechzimmer", "sz-01")
    final = h.one("transcript.final", utt)
    assert final["text"] == sz01.text["de"] and final["lang"] == "de" and final["asr_conf"] == pytest.approx(0.93)
    tr = h.one("translation.final", utt)
    assert {k: v for k, v in tr.items() if v is not None} == {"target_lang": "ar", "text": sz01.text["ar"]}
    deltas = h.of("translation.delta", utt)
    assert "".join(d.data["delta"] for d in deltas) == tr["text"]
    assert [d.data["seq"] for d in deltas] == list(range(1, len(deltas) + 1))
    facts = h.one("verify.facts", utt)
    assert facts["facts"] == [] and facts["error"] == "fact guard unavailable"
    res = h.one("verify.result", utt)
    assert res["badge"] == "green" and res["similarity"] == pytest.approx(1.0)
    assert "fact guard unavailable" in res["reasons"]

    t = h.one("pipeline.timing", utt)
    assert t["target_lang"] == "ar" and t["vad_ms"] == 350.0
    assert t["asr_ms"] > 0 and t["mt_first_ms"] >= t["vad_ms"] and t["mt_full_ms"] >= t["mt_first_ms"]
    assert t["check_ms"] is not None and t["tts_first_ms"] is not None
    assert t["models"]["translate"] == "mock:mock-gemma"
    assert all(e.room == "sz-1000" for e in h.events)


async def test_partials_grow_and_stay_before_final(no_guard):
    h = Harness()
    utt = await h.say(DOC.id, seconds=2.0)
    parts = h.of("transcript.partial", utt)
    assert len(parts) >= 2
    texts = [p.data["text"] for p in parts]
    final = h.one("transcript.final", utt)["text"]
    assert all(final.startswith(x) for x in texts)
    assert [len(x) for x in texts] == sorted(len(x) for x in texts)
    assert [p.data["seq"] for p in parts] == list(range(1, len(parts) + 1))


async def test_multi_language_fanout_dedupes(no_guard):
    people = [DOC, PAT, ParticipantInfo(id="b-tr01", role="b", lang="tr"),
              ParticipantInfo(id="b-ar02", role="b", lang="ar"),
              ParticipantInfo(id="t-table", role="observer", lang="en")]
    h = Harness(participants=people)
    utt = await h.say(DOC.id)
    await h.p.drain()
    finals = {e.data["target_lang"]: e.data["text"] for e in h.of("translation.final", utt)}
    assert set(finals) == {"ar", "tr"}  # deduped, observers and the speaker's own language skipped
    assert len(h.of("translation.final", utt)) == 2
    assert finals["tr"] == line("sprechzimmer", "sz-01").text["tr"]
    assert {e.data["target_lang"] for e in h.of("pipeline.timing", utt)} == {"ar", "tr"}


async def test_no_listener_translates_to_other_roles_default(no_guard):
    h = Harness(participants=[PAT])
    utt = await h.say(PAT.id)
    tr = h.one("translation.final", utt)
    assert {k: v for k, v in tr.items() if v is not None} == {"target_lang": "de", "text": line("sprechzimmer", "sz-02").text["de"]}

    h2 = Harness(participants=[DOC])
    utt = await h2.say(DOC.id)
    assert h2.one("translation.final", utt)["target_lang"] == "en"  # role b default "auto" -> en


async def test_low_confidence_asks_to_repeat_and_is_not_translated(no_guard):
    h = Harness()
    h.set_next_line("b", 3)  # sz-08, conf 0.4
    utt = await h.say(PAT.id)
    await h.p.drain()
    assert h.one("transcript.final", utt)["asr_conf"] == pytest.approx(0.4)
    flag = h.one("flag", utt)
    assert flag["kind"] == "uncertain" and flag["needs_confirm"] is False
    assert flag["text"] == T.SAY_AGAIN["ar"]
    assert set(flag["texts"]) >= {"de", "en", "tr", "ru", "ar"}
    assert not [t for t in h.types(utt) if t.startswith(("translation", "verify"))]
    timing = h.one("pipeline.timing", utt)
    assert timing["asr_ms"] is not None and timing["mt_first_ms"] is None


async def test_off_record_drops_all_audio(no_guard):
    h = Harness()
    h.p.set_state(SessionState(off_record=True, sealed=False, participants=[], mode="sprechzimmer"))
    assert await h.p.speech_start(DOC.id) is None
    await h.p.speech_audio(DOC.id, np.zeros(320, dtype=np.float32))
    await h.p.speech_end(DOC.id, np.zeros(16000, dtype=np.float32), 350)
    assert h.events == []

    h.p.set_state(SessionState(off_record=False, sealed=False, participants=[], mode="sprechzimmer"))
    utt = await h.p.speech_start(DOC.id)
    await h.p.handle_control(Control(action="off_record", value=True))  # mid-utterance
    await asyncio.sleep(0)
    await h.p.speech_end(DOC.id, None, 350)
    await h.p.drain()
    assert h.types(utt) == ["utterance.start", "utterance.end"]  # bubble closed, no content

    await h.p.handle_control(Control(action="off_record", value=False))
    utt2 = await h.say(DOC.id)
    assert utt2 == "u-0002" and h.one("transcript.final", utt2)


async def test_red_path_dose_mismatch(fake_guard):
    h = Harness()
    h.set_next_line("a", 3)  # sz-07: 400 mg comes out as 40 mg
    utt = await h.say(DOC.id)
    await h.p.drain()
    tr = h.one("translation.final", utt)["text"]
    assert "40 ملغ" in tr and "400" not in tr
    facts = h.one("verify.facts", utt)
    assert facts["red"] is True
    assert {"kind": "dose", "src": "400 mg", "tgt": "40 mg", "match": False} in facts["facts"]
    flags = h.of("flag", utt)
    assert len(flags) == 1  # the guard flag; the red check does not prompt twice
    flag = flags[0].data
    assert flag["kind"] == "mismatch" and flag["needs_confirm"] is True and flag["confirm_by"] == "b"
    assert "400 mg" in flag["text"] and flag["id"] == f"{utt}-ar-mismatch"
    # read back what was SAID, as a clarification: "Nur zur Sicherheit: 400 mg?"
    assert flag["texts"]["de"] == "Nur zur Sicherheit: 400 mg?" and flag["text"] == "للتأكد فقط: 400 mg؟"
    assert flag["status"] == "unconfirmed" and flag["why"] == ["guard"] and flag["spoken"] is True
    res = h.one("verify.result", utt)
    assert res["badge"] == "red"  # fact-guard red wins over a high similarity
    assert any("fact mismatch" in r for r in res["reasons"])
    assert "40 mg" in res["back_translation"]  # the back-translation keeps the mistranslation


async def test_correct_dose_is_noted_not_asked(fake_guard):
    h = Harness()
    h.set_next_line("a", 2)  # sz-05: 500 mg Metformin (correct)
    utt = await h.say(DOC.id)
    await h.p.drain()
    assert h.one("verify.facts", utt)["red"] is False
    assert h.one("verify.result", utt)["badge"] == "green"
    flag = h.one("flag", utt)  # guard and checker green: nobody is asked, the doctor's screen notes it
    assert flag["kind"] == "noted" and flag["needs_confirm"] is False and flag["status"] == "noted"
    assert flag["show_to"] == "a" and flag["confirm_by"] is None and flag["id"] == f"{utt}-ar-noted"
    assert flag["text"] == flag["texts"]["de"] == "500 mg"
    assert [f["texts"]["de"] for f in flag["facts"]] == ["500 mg"]
    assert "Stimmt das?" not in str(flag)


async def test_verify_engine_off_uses_guard_only(fake_guard):
    h = Harness()
    h.engines.verify = None
    h.set_next_line("a", 2)
    utt = await h.say(DOC.id)
    await h.p.drain()
    res = h.one("verify.result", utt)
    assert res["similarity"] is None and res["badge"] == "green"
    assert any("second check off" in r for r in res["reasons"])


class _BrokenVerifier:
    name = "broken"

    async def back_translate(self, *a, **k):
        raise ConnectionError("gpt-oss down")


class _FarEmbedder:
    name = "far"

    async def embed(self, texts):
        return [[1.0, 0.0], [0.0, 1.0]][: len(texts)]


async def test_checker_down_never_blocks_live_path(no_guard):
    h = Harness()
    h.engines.verify = _BrokenVerifier()  # type: ignore[assignment]
    utt = await h.say(DOC.id)
    assert h.of("translation.final", utt)  # live path done before the check finishes
    await h.p.drain()
    res = h.one("verify.result", utt)
    assert res["badge"] == "amber" and res["similarity"] is None
    assert "second check failed: ConnectionError" in res["reasons"]
    assert h.one("pipeline.timing", utt)["check_ms"] is not None


async def test_red_similarity_interrupts_with_flag(no_guard):
    h = Harness()
    h.engines.embed = _FarEmbedder()  # type: ignore[assignment]
    utt = await h.say(DOC.id)
    await h.p.drain()
    res = h.one("verify.result", utt)
    assert res["badge"] == "red" and res["similarity"] == 0.0
    flag = h.one("flag", utt)
    assert flag["kind"] == "mismatch" and flag["needs_confirm"] and flag["text"] == T.UNSURE["ar"]


async def test_repeat_back_compares_numbers_without_guard(no_guard):
    h = Harness()
    h.set_next_line("a", 4)  # sz-09: Thursday, 12 November, 9 o'clock
    inst = await h.say(DOC.id)
    await h.p.handle_control(Control(action="repeat_back", by="a"))
    h.set_next_line("b", 4)  # sz-10: restatement without the date
    rest = await h.say(PAT.id)
    await h.p.drain()
    rb = h.one("repeat_back.result", rest)
    assert rb["understood"] == "partly" and "12" in rb["missing"] and rb["instruction_utt"] == inst
    # only the next role-b utterance is checked
    await h.say(PAT.id)
    await h.p.drain()
    assert len(h.of("repeat_back.result")) == 1


async def test_repeat_back_with_guard_facts(fake_guard):
    h = Harness()
    h.set_next_line("a", 2)  # sz-05: 500 mg Metformin twice a day
    await h.say(DOC.id)
    await h.p.handle_control(Control(action="repeat_back"))
    h.set_next_line("b", 4)  # sz-10 repeats 500 mg
    rest = await h.say(PAT.id)
    await h.p.drain()
    assert h.one("repeat_back.result", rest)["understood"] == "yes"


async def test_explain_simply_draft_then_send(no_guard):
    lawyer = ParticipantInfo(id="a-law", role="a", lang="de")
    client = ParticipantInfo(id="b-cli", role="b", lang="ar")
    h = Harness(mode="kanzlei", room="kz-1170", participants=[lawyer, client])
    await h.say(lawyer.id)
    utt = await h.say(lawyer.id)  # kz-03: Die Klagefrist endet am 24. November.
    await h.p.handle_control(Control(action="explain_simply", utt=utt, by="a"))
    await h.p.drain()  # runs in the background: the link keeps reading controls meanwhile
    draft = h.one("explain.draft", utt)
    assert draft == {"source_utt": utt, "target_lang": "ar", "text": line("kanzlei", "kz-03").simple["ar"]}
    await h.p.handle_control(Control(action="explain_approve", utt=utt, by="a"))
    assert h.one("explain.sent", utt)["text"] == draft["text"]
    await h.p.handle_control(Control(action="explain_approve", utt=utt, text="Bearbeitet.", by="a"))
    assert h.of("explain.sent", utt)[-1].data["text"] == "Bearbeitet."


async def test_correct_term_and_burn(no_guard):
    h = Harness()
    await h.p.handle_control(Control(action="correct_term", value={"src": "Blutdruck", "tgt": "الضغط", "lang": "ar"}))
    assert "Blutdruck = الضغط" in h.p.glossary.prompt_lines("de", "ar")
    await h.say(DOC.id)
    assert h.p.memory() and h.p.names
    await h.p.handle_control(Control(action="burn"))
    assert h.p.memory() == [] and h.p.names == []
    assert "Blutdruck = ضغط الدم" in h.p.glossary.prompt_lines("de", "ar")  # correction forgotten
    utt = await h.say(DOC.id)
    assert h.one("transcript.final", utt)["text"] == line("sprechzimmer", "sz-01").text["de"]  # script restarts


async def test_names_from_earlier_turns_reach_the_prompt(no_guard):
    h = Harness()
    seen: list[str] = []
    inner = MockTranslator({"first_token_ms": 0, "token_ms": 0})

    class Spy:
        name = "spy"

        def stream(self, req):
            seen.append(req.context)
            return inner.stream(req)

    h.engines.translate = Spy()  # type: ignore[assignment]
    await h.say(DOC.id)  # sz-01 names Frau Demir and Dr. Weber
    await h.say(PAT.id)
    assert seen[0] == "(none)"
    assert "Frau Demir" in seen[1] and "Dr. Weber" in seen[1]


async def test_offer_updates_in_verhandlungsraum(monkeypatch):
    monkeypatch.setattr(guard, "check", not_implemented)

    def extract(text, lang, *, kinds=None, glossary=None):
        if "1,2 Millionen" in text:
            return [Fact(kind="amount", value="1200000 EUR", surface="1,2 Millionen Euro")]
        return []

    monkeypatch.setattr(guard, "extract", extract)
    ceo = ParticipantInfo(id="a-ceo", role="a", lang="de")
    partner = ParticipantInfo(id="b-ptr", role="b", lang="ru")
    h = Harness(mode="verhandlungsraum", room="vr-3305", participants=[ceo, partner])
    h.set_next_line("a", 1)  # vr-03: Unser Angebot liegt bei 1,2 Millionen Euro.
    utt = await h.say(ceo.id)
    offer = h.one("offer.update", utt)
    assert offer["amount"] == 1200000.0 and offer["currency"] == "EUR" and offer["by"] == "a"
    assert offer["text"] == "1,2 Millionen Euro"

    hs = Harness()  # no offer tracker in the Sprechzimmer
    await hs.say(DOC.id)
    assert not hs.of("offer.update")


class _Voice:
    name = "voice"
    sample_rate = 16000
    simulated = False

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def synthesize(self, text, lang):
        self.calls += 1
        if self.fail:
            raise RuntimeError("piper crashed")
        for _ in range(2):
            yield np.zeros(320, dtype=np.int16)


async def test_voice_output_goes_to_sink_and_failures_never_end_it(no_guard):
    h = Harness()
    got: list[tuple[str, int, int]] = []

    async def sink(lang, pcm, rate):
        got.append((lang, len(pcm), rate))

    h.engines.tts = _Voice()  # type: ignore[assignment]
    h.p.tts_sink = sink
    utt = await h.say(DOC.id)
    await h.p.drain()
    # spoken clause by clause (C-13): every clause yields the voice's two chunks, in order
    assert got and len(got) % 2 == 0 and all(g == ("ar", 320, 16000) for g in got)
    assert h.one("pipeline.timing", utt)["tts_first_ms"] is not None

    broken = _Voice(fail=True)
    h.engines.tts = broken  # type: ignore[assignment]
    await h.say(DOC.id)
    await h.p.drain()
    await h.say(DOC.id)
    await h.p.drain()
    # each failed clause is dropped and the next one is tried (never off for the session); after
    # VOICE_MAX_FAILS in a row the language pauses briefly; text continues throughout
    assert 1 < broken.calls <= room_mod.VOICE_MAX_FAILS
    assert len(h.of("translation.final")) == 3
    h.engines.tts = _Voice()  # type: ignore[assignment]
    h.p._voice_paused_until.clear()
    got.clear()
    await h.say(DOC.id)
    await h.p.drain()
    assert got  # the voice is back


async def test_speech_end_detaches_before_first_await(no_guard):
    h = Harness()
    first = await h.p.speech_start(DOC.id)
    task = asyncio.create_task(h.p.speech_end(DOC.id, np.zeros(8000, dtype=np.float32), 350))
    await asyncio.sleep(0)
    second = await h.p.speech_start(DOC.id)
    assert (first, second) == ("u-0001", "u-0002")
    await task
    await h.p.drain()


async def test_speech_end_without_start_and_unknown_speaker(no_guard):
    h = Harness(participants=[DOC])
    await h.p.speech_end("b-zz99", np.zeros(8000, dtype=np.float32), 300)  # VAD start missed
    assert h.types("u-0001")[:2] == ["utterance.start", "utterance.end"]
    assert h.one("transcript.final", "u-0001")["lang"] == "en"  # role b from identity, no hint: en line
    await h.p.speech_end("b-zz99", None, 300)  # nothing buffered: ignored
    assert "u-0002" not in {e.utt for e in h.events}


async def test_irrelevant_or_bad_controls_are_harmless(no_guard):
    h = Harness()
    for c in [Control(action="end"), Control(action="mute", value=True), Control(action="confirm", accepted=True),
              Control(action="correct_term", value="nonsense"), Control(action="explain_simply", utt="u-0099"),
              Control(action="explain_approve", utt="u-0099"), Control(action="glossary", value={"terms": "x"})]:
        await h.p.handle_control(c)
    await h.p.drain()
    assert h.events == []
    await h.p.aclose()
    assert await h.p.speech_start(DOC.id) is None


def test_take_clause_splits_speakable_prefixes():
    from raum.pipeline.room import take_clause

    assert take_clause("Nehmen Sie zweimal täglich") == ("", "Nehmen Sie zweimal täglich")
    head, rest = take_clause("Guten Tag, Frau Demir. Ich bin Dr. Weber. Was")
    assert head == "Guten Tag, Frau Demir. Ich bin Dr. Weber." and rest == " Was"
    # ordinals and abbreviations never end a clause
    assert take_clause("Kommen Sie am Donnerstag, dem 12. November wieder")[0] == ""
    assert take_clause("Bitte kommen Sie zu Dr. Weber")[0] == ""
    # a comma ends a later clause only from COMMA_WORDS words on: the rest of a sentence is one voice request
    assert take_clause("Ja, gut")[0] == ""
    assert take_clause("Nehmen Sie die Tabletten bitte immer morgens, und abends")[0] == ""
    assert take_clause("a day, always after meals. Then") == ("a day, always after meals.", " Then")
    head, rest = take_clause("Bitte nehmen Sie ab morgen zweimal täglich 500 mg Metformin zum Essen, und wenn")
    assert head.endswith("zum Essen,") and rest == " und wenn"  # a long sentence still goes out in pieces
    assert take_clause("أتناول ٥٠٠ ملغ من الميتفورمين مرتين يوميًا، وعندي")[0] == ""  # the Arabic comma too
    head, _ = take_clause("أتناول ٥٠٠ ملغ من الميتفورمين مرتين يوميًا مع الطعام كل صباح وكل مساء دائمًا، وعندي")
    assert head.endswith("دائمًا،")


async def test_facts_skip_questions_and_valueless_facts(no_guard):
    from types import SimpleNamespace

    from raum.events import FactCheck
    from raum.verify.guard import GuardResult

    h = Harness()
    allergy_q = GuardResult(facts=[], confirm=[FactCheck(kind="allergy", src="present", tgt="present", match=True)])
    rec = SimpleNamespace(text="Sind Sie gegen irgendwelche Medikamente allergisch?", utt="u-0009", role="a",
                          lang="de", conf=0.93)
    assert await h.p._settle_facts(rec, "ar", allergy_q, "amber") is False  # a question: nothing to confirm
    rec.text = "Ich bin allergisch."
    assert await h.p._settle_facts(rec, "ar", allergy_q, "amber") is False  # no concrete allergen: nothing
    assert not h.of("flag")
    real = GuardResult(facts=[], confirm=[FactCheck(kind="allergy", src="Penicillin", tgt="البنسلين", match=True)])
    rec.text = "Ich bin allergisch gegen Penicillin."
    assert await h.p._settle_facts(rec, "ar", real, "amber") is True  # the checker unsure: read back once
    flag = h.of("flag")[-1].data
    assert flag["kind"] == "allergy" and flag["needs_confirm"] and flag["status"] == "unconfirmed"
    assert flag["texts"]["de"] == "Nur zur Sicherheit: Allergie gegen Penicillin?"
    assert flag["text"] == "للتأكد فقط: حساسية من البنسلين؟" and flag["why"] == ["check:amber"]
    assert await h.p._settle_facts(rec, "ar", real, "amber") is True and len(h.of("flag")) == 1  # never twice
