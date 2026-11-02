"""Facts read back like a human interpreter (raum.pipeline.readback, RoomPipeline ``_fact_flags`` /
``_settle_facts``): green -> a quiet "noted" chip for the professional, nobody asked; uncertain -> ONE spoken
read-back in the listener's language, answered by their next short yes/no; never asked twice; summary + audit."""

from __future__ import annotations

import math

import numpy as np
import pytest
from pipeline_helpers import DOC, PAT, Harness, fast_config

from raum.events import Control, ParticipantInfo, make_event
from raum.modes import get_mode
from raum.pipeline import readback
from raum.pipeline.base import AsrResult
from raum.summary import build_summary, collect_facts

# ---------------------------------------------------------------- the spoken answer


@pytest.mark.parametrize(("text", "lang", "want"), [
    ("Ja.", "de", True), ("Ja, genau.", "de", True), ("Genau", "de", True), ("Richtig.", "de", True),
    ("Stimmt.", "de", True), ("Äh, ja.", "de", True), ("Ja, das ist richtig.", "de", True), ("Das stimmt.", "de", True),
    ("Nein.", "de", False), ("Nee", "de", False), ("Stimmt nicht.", "de", False), ("Nein, 40 mg.", "de", False),
    ("Yes.", "en", True), ("Yeah, correct.", "en", True), ("That's right.", "en", True), ("Right.", "en", True),
    ("No.", "en", False), ("Nope", "en", False), ("That's wrong.", "en", False),
    ("Evet.", "tr", True), ("Evet evet.", "tr", True), ("Doğru.", "tr", True), ("Hayır.", "tr", False),
    ("Doğru değil.", "tr", False),
    ("Да.", "ru", True), ("Да-да.", "ru", True), ("Всё верно.", "ru", True), ("Нет.", "ru", False), ("Не", "ru", False),
    ("نعم.", "ar", True), ("أجل", "ar", True), ("نعم، صحيح", "ar", True), ("لا.", "ar", False), ("كلا", "ar", False),
    ("Yes", "ar", True),  # English is understood in every language
])
def test_yes_no_answers(text, lang, want):
    assert readback.yes_no(text, lang) is want


@pytest.mark.parametrize(("text", "lang"), [
    ("Ja, und wann soll ich wiederkommen?", "de"),  # longer than 4 words: a new turn, not an answer
    ("Ich nehme sie abends.", "de"),  # does not start with yes/no
    ("Nein, ja", "de"), ("Нет, всё верно", "ru"),  # both: no answer
    ("Ich weiß nicht.", "de"), ("I don't know", "en"), ("Не знаю.", "ru"), ("لا أعرف", "ar"),  # don't know
    ("Ne?", "tr"),  # Turkish "what?", not the German "ne"
    ("", "de"),
])
def test_not_an_answer(text, lang):
    assert readback.yes_no(text, lang) is None


# ---------------------------------------------------------------- wording


def guard_facts(src, sl, tgt, tl, mode="sprechzimmer"):
    from raum.glossary import Glossary
    from raum.verify import guard

    m = get_mode(mode)
    g = Glossary.load(m.glossary) if m.glossary else None
    res = guard.check(src, sl, tgt, tl, kinds=m.guard.kinds, confirm_on=m.guard.confirm_on, glossary=g)
    vals = {(f.kind, f.surface.strip().casefold()): f.value for f in guard.extract(src, sl, kinds=m.guard.kinds,
                                                                                    glossary=g)}
    return res, readback.Wording(sl, tl, g, vals)


def test_a_dose_is_one_chip_with_its_drug_and_frequency_in_every_language():
    res, w = guard_facts("Nehmen Sie zweimal täglich 500 mg Metformin weiter, morgens und abends zum Essen.", "de",
                         "استمري في تناول 500 ملغ من ميتفورمين مرتين يوميًا، صباحًا ومساءً مع الطعام.", "ar")
    gs = readback.groups(res.confirm, res.facts)
    chips = w.chips(gs, ["de", "en", "ar"])
    assert [c["texts"] for c in chips] == [{"de": "Metformin 500 mg, zweimal täglich", "en": "Metformin 500 mg, twice a day",
                                            "ar": "ميتفورمين 500 ملغ، مرتين يوميًا"}]
    assert w.question(gs, "ar") == "للتأكد فقط: ميتفورمين 500 ملغ، مرتين يوميًا؟"  # the listener's own words
    assert w.question(gs, "de") == "Nur zur Sicherheit: Metformin 500 mg, zweimal täglich?"
    assert readback.spoken(w.question(gs, "de"), "de") == "Nur zur Sicherheit: Metformin 500 Milligramm, zweimal täglich?"


def test_allergy_in_the_glossary_form_and_a_mismatch_reads_back_what_was_said():
    res, w = guard_facts("Ich bin allergisch gegen Penicillin.", "de", "Penisiline alerjim var.", "tr")
    gs = readback.groups(res.confirm, res.facts)
    assert [c["texts"]["de"] for c in w.chips(gs, ["de"])] == ["Allergie: Penicillin"]  # the allergen not twice
    assert w.question(gs, "tr") == "Emin olmak için: penisilin alerjisi?"  # not the inflected "Penisiline"
    assert w.question(gs, "en") == "Just to be sure: allergy to penicillin?"
    assert w.question(gs, "ru") == "Просто уточню: аллергия на пенициллин?"

    res, w = guard_facts("Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.", "de",
                         "عودي يوم الخميس، 13 نوفمبر، الساعة 9.", "ar")
    bad = [f for f in res.facts if not f.match]
    gs = readback.groups(bad, res.facts)
    assert w.question(gs, "ar") == "للتأكد فقط: 12 نوفمبر؟"  # the date SAID, in the listener's language
    assert w.question(gs, "de") == "Nur zur Sicherheit: 12. November?"


def test_no_exam_question_is_left():
    from raum.pipeline import texts as T

    assert not hasattr(T, "CORRECT") and not hasattr(T, "MISMATCH")
    assert T.JUST_TO_BE_SURE["de"] == "Nur zur Sicherheit: {facts}?" and set(T.JUST_TO_BE_SURE) == set(T.LANGS)


# ---------------------------------------------------------------- the pipeline


class Voice:
    """Records what the room's voice says, per language."""

    name = "voice"
    sample_rate = 16000
    simulated = False

    def __init__(self, h: Harness) -> None:
        self.made: list[tuple[str, str]] = []
        self.spoken: list[tuple[str, str]] = []
        h.engines.tts = self  # type: ignore[assignment]
        h.p.tts_sink = self.sink

    async def synthesize(self, text, lang):
        self.made.append((lang, text))
        yield np.full(160, len(self.made) - 1, dtype=np.int16)

    async def sink(self, lang, pcm, rate):
        self.spoken.append(self.made[int(pcm[0])])


class Near:
    """An embedder that puts source and back-translation at a fixed similarity (the checker's verdict)."""

    name = "near"

    def __init__(self, sim: float) -> None:
        self.sim = sim

    async def embed(self, texts):
        return [[1.0, 0.0], [self.sim, math.sqrt(max(0.0, 1 - self.sim ** 2))]][: len(texts)]


def amber(h: Harness) -> None:
    h.cfg.thresholds.green, h.cfg.thresholds.amber = 0.9, 0.5
    h.engines.embed = Near(0.7)  # type: ignore[assignment]


def saying(h: Harness, text: str, lang: str, conf: float = 0.93) -> None:
    """The next utterance's transcript (partials and final)."""

    async def transcribe(audio, *, lang_hint, partial, turn):
        return AsrResult(text=text, lang=lang, conf=conf, ms=1.0, has_conf=True)

    h.engines.asr.transcribe = transcribe  # type: ignore[method-assign]


def flags(h: Harness, utt: str | None = None) -> list[dict]:
    return [e.data for e in h.of("flag", utt)]


async def test_green_facts_are_noted_for_the_doctor_and_nobody_is_asked():
    h = Harness()
    voice = Voice(h)
    h.set_next_line("a", 2)  # sz-05: Metformin 500 mg twice a day (real guard, mock checker green)
    utt = await h.say(DOC.id)
    await h.p.drain()
    (flag,) = flags(h, utt)
    assert flag["kind"] == "noted" and flag["needs_confirm"] is False and flag["show_to"] == "a"
    assert flag["text"] == "Metformin 500 mg, zweimal täglich"  # in the doctor's language
    assert flag["facts"][0]["texts"]["ar"] == "ميتفورمين 500 ملغ، مرتين يوميًا"
    assert all(lang == "ar" for lang, _ in voice.spoken) and len(voice.spoken) >= 1  # the translation only
    assert not any("للتأكد" in t for _, t in voice.spoken)  # no read-back
    assert h.p._asks == {}

    h.set_next_line("b", 1)  # sz-04: the patient's allergy, to the doctor: noted on the doctor's screen too
    utt = await h.say(PAT.id)
    await h.p.drain()
    (flag,) = flags(h, utt)
    assert flag["kind"] == "noted" and flag["text"] == "Allergie: Penicillin" and flag["target_lang"] == "de"


async def test_uncertain_dose_is_read_back_and_a_spoken_yes_confirms_it():
    h = Harness()
    voice = Voice(h)
    amber(h)
    h.set_next_line("a", 2)
    utt = await h.say(DOC.id)
    await h.p.drain()
    (flag,) = flags(h, utt)
    assert flag["kind"] == "dose" and flag["needs_confirm"] is True and flag["confirm_by"] == "b"
    assert flag["status"] == "unconfirmed" and flag["why"] == ["check:amber"] and flag["spoken"] is True
    assert flag["text"] == "للتأكد فقط: ميتفورمين 500 ملغ، مرتين يوميًا؟"
    assert flag["texts"]["de"] == "Nur zur Sicherheit: Metformin 500 mg, zweimal täglich?"
    # spoken in the patient's language, after the utterance's own voice, units in words
    assert voice.spoken[-1] == ("ar", "للتأكد فقط: ميتفورمين 500 ملليغرام، مرتين يوميًا؟")

    saying(h, "نعم.", "ar")
    answer = await h.say(PAT.id)
    await h.p.drain()
    (c,) = [e for e in h.of("confirm")]
    assert c.utt == utt and c.data == {"by": "b", "accepted": True, "flag_id": flag["id"], "via": "voice",
                                       "answer_utt": answer}
    assert h.one("translation.final", answer)["target_lang"] == "de"  # the answer is still translated
    assert h.p._asks == {}


async def test_spoken_no_rejects_and_an_unrelated_reply_leaves_it_unconfirmed_without_nagging():
    h = Harness()
    Voice(h)
    amber(h)
    h.set_next_line("a", 2)
    first = await h.say(DOC.id)
    await h.p.drain()
    saying(h, "لا، 400.", "ar")
    await h.say(PAT.id)
    await h.p.drain()
    assert [e.data["accepted"] for e in h.of("confirm")] == [False]

    h2 = Harness()
    Voice(h2)
    amber(h2)
    h2.set_next_line("a", 2)
    await h2.say(DOC.id)
    await h2.p.drain()
    saying(h2, "متى يجب أن أعود إلى العيادة مرة أخرى؟", "ar")  # a new question: the window closes
    await h2.say(PAT.id)
    saying(h2, "نعم.", "ar")  # too late: only the next utterance answers
    await h2.say(PAT.id)
    await h2.p.drain()
    assert not h2.of("confirm")
    h2.engines.asr = h.engines.asr.__class__()  # back to the mock script
    h2.set_next_line("a", 2)  # the same dose again, still amber: never read back a second time
    again = await h2.say(DOC.id)
    await h2.p.drain()
    assert flags(h2, again) == [] and len([f for f in flags(h2) if f["needs_confirm"]]) == 1
    assert first == "u-0001"


async def test_a_tap_answers_and_a_later_spoken_word_is_not_taken():
    h = Harness()
    Voice(h)
    amber(h)
    h.set_next_line("a", 2)
    await h.say(DOC.id)
    await h.p.drain()
    fid = flags(h)[0]["id"]
    await h.p.handle_control(Control(action="confirm", flag_id=fid, accepted=True, by="b"))  # core forwards the tap
    saying(h, "لا.", "ar")
    await h.say(PAT.id)
    await h.p.drain()
    assert not h.of("confirm")  # the agent emits none: the tap's confirm came from core


async def test_low_asr_confidence_on_a_fact_asks():
    h = Harness()
    Voice(h)
    from raum.pipeline import mockdata

    sz05 = next(ln for ln in mockdata.load_script("sprechzimmer").lines if ln.id == "sz-05")
    saying(h, sz05.text["de"], "de", conf=0.58)  # >= asr_min_conf 0.55 (translated), < asr_confirm_conf 0.6
    utt = await h.say(DOC.id)
    await h.p.drain()
    (flag,) = flags(h, utt)
    assert flag["needs_confirm"] is True and flag["why"] == ["asr"]


@pytest.mark.parametrize(("room_floor", "conf", "asked"), [(None, 0.65, False), (0.7, 0.65, True), (0.7, 0.75, False)])
async def test_asr_confirm_floor_default_and_per_room(room_floor, conf, asked):
    """Box data: 0.7 read back ~1 in 5 utterances with a fact; the default is 0.6, a room may be stricter."""
    from raum.pipeline import mockdata

    h = Harness()
    Voice(h)
    assert h.p.cfg.asr_confirm_conf == 0.6
    if room_floor is not None:
        h.p.mode = h.p.mode.model_copy(deep=True)
        h.p.mode.pipeline.asr_confirm_conf = room_floor
    sz05 = next(ln for ln in mockdata.load_script("sprechzimmer").lines if ln.id == "sz-05")
    saying(h, sz05.text["de"], "de", conf=conf)
    utt = await h.say(DOC.id)
    await h.p.drain()
    (flag,) = flags(h, utt)
    assert flag["needs_confirm"] is asked


async def test_guard_mismatch_asks_at_once_and_the_doctor_sees_it_unconfirmed():
    h = Harness()
    voice = Voice(h)
    h.set_next_line("a", 3)  # sz-07: 400 mg -> 40 mg
    utt = await h.say(DOC.id)
    await h.p.drain()
    (flag,) = flags(h, utt)
    assert flag["kind"] == "mismatch" and flag["why"] == ["guard"] and flag["status"] == "unconfirmed"
    assert flag["texts"]["de"] == "Nur zur Sicherheit: Ibuprofen 400 mg, dreimal täglich?"
    assert "400" in flag["text"] and "40 " not in flag["text"].replace("400", "")  # what was SAID
    assert flag["facts"][0]["texts"]["de"] == "Ibuprofen 400 mg, dreimal täglich" and flag["facts"][0]["match"] is False
    assert voice.spoken[-1][0] == "ar" and "400 ملليغرام" in voice.spoken[-1][1]


async def test_the_doctor_is_asked_in_german_when_the_patient_speaks():
    h = Harness()
    voice = Voice(h)
    amber(h)
    h.set_next_line("b", 1)  # sz-04: allergic to penicillin
    utt = await h.say(PAT.id)
    await h.p.drain()
    (flag,) = flags(h, utt)
    assert flag["confirm_by"] == "a" and flag["text"] == "Nur zur Sicherheit: Allergie gegen Penicillin?"
    assert voice.spoken[-1] == ("de", "Nur zur Sicherheit: Allergie gegen Penicillin?")
    saying(h, "Ja, genau.", "de")
    await h.say(DOC.id)
    await h.p.drain()
    assert h.one("confirm")["accepted"] is True and h.one("confirm")["by"] == "a"


async def test_gespraech_stays_quiet_unless_the_guard_is_red():
    gs_a = ParticipantInfo(id="a-host1", role="a", lang="de")
    gs_b = ParticipantInfo(id="b-gst01", role="b", lang="en")
    cfg = fast_config()
    cfg.pipeline.simultaneous = "off"
    h = Harness(mode="gespraech", room="gs-1000", participants=[gs_a, gs_b], cfg=cfg)
    voice = Voice(h)
    saying(h, "Wir treffen uns am Freitag um 18 Uhr.", "de")
    said = {"text": "We will meet on Friday at 6 pm."}

    async def translate(req):
        yield said["text"]

    h.engines.translate.stream = translate  # type: ignore[method-assign]
    await h.say(gs_a.id)
    await h.p.drain()
    assert h.of("verify.facts")[-1].data["red"] is False
    assert flags(h) == []  # confirm_on: []: no chip, no question
    spoken = len(voice.spoken)

    said["text"] = "We will meet on Friday at 7 pm."  # the translation gets the time wrong: the guard is red
    utt = await h.say(gs_a.id)
    await h.p.drain()
    (flag,) = flags(h, utt)
    assert flag["kind"] == "mismatch" and flag["spoken"] is True and flag["text"].startswith("Just to be sure:")
    assert len(voice.spoken) > spoken and voice.spoken[-1][1].startswith("Just to be sure:")


async def test_burn_forgets_open_questions():
    h = Harness()
    Voice(h)
    amber(h)
    h.set_next_line("a", 2)
    await h.say(DOC.id)
    await h.p.drain()
    assert h.p._asks and h.p._asked
    await h.p.handle_control(Control(action="burn"))
    assert h.p._asks == {} and h.p._asked == {}


# ---------------------------------------------------------------- summary


def rec(type_, data, utt):
    return make_event(type_, "sz-1", data, utt=utt).model_dump(mode="json")


def fact(texts, kind="dose"):
    return {"kind": kind, "texts": texts}


def conversation():
    return [
        rec("transcript.final", {"speaker": "b-ab12", "lang": "ar", "text": "لدي حساسية من البنسلين.", "asr_conf": 0.9},
            "u-0001"),
        rec("translation.final", {"target_lang": "de", "text": "Ich bin allergisch gegen Penicillin."}, "u-0001"),
        rec("flag", {"kind": "noted", "text": "Allergie: Penicillin", "needs_confirm": False, "id": "u-0001-de-noted",
                     "status": "noted", "show_to": "a",
                     "facts": [fact({"de": "Allergie: Penicillin", "ar": "حساسية: البنسلين"}, "allergy")]}, "u-0001"),
        rec("transcript.final", {"speaker": "a-ab12", "lang": "de", "text": "Nehmen Sie 500 mg Metformin.",
                                 "asr_conf": 0.9}, "u-0002"),
        rec("translation.final", {"target_lang": "ar", "text": "تناولي 500 ملغ من ميتفورمين."}, "u-0002"),
        rec("flag", {"kind": "dose", "text": "للتأكد فقط: ميتفورمين 500 ملغ؟", "needs_confirm": True,
                     "id": "u-0002-ar-confirm", "status": "unconfirmed", "confirm_by": "b",
                     "facts": [fact({"de": "Metformin 500 mg", "ar": "ميتفورمين 500 ملغ"})]}, "u-0002"),
        rec("transcript.final", {"speaker": "a-ab12", "lang": "de", "text": "Und Ibuprofen 400 mg bei Schmerzen.",
                                 "asr_conf": 0.9}, "u-0003"),
        rec("translation.final", {"target_lang": "ar", "text": "وإيبوبروفين 40 ملغ عند الألم."}, "u-0003"),
        rec("flag", {"kind": "mismatch", "text": "للتأكد فقط: إيبوبروفين 400 ملغ؟", "needs_confirm": True,
                     "id": "u-0003-ar-mismatch", "status": "unconfirmed", "confirm_by": "b",
                     "facts": [fact({"de": "Ibuprofen 400 mg", "ar": "إيبوبروفين 400 ملغ"})]}, "u-0003"),
        rec("confirm", {"by": "b", "accepted": True, "flag_id": "u-0003-ar-mismatch", "via": "voice",
                        "answer_utt": "u-0004"}, "u-0003"),
    ]


def test_summary_files_each_fact_with_what_became_of_it():
    facts = collect_facts(conversation(), "de", "ar")
    assert [(f.text["de"], f.status) for f in facts] == [
        ("Allergie: Penicillin", "noted"), ("Metformin 500 mg", "unconfirmed"), ("Ibuprofen 400 mg", "confirmed")]
    assert facts[0].text["ar"] == "حساسية: البنسلين" and facts[1].utt == "u-0002"
    recs = conversation()
    recs.append(rec("confirm", {"by": "b", "accepted": False, "flag_id": "u-0002-ar-confirm", "via": "button"}, "u-0002"))
    assert [f.status for f in collect_facts(recs, "de", "ar")] == ["noted", "rejected", "confirmed"]


async def test_summary_ready_carries_the_facts_and_the_file_note_lists_them():
    s = await build_summary(get_mode("sprechzimmer"), conversation(), None, "de", "ar")
    assert [(f.kind, f.status) for f in s.facts] == [("allergy", "noted"), ("dose", "unconfirmed"),
                                                     ("dose", "confirmed")]
    note = s.file_note["de"]
    assert "Geprüfte Fakten: Allergie: Penicillin (notiert); Metformin 500 mg (unbestätigt); Ibuprofen 400 mg " \
           "(bestätigt)." in note
    meds = next(x for x in s.sections if x.key == "medications")
    unconfirmed = next(i for i in meds.items if (i.model_extra or {}).get("utt") == "u-0002")
    assert unconfirmed.uncertain is True and unconfirmed.model_extra["status"] == "unconfirmed"


# ---------------------------------------------------------------- core: store + audit


def test_core_audits_noted_asked_and_a_spoken_answer(tmp_path, monkeypatch):
    from core_doubles import FakePublisher, ev, install_security, make_config
    from fastapi.testclient import TestClient

    from raum.api.server import create_app
    from raum.link import KEY_HEADER

    monkeypatch.delenv("RAUM_PROFILE", raising=False)
    install_security(monkeypatch, tmp_path)
    app = create_app(make_config(tmp_path))
    core = app.state.core
    core.sessions.publisher = FakePublisher()
    room = "sz-4821"
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        j = client.post("/api/join", json={"room": room, "role": "a", "lang": "de", "device": "phone",
                                           "consent": True}).json()
        with client.websocket_connect(f"/api/internal/agent/{room}", headers={KEY_HEADER: "dev-internal"}) as agent:
            agent.receive_json()
            agent.send_json({"kind": "hello", "room": room, "agent": "w-1"})
            for e in conversation()[2:]:
                agent.send_json(ev(e["type"], room, e["data"], utt=e["utt"]))
            agent.send_json(ev("translation.final", room, {"target_lang": "de", "text": "Ja."}, utt="u-0009"))
            for _ in range(200):
                stored = core.sessions.rooms[room].session.store.read_all()
                if stored and stored[-1]["type"] == "translation.final":
                    break
                import time
                time.sleep(0.01)
        entries = core.sessions.audit.entries(session=j["session_id"])
        got = [(e["event"], e["meta"].get("flag_id"), e["meta"].get("via")) for e in entries
               if e["event"] in ("fact_noted", "fact_asked", "confirm")]
        assert got == [("fact_noted", "u-0001-de-noted", None), ("fact_asked", "u-0002-ar-confirm", None),
                       ("fact_asked", "u-0003-ar-mismatch", None), ("confirm", "u-0003-ar-mismatch", "voice")]
        assert all("Penicillin" not in str(e) and "Metformin" not in str(e) for e in entries)  # no content
        report = client.get(f"/api/rooms/{room}/compliance").json()
        assert [(f["flag_id"], f["status"]) for f in report["checked_facts"]] == [
            ("u-0001-de-noted", "noted"), ("u-0002-ar-confirm", "unconfirmed"), ("u-0003-ar-mismatch", "confirmed")]


def test_a_missing_second_opinion_is_no_reason_to_ask():
    """On the box gpt-oss loads for minutes: "checker warming up" / "second check failed" ambers keep their badge
    on the card but never read a fact back; a real amber (similarity) or a missing guard does."""
    from raum.pipeline.room import ask_badge
    from raum.verify.backtranslate import CheckResult

    assert ask_badge(CheckResult(similarity=None, badge="amber", reasons=["checker warming up"])) == "green"
    assert ask_badge(CheckResult(similarity=None, badge="amber", reasons=["second check failed: ReadTimeout"])) == "green"
    assert ask_badge(CheckResult(similarity=0.84, badge="amber", reasons=["unsure, please confirm (similarity 0.84)"])) \
        == "amber"
    assert ask_badge(CheckResult(similarity=None, badge="amber", reasons=["fact guard unavailable"])) == "amber"
    assert ask_badge(CheckResult(similarity=None, badge="red", reasons=["checker warming up"])) == "red"
    assert ask_badge(CheckResult(similarity=0.95, badge="green", reasons=[])) == "green"
