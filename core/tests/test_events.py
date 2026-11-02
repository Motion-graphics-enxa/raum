"""Event protocol (§6.5): envelope, typed payloads, parity with app/src/lib/events.ts."""

import re

import pytest
from pydantic import ValidationError

from raum.config import repo_root
from raum.events import EVENT_TYPES, Control, Envelope, make_event, parse_event


def test_envelope_roundtrip():
    env = make_event("translation.delta", "sz-4821", {"target_lang": "ar", "delta": "خذ "}, utt="u-0007")
    raw = env.to_json()
    back = parse_event(raw)
    assert back.v == 1 and back.type == "translation.delta" and back.utt == "u-0007"
    assert back.payload().delta == "خذ "


def test_plan_example_parses():
    raw = ('{ "v": 1, "type": "translation.delta", "room": "sz-4821", "utt": "u-0007",'
           ' "ts": 1793616884123, "data": { "target_lang": "en", "delta": "Take" } }')
    assert parse_event(raw).ts == 1793616884123


@pytest.mark.parametrize("etype,data", [
    ("participant.joined", {"id": "p1", "role": "b", "lang": "ar", "device": "phone", "path": "direct"}),
    ("utterance.start", {"speaker": "p1", "lang": "ar"}),
    ("utterance.continued", {"speaker": "a-doc1", "into": "u-0003"}),
    ("transcript.partial", {"speaker": "p1", "lang": "ar", "text": "..", "seq": 1}),
    ("transcript.final", {"speaker": "p1", "lang": "ar", "text": "..", "asr_conf": 0.93}),
    ("translation.final", {"target_lang": "de", "text": ".."}),
    ("translation.draft", {"target_lang": "de", "text": "..", "seq": 1}),
    ("translation.draft", {"target_lang": "de", "text": "..", "seq": 3, "seg": 2}),
    ("verify.facts", {"target_lang": "de", "facts": [{"kind": "dose", "src": "500 mg", "tgt": "50 mg", "match": False}]}),
    ("verify.result", {"target_lang": "de", "similarity": 0.91, "badge": "green", "reasons": []}),
    ("flag", {"kind": "dose", "text": "Nur zur Sicherheit: 500 mg?", "needs_confirm": True}),
    ("flag", {"kind": "dose", "text": "Nur zur Sicherheit: 500 mg?", "needs_confirm": True, "status": "unconfirmed",
              "facts": [{"kind": "dose", "texts": {"de": "500 mg"}}], "spoken": True, "why": ["check:amber"]}),
    ("flag", {"kind": "noted", "text": "Allergie: Penicillin", "needs_confirm": False, "status": "noted",
              "show_to": "a", "facts": [{"kind": "allergy", "texts": {"de": "Allergie: Penicillin"}}]}),
    ("confirm", {"by": "a", "accepted": True}),
    ("confirm", {"by": "b", "accepted": False, "flag_id": "u-0001-ar-confirm", "via": "voice", "answer_utt": "u-0002"}),
    ("offer.update", {"amount": 1200000, "currency": "EUR", "by": "a", "ts": 1}),
    ("pipeline.timing", {"vad_ms": 320, "asr_ms": 210, "mt_first_ms": 240, "mt_full_ms": 700,
                         "tts_first_ms": None, "check_ms": 1000}),
    ("voice.lock", {"speaker": "a-doc1", "accepted": False, "status": "mismatch", "score": 0.12, "threshold": 0.35}),
    ("voice.lock", {"speaker": "a-doc1", "accepted": True, "status": "enrolling"}),
    ("session.state", {"off_record": False, "sealed": False, "participants": [], "mode": "sprechzimmer"}),
    ("privacy.meter", {"third_party_bytes": 0, "tunnel_bytes": 2048, "sealed": False}),
    ("gpu.stats", {"util_pct": 41.5, "models_loaded": ["gemma4:26b"]}),
    ("summary.ready", {"template": "sprechzimmer", "lang_a": "de", "lang_b": "ar", "sections": [
        {"key": "medications", "title": {"de": "Medikamente", "ar": "الأدوية"},
         "items": [{"text": {"de": "Metformin 500 mg", "ar": "ميتفورمين 500 ملغ"}}]}],
        "facts": [{"kind": "dose", "text": {"de": "Metformin 500 mg"}, "status": "confirmed", "utt": "u-0002"}]}),
    ("burn.done", {"key_destroyed_at": 1, "audit_head_hash": "ab" * 32}),
])
def test_each_type_validates(etype, data):
    assert parse_event(make_event(etype, "sz-1", data).to_json()).type == etype


def test_bad_payload_rejected():
    with pytest.raises(ValidationError):
        make_event("verify.result", "sz-1", {"target_lang": "de", "similarity": 0.5, "badge": "purple"})
    with pytest.raises(ValueError):
        make_event("nope", "sz-1", {})
    with pytest.raises(ValidationError):
        Envelope.model_validate({"v": 2, "type": "flag", "room": "x", "data": {}})
    with pytest.raises(ValidationError):
        make_event("voice.lock", "sz-1", {"speaker": "a-doc1", "accepted": True, "status": "maybe"})


def test_control_actions():
    assert Control(action="off_record", value=True, by="a").value is True
    with pytest.raises(ValidationError):
        Control(action="selfdestruct")


def test_typescript_twin_lists_the_same_types():
    ts = (repo_root() / "app" / "src" / "lib" / "events.ts").read_text(encoding="utf-8")
    block = re.search(r"export const EVENT_TYPES = \[(.*?)\] as const", ts, re.DOTALL)
    assert block, "EVENT_TYPES array missing in events.ts"
    ts_types = set(re.findall(r'"([a-z_.]+)"', block.group(1)))
    assert ts_types == set(EVENT_TYPES)
