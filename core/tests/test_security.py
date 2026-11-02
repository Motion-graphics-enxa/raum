"""Session security (T-01..T-05, plan §6.10): encrypted store, burn, audit chain, CLI."""

from __future__ import annotations

import json
import struct
import threading
from pathlib import Path

import pytest

import raum.cli
from raum.events import BurnDone
from raum.security.audit import (
    FORBIDDEN_META_KEYS,
    GENESIS,
    AuditLog,
    canonical_json,
    entry_hash,
    verify_chain,
)
from raum.security.burn import burn_session
from raum.security.crypto import SessionBurned, SessionStore, SessionTampered

RECORDS = [
    {"type": "transcript.final", "data": {"text": "Ich nehme zweimal täglich 500 mg Metformin.", "lang": "de"}},
    {"type": "translation.final", "data": {"text": "أتناول 500 ملغ من الميتفورمين مرتين يوميًا.", "lang": "ar"}},
    {"type": "summary.ready", "data": {"sections": [{"key": "allergies", "items": ["Penicillin"]}]}},
]


def frames(path: Path) -> list[tuple[bytes, bytes]]:
    raw = path.read_bytes()
    out, pos = [], 0
    while pos < len(raw):
        (n,) = struct.unpack_from(">I", raw, pos)
        out.append((raw[pos + 4:pos + 16], raw[pos + 16:pos + 16 + n]))
        pos += 16 + n
    assert pos == len(raw)
    return out


# ---------------------------------------------------------------- SessionStore


def test_roundtrip_and_no_plaintext_on_disk(tmp_path):
    store = SessionStore("s-1", tmp_path / "sessions")  # directory is created
    assert store.path == tmp_path / "sessions" / "s-1.bin" and store.read_all() == []
    for r in RECORDS:
        store.append(r)
    assert store.read_all() == RECORDS
    assert store.record_count == 3
    raw = store.path.read_bytes()
    for needle in ("Metformin", "ميتفورمين", "Penicillin", "transcript", "500 mg", "text"):
        assert needle.encode() not in raw
    assert len(frames(store.path)) == 3
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert "s-1" in repr(store) and bytes(store._key).hex() not in repr(store)


def test_key_is_256_bit_bytearray_and_nonces_unique(tmp_path):
    store = SessionStore("s-2", tmp_path)
    assert isinstance(store._key, bytearray) and len(store._key) == 32 and any(store._key)
    for i in range(200):
        store.append({"i": i})
    nonces = [n for n, _ in frames(store.path)]
    assert len(nonces) == 200 and all(len(n) == 12 for n in nonces) and len(set(nonces)) == 200
    other = SessionStore("s-3", tmp_path)
    assert bytes(other._key) != bytes(store._key)


def test_without_the_key_the_file_is_unreadable(tmp_path):
    a = SessionStore("same", tmp_path / "a")
    a.append(RECORDS[0])
    b = SessionStore("same", tmp_path / "b")  # same session id, different key
    b.path.write_bytes(a.path.read_bytes())
    with pytest.raises(SessionTampered):
        b.read_all()


@pytest.mark.parametrize("attack", ["flip", "swap", "truncate", "foreign", "drop_first"])
def test_tampered_records_raise(tmp_path, attack):
    store = SessionStore("victim", tmp_path)
    for r in RECORDS:
        store.append(r)
    raw = bytearray(store.path.read_bytes())
    fr = frames(store.path)
    first = 16 + len(fr[0][1])
    second = first + 16 + len(fr[1][1])
    if attack == "flip":
        raw[first + 20] ^= 0x01
    elif attack == "swap":
        raw = raw[first:second] + raw[:first] + raw[second:]
    elif attack == "truncate":
        raw = raw[:-5]
    elif attack == "drop_first":
        raw = raw[first:]
    else:  # a record from another session (same key would not help: AAD binds the session id)
        other = SessionStore("other", tmp_path / "o")
        other._key[:] = store._key
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        other._aead = AESGCM(other._key)
        other.append(RECORDS[0])
        raw = bytearray(other.path.read_bytes()) + raw[first:]
    store.path.write_bytes(bytes(raw))
    with pytest.raises(SessionTampered):
        store.read_all()


def test_stale_file_from_dead_process_is_replaced(tmp_path):
    (tmp_path / "old.bin").write_bytes(b"\x00\x00\x00\x20" + b"x" * 44)
    store = SessionStore("old", tmp_path)
    assert not store.path.exists()
    store.append({"a": 1})
    assert store.read_all() == [{"a": 1}]


def test_invalid_session_id(tmp_path):
    for bad in ("", "..", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            SessionStore(bad, tmp_path)


def test_burn_zeroes_key_and_deletes_file(tmp_path):
    store = SessionStore("s-burn", tmp_path)
    store.append(RECORDS[0])
    key = store._key
    assert store.path.exists() and any(key)
    store.burn()
    assert store._key is key and key == bytearray(32)  # zeroed in place
    assert store._aead is None
    assert store.burned and not store.path.exists()
    with pytest.raises(SessionBurned):
        store.append({"x": 1})
    with pytest.raises(SessionBurned):
        store.read_all()
    store.burn()  # idempotent
    assert not store.path.exists()


def test_burn_session_writes_key_destroyed(tmp_path):
    audit = AuditLog(tmp_path / "audit" / "audit.jsonl")  # parent created
    store = SessionStore("s-9", tmp_path / "sessions")
    audit.append("session_start", "s-9", {"room": "sz-4821", "mode": "sprechzimmer"})
    store.append(RECORDS[0])
    store.append(RECORDS[1])
    done = burn_session(store, audit)
    assert isinstance(done, BurnDone)
    assert store._key == bytearray(32) and not store.path.exists()
    last = audit.entries()[-1]
    assert last["event"] == "key_destroyed" and last["session"] == "s-9"
    assert last["meta"] == {"records": 2, "file_deleted": True}
    assert done.audit_head_hash == last["hash"] == audit.head_hash()
    assert abs(done.key_destroyed_at - last["ts"]) < 5_000
    assert verify_chain(audit.path).ok


# ---------------------------------------------------------------- AuditLog

CORE_META = {"room": "sz-1", "mode": "sprechzimmer", "profile": "mock", "reason": "join",
             "models": {"asr": "whisper", "translate": "ollama:gemma4"}, "role": "a", "lang": "de",
             "device": "phone", "consent_version": "1", "by": "a", "accepted": True, "flag_id": None,
             "requested": True, "duration_ms": 12, "generated_ms": 3.5, "applied": None, "count": 2}


def make_log(tmp_path: Path, n: int = 5) -> AuditLog:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("session_start", "s-1", {"room": "sz-1", "mode": "sprechzimmer"})
    for i in range(n - 2):
        log.append("consent", "s-1", {"role": "b", "lang": "ar", "i": i})
    log.append("sealed_on", None, {"applied": True})
    return log


def test_chain_format_and_verify(tmp_path):
    log = make_log(tmp_path, 5)
    lines = [json.loads(x) for x in log.path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 5 and lines[0]["prev"] == GENESIS
    for prev, e in zip([GENESIS] + [x["hash"] for x in lines], lines, strict=False):
        assert set(e) == {"ts", "session", "event", "meta", "prev", "hash"}
        assert e["prev"] == prev
        body = {k: v for k, v in e.items() if k != "hash"}
        import hashlib

        assert e["hash"] == hashlib.sha256((prev + canonical_json(body)).encode()).hexdigest()
        assert e["hash"] == entry_hash(prev, e)
    assert log.head_hash() == lines[-1]["hash"]
    assert verify_chain(log.path).ok and verify_chain(log.path).lines == 5
    assert [e["event"] for e in log.entries("s-1")] == ["session_start", "consent", "consent", "consent"]
    assert len(log.entries()) == 5


def test_core_meta_keys_accepted_content_keys_rejected(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("session_start", "s-1", CORE_META)
    log.append("key_destroyed", "s-1", {"records": 3, "file_deleted": True})
    log.append("burn_all", None, None)
    for key in sorted(FORBIDDEN_META_KEYS | {"Text", "TRANSCRIPT"}):
        with pytest.raises(ValueError):
            log.append("consent", "s-1", {key: "Ich bin allergisch gegen Penicillin."})
    with pytest.raises(ValueError):
        log.append("consent", "s-1", {"reason": "x" * 2000})  # a sentence smuggled into a value
    with pytest.raises(ValueError):
        log.append("consent", "s-1", {"bad": object()})
    assert len(log.entries()) == 3 and verify_chain(log.path).ok


def test_reopen_continues_chain(tmp_path):
    log = make_log(tmp_path, 3)
    head = log.head_hash()
    again = AuditLog(log.path)
    e = again.append("session_end", "s-1", {"reason": "end"})
    assert e["prev"] == head
    log.append("burn_all", None, {"count": 1})  # the old object sees the new head too
    assert verify_chain(log.path).ok and verify_chain(log.path).lines == 5


def test_partial_last_line_is_reported_and_chain_resumes(tmp_path):
    log = make_log(tmp_path, 3)
    with log.path.open("ab") as f:
        f.write(b'{"ts": 1, "sess')  # crash mid-write
    log.append("session_end", "s-1", {})
    rep = verify_chain(log.path)
    assert not rep.ok and rep.first_bad == 4 and rep.reason == "invalid JSON"
    assert log.entries()[-1]["event"] == "session_end"


def edit(path: Path, fn) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(fn(lines)) + "\n", encoding="utf-8")


def test_edit_one_line_breaks_at_that_line(tmp_path):
    log = make_log(tmp_path, 6)

    def change(lines):
        e = json.loads(lines[2])
        e["meta"]["lang"] = "tr"
        lines[2] = json.dumps(e)
        return lines

    edit(log.path, change)
    rep = verify_chain(log.path)
    assert not rep.ok and rep.first_bad == 3 and "hash mismatch" in rep.reason and rep.lines == 6


def test_edit_and_rehash_one_line_breaks_the_next(tmp_path):
    log = make_log(tmp_path, 6)

    def forge(lines):
        e = json.loads(lines[2])
        e["meta"]["lang"] = "tr"
        e["hash"] = entry_hash(e["prev"], e)
        lines[2] = canonical_json(e)
        return lines

    edit(log.path, forge)
    rep = verify_chain(log.path)
    assert not rep.ok and rep.first_bad == 4 and "prev" in rep.reason


@pytest.mark.parametrize(("op", "line"), [("delete", 3), ("reorder", 2), ("insert", 3), ("json", 4),
                                          ("delete_first", 1), ("field", 5), ("blank", 2)])
def test_tampering_detected(tmp_path, op, line):
    log = make_log(tmp_path, 6)

    def mutate(lines):
        if op == "delete":
            del lines[2]
        elif op == "delete_first":
            del lines[0]
        elif op == "reorder":
            lines[1], lines[2] = lines[2], lines[1]
        elif op == "insert":
            fake = {"ts": 1, "session": "s-1", "event": "consent", "meta": {}, "prev": GENESIS}
            fake["hash"] = entry_hash(GENESIS, fake)
            lines.insert(2, canonical_json(fake))
        elif op == "json":
            lines[3] = lines[3][:-3]
        elif op == "field":
            e = json.loads(lines[4])
            e["extra"] = 1
            lines[4] = json.dumps(e)
        elif op == "blank":
            lines.insert(1, "")
        return lines

    edit(log.path, mutate)
    rep = verify_chain(log.path)
    assert not rep.ok and rep.first_bad == line, rep


def test_missing_log_verifies_empty(tmp_path):
    rep = verify_chain(tmp_path / "nope.jsonl")
    assert rep.ok and rep.lines == 0
    assert AuditLog(tmp_path / "x" / "a.jsonl").head_hash() == GENESIS


def test_concurrent_appends_keep_one_chain(tmp_path):
    a = AuditLog(tmp_path / "audit.jsonl")
    b = AuditLog(tmp_path / "audit.jsonl")  # second object on the same file (flock + head from file)

    def work(log: AuditLog, n: int) -> None:
        for i in range(n):
            log.append("consent", f"s-{n}", {"i": i})

    threads = [threading.Thread(target=work, args=(a if i % 2 else b, 25 + i)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rep = verify_chain(a.path)
    assert rep.ok and rep.lines == sum(25 + i for i in range(8))


# ---------------------------------------------------------------- CLI


def test_cli_verify_ok_and_broken(tmp_path, capsys):
    log = make_log(tmp_path, 4)
    assert raum.cli.main(["audit", "verify", "--path", str(log.path)]) == 0
    out = capsys.readouterr().out.strip()
    assert out == f"ok: 4 entries, head {log.head_hash()}"

    head = log.head_hash()
    log.append("session_end", "s-1", {})
    assert raum.cli.main(["audit", "verify", "--path", str(log.path), "--expect-head", head]) == 0
    capsys.readouterr()
    edit(log.path, lambda lines: lines[:-2])  # tail cut off: the chain alone still verifies...
    assert raum.cli.main(["audit", "verify", "--path", str(log.path)]) == 0
    capsys.readouterr()
    assert raum.cli.main(["audit", "verify", "--path", str(log.path), "--expect-head", head]) == 1  # ...the head does not
    assert "BROKEN" in capsys.readouterr().out

    def change(lines):
        e = json.loads(lines[1])
        e["event"] = "summary_approved"
        lines[1] = json.dumps(e)
        return lines

    edit(log.path, change)
    assert raum.cli.main(["audit", "verify", "--path", str(log.path)]) == 1
    assert capsys.readouterr().out.strip() == "BROKEN at line 2: hash mismatch (line edited)"


def test_cli_missing_file_and_usage(tmp_path, capsys):
    assert raum.cli.main(["audit", "verify", "--path", str(tmp_path / "none.jsonl")]) == 1
    assert "no audit log" in capsys.readouterr().err
    assert raum.cli.main(["audit"]) == 2
    assert raum.cli.main(["audit", "show"]) == 2  # --session is required
    capsys.readouterr()


def test_cli_default_path_from_config(tmp_path, capsys, monkeypatch):
    import raum.config

    log = make_log(tmp_path, 3)

    class Cfg:
        def audit_path(self) -> Path:
            return log.path

    monkeypatch.setattr(raum.config, "get_config", lambda: Cfg())
    assert raum.cli.main(["audit", "verify"]) == 0
    assert "ok: 3 entries" in capsys.readouterr().out


def test_cli_show_compliance_export(tmp_path, capsys):
    log = AuditLog(tmp_path / "audit.jsonl")
    store = SessionStore("s-77", tmp_path / "sessions")
    log.append("session_start", "s-77", {"room": "sz-4821", "mode": "sprechzimmer", "profile": "mock",
                                         "reason": "join"})
    log.append("models", "s-77", {"models": {"asr": "whisper", "translate": "gemma4"}})
    log.append("consent", "s-77", {"role": "b", "lang": "ar", "device": "phone", "consent_version": "1"})
    log.append("consent", "s-other", {"role": "a", "lang": "de"})
    log.append("off_record_on", "s-77", {"by": "a"})
    log.append("off_record_off", "s-77", {"by": "a"})
    log.append("fact_noted", "s-77", {"flag_id": "f-0", "kinds": ["allergy"], "spoken": False, "why": []})
    log.append("fact_asked", "s-77", {"flag_id": "f-1", "kinds": ["dose"], "spoken": True, "why": ["check:amber"]})
    log.append("confirm", "s-77", {"by": "b", "accepted": True, "flag_id": "f-1", "via": "voice"})
    log.append("session_end", "s-77", {"reason": "end", "duration_ms": 1234})
    store.append({"type": "transcript.final"})
    burn_session(store, log)

    assert raum.cli.main(["audit", "show", "--session", "s-77", "--path", str(log.path)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["kind"] == "raum.compliance.v1" and doc["session_id"] == "s-77"
    assert doc["room"] == "sz-4821" and doc["mode"] == "sprechzimmer"
    assert doc["models"] == {"asr": "whisper", "translate": "gemma4"}
    assert doc["consents"][0]["lang"] == "ar" and len(doc["consents"]) == 1
    assert doc["off_record"][0]["on"] and doc["off_record"][0]["off"]
    assert doc["confirmations"] == [{"ts": doc["confirmations"][0]["ts"], "by": "b", "accepted": True,
                                     "flag_id": "f-1", "via": "voice"}]
    # checked facts (raum.pipeline.readback): no content, only how each was handled
    assert [(f["flag_id"], f["kinds"], f["status"], f["spoken"]) for f in doc["checked_facts"]] == [
        ("f-0", ["allergy"], "noted", False), ("f-1", ["dose"], "confirmed", True)]
    assert doc["ended"]["duration_ms"] == 1234
    assert doc["burn"]["records"] == 1 and doc["burn"]["file_deleted"] is True
    assert doc["audit"]["chain_ok"] is True and doc["audit"]["head_hash"] == log.head_hash()
    assert all(e["session"] == "s-77" for e in doc["entries"]) and len(doc["entries"]) == 10
    assert "Penicillin" not in json.dumps(doc)

    assert raum.cli.main(["audit", "show", "--session", "nope", "--path", str(log.path)]) == 1
    assert "no audit entries" in capsys.readouterr().err
