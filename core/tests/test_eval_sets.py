"""eval/sets/*.jsonl (plan §12.1): every record well-formed, every language a complete set."""

import json
from collections import Counter, defaultdict
from typing import get_args

import pytest
from raum.config import repo_root
from raum.modes import load_modes
from raum.modes.schema import GuardKind

SETS = repo_root() / "eval" / "sets"
REQUIRED = {"id", "room", "src_lang", "tgt_lang", "src", "facts", "approved"}
OPTIONAL = {"ref", "perturbed", "perturbation"}
PERTURBATIONS = {"wrong_number": 3, "wrong_unit": 2, "negation": 2, "allergy_omitted": 2, "date_shifted": 1}
DEMO_LANGS = {"ru", "tr"}  # §19: the demo language is one of these; both must be complete
DEMO_SENTENCES = [  # §20.7, German side (src or ref)
    "Ich nehme zweimal täglich 500 mg Metformin.",
    "Ich bin allergisch gegen Penicillin.",
    "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.",
    "Die Klagefrist endet am 24. November.",
    "Bringen Sie bitte Ihren Arbeitsvertrag und die letzten drei Lohnabrechnungen mit.",
    "Unser Angebot liegt bei 1,2 Millionen Euro.",
    "Wir können auf 1,05 Millionen gehen, wenn die Lieferung bis März erfolgt.",
]


def _records() -> list[dict]:
    out = []
    for path in sorted(SETS.glob("*.jsonl")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            rec = json.loads(line)
            rec["_file"] = f"{path.name}:{n}"
            rec["_stem"] = path.stem
            out.append(rec)
    return out


RECORDS = _records()


def _lang(rec: dict) -> str:
    return rec["tgt_lang"] if rec["src_lang"] == "de" else rec["src_lang"]


def test_sets_exist():
    assert {p.stem for p in SETS.glob("*.jsonl")} == {"sprechzimmer", "kanzlei", "verhandlungsraum"}
    assert RECORDS


@pytest.mark.parametrize("rec", RECORDS, ids=[r["id"] for r in RECORDS])
def test_record_shape(rec):
    modes = load_modes()
    keys = set(rec) - {"_file", "_stem"}
    assert REQUIRED <= keys, f"{rec['_file']}: missing {REQUIRED - keys}"
    assert keys <= REQUIRED | OPTIONAL, f"{rec['_file']}: unknown keys {keys - REQUIRED - OPTIONAL}"
    assert rec["room"] == rec["_stem"], "record is in the wrong file"
    mode = modes[rec["room"]]
    assert rec["id"].startswith(mode.prefix + "-")
    assert "de" in (rec["src_lang"], rec["tgt_lang"]) and rec["src_lang"] != rec["tgt_lang"]
    assert isinstance(rec["src"], str) and rec["src"].strip()
    assert isinstance(rec["approved"], bool)
    assert rec["facts"], "every sentence carries at least one fact"
    for f in rec["facts"]:
        assert set(f) == {"kind", "value"}
        assert f["kind"] in get_args(GuardKind), f"unknown kind {f['kind']}"
        assert f["kind"] in mode.guard.kinds, f"kind {f['kind']} is not guarded in room {rec['room']}"
        assert isinstance(f["value"], str) and f["value"].strip()
    if "ref" in rec:
        assert isinstance(rec["ref"], str) and rec["ref"].strip()
    assert ("perturbed" in rec) == ("perturbation" in rec), "perturbed and perturbation go together"
    if "perturbed" in rec:
        p = rec["perturbed"]
        assert rec["perturbation"] in PERTURBATIONS
        assert {"tgt_text", "expect"} <= set(p) <= {"tgt_text", "expect", "note"}
        assert p["expect"] == "red"
        assert p["tgt_text"].strip() and p["tgt_text"] != rec.get("ref")


def test_ids_unique():
    ids = Counter(r["id"] for r in RECORDS)
    assert not [i for i, c in ids.items() if c > 1]


def test_every_language_is_a_complete_set():
    by_lang: dict[str, list[dict]] = defaultdict(list)
    for r in RECORDS:
        by_lang[_lang(r)].append(r)
    assert DEMO_LANGS <= set(by_lang), f"missing demo languages: {DEMO_LANGS - set(by_lang)}"
    for lang, recs in by_lang.items():
        assert len(recs) == 30, f"{lang}: {len(recs)} records"
        rooms = Counter(r["room"] for r in recs)
        assert set(rooms.values()) == {10}, f"{lang}: {rooms}"
        for room in rooms:
            dirs = {(r["src_lang"] == "de") for r in recs if r["room"] == room}
            assert dirs == {True, False}, f"{lang}/{room}: both directions needed"
        perts = Counter(r["perturbation"] for r in recs if "perturbation" in r)
        assert dict(perts) == PERTURBATIONS, f"{lang}: {perts}"
        clean = [r for r in recs if r.get("ref")]
        assert len(clean) >= 10, f"{lang}: {len(clean)} clean references"


def test_sentence_is_the_same_across_languages():
    by_sid: dict[str, list[dict]] = defaultdict(list)
    for r in RECORDS:
        by_sid[r["id"].rsplit("-", 1)[0]].append(r)
    for sid, recs in by_sid.items():
        first = recs[0]
        for r in recs[1:]:
            assert r["room"] == first["room"]
            assert r["facts"] == first["facts"], sid
            assert (r["src_lang"] == "de") == (first["src_lang"] == "de"), sid
            assert r.get("perturbation") == first.get("perturbation"), sid
            german = r["src"] if r["src_lang"] == "de" else r.get("ref")
            german_first = first["src"] if first["src_lang"] == "de" else first.get("ref")
            assert german == german_first, sid


def test_demo_sentences_included():
    for lang in {_lang(r) for r in RECORDS}:
        german = {r["src"] for r in RECORDS if _lang(r) == lang and r["src_lang"] == "de"}
        german |= {r.get("ref") for r in RECORDS if _lang(r) == lang and r["tgt_lang"] == "de"}
        missing = [s for s in DEMO_SENTENCES if s not in german]
        assert not missing, f"{lang}: §20.7 sentences missing: {missing}"
