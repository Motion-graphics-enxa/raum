"""Room schema (§6.6): loader and validation."""

from pathlib import Path

import pytest
import yaml

from raum.config import repo_root
from raum.glossary import Glossary
from raum.modes import ModeError, get_mode, load_mode, load_modes, mode_for_room, new_room_id

ROOT = repo_root()


def test_four_rooms_load():
    modes = load_modes()
    assert set(modes) == {"sprechzimmer", "kanzlei", "verhandlungsraum", "gespraech"}
    assert {m.prefix for m in modes.values()} == {"sz", "kz", "vr", "gs"}


def test_gespraech_is_the_fast_room():
    m = get_mode("gespraech")
    assert m.checker == "off" and m.glossary is None
    assert set(m.guard.kinds) == {"number", "date", "time"} and m.guard.confirm_on == []
    assert m.pipeline.simultaneous == "auto" and m.pipeline.simultaneous_after_ms == 0
    assert all(get_mode(x).checker == "on" for x in ("sprechzimmer", "kanzlei", "verhandlungsraum"))


def test_sprechzimmer_matches_plan():
    m = get_mode("sprechzimmer")
    assert m.icon == "stethoscope"
    assert m.register == "formal"
    assert m.roles["a"].default_lang == "de"
    assert m.roles["b"].default_lang == "auto"
    assert set(m.guard.confirm_on) == {"dose", "allergy"}
    assert {"dose", "frequency", "allergy", "date", "number", "medication"} <= set(m.guard.kinds)
    assert m.summary.sections == ["medications", "allergies", "next_appointment", "warning_signs", "open_questions"]
    assert m.summary.takehome_for == "b" and m.summary.file_note_for == "a"
    assert m.buttons == ["off_record", "repeat_back", "human_interpreter", "end"]
    assert m.retention.auto_burn_minutes == 30
    assert m.label("a", "de") == "Ärztin/Arzt"
    assert m.label("b", "fr") == "Patient"  # falls back to English


def test_room_ids_map_to_modes():
    assert mode_for_room("sz-4821").id == "sprechzimmer"
    assert mode_for_room("kz-1170").id == "kanzlei"
    assert mode_for_room("vr-3305").id == "verhandlungsraum"
    rid = new_room_id("kanzlei")
    assert mode_for_room(rid).id == "kanzlei"
    with pytest.raises(ModeError):
        mode_for_room("xx-1234")
    with pytest.raises(ModeError):
        mode_for_room("not a room")


def test_glossaries_load_for_every_room():
    for m in load_modes().values():
        if m.glossary is None:  # Gespraech: everyday talk, no glossary
            continue
        g = Glossary.load(m.glossary)
        assert g.terms, m.id


def test_medical_glossary_forms():
    g = Glossary.load("glossaries/medical-de.yaml")
    met = next(t for t in g.medications() if t.de == "Metformin")
    assert "Metformin" in met.forms_for("en")  # keep: true
    assert "Метформин" in met.forms_for("ru")
    assert [t.de for t in g.find("Ich bin allergisch gegen Penicillin", "de", kinds=["medication"])] == ["Penicillin"]
    g.add_correction("Blutdruck", "BP", "en")
    bp = next(t for t in g.terms if t.de == "Blutdruck")
    assert bp.preferred("en") == "BP"


def _write(tmp_path: Path, data: dict, name: str = "badroom") -> Path:
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def _base() -> dict:
    return yaml.safe_load((ROOT / "modes" / "sprechzimmer.yaml").read_text(encoding="utf-8"))


def test_rejects_confirm_kind_not_in_kinds(tmp_path):
    d = _base() | {"id": "badroom"}
    d["guard"] = {"kinds": ["dose"], "confirm_on": ["allergy"]}
    with pytest.raises(ModeError, match="confirm_on"):
        load_mode(_write(tmp_path, d))


def test_rejects_unknown_key(tmp_path):
    d = _base() | {"id": "badroom", "colour": "red"}
    with pytest.raises(ModeError):
        load_mode(_write(tmp_path, d))


def test_rejects_unknown_button_and_missing_end(tmp_path):
    d = _base() | {"id": "badroom", "buttons": ["off_record", "teleport", "end"]}
    with pytest.raises(ModeError):
        load_mode(_write(tmp_path, d))
    d = _base() | {"id": "badroom", "buttons": ["off_record"]}
    with pytest.raises(ModeError, match="end"):
        load_mode(_write(tmp_path, d))


def test_rejects_id_filename_mismatch(tmp_path):
    with pytest.raises(ModeError, match="file name"):
        load_mode(_write(tmp_path, _base(), name="other"))


def test_rejects_missing_prompt_file(tmp_path):
    d = _base() | {"id": "badroom"}
    d["prompts"] = {**d["prompts"], "summary": "prompts/does-not-exist.md"}
    with pytest.raises(ModeError, match="not found"):
        load_mode(_write(tmp_path, d))


def test_rejects_bad_role_set(tmp_path):
    d = _base() | {"id": "badroom"}
    d["roles"] = {"a": d["roles"]["a"]}
    with pytest.raises(ModeError):
        load_mode(_write(tmp_path, d))
