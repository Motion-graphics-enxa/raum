"""raum-core session lifecycle: end -> summary (C-11), approve, burn (T-02), compliance (T-05), auto-burn
(T-03), sealed mode (T-07), admin."""

from __future__ import annotations

import json
import time

import pytest
from core_doubles import FakePublisher, ev, install_security, make_config, recv_until
from fastapi.testclient import TestClient

import raum.sessions as S
from raum.api.server import create_app
from raum.link import KEY_HEADER

pytestmark = pytest.mark.filterwarnings("ignore:The HMAC key")

ROOM = "sz-4821"
KEY = {KEY_HEADER: "dev-internal"}

# (role, lang, text, {target_lang: translation})
CONSULTATION = [
    ("b", "ar", "أتناول 500 ملغ ميتفورمين مرتين يوميا.", {"de": "Ich nehme zweimal täglich 500 mg Metformin."}),
    ("b", "ar", "أنا عندي حساسية من البنسلين.", {"de": "Ich bin allergisch gegen Penicillin."}),
    ("a", "de", "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.",
     {"ar": "تعالي مرة أخرى يوم الخميس 12 نوفمبر الساعة 9."}),
    ("a", "de", "Wenn Sie Atemnot bekommen, rufen Sie sofort die 112.",
     {"ar": "إذا شعرت بضيق في التنفس، اتصلي فورا بالرقم 112."}),
    ("b", "ar", "هل يمكنني شرب القهوة؟", {"de": "Darf ich Kaffee trinken?"}),
]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("RAUM_PROFILE", raising=False)
    install_security(monkeypatch, tmp_path)
    app = create_app(make_config(tmp_path))
    app.state.core.sessions.publisher = FakePublisher()
    app.state.core.tick_s = 0.05
    return app


@pytest.fixture
def core(app):
    return app.state.core


@pytest.fixture
def pub(core) -> FakePublisher:
    return core.sessions.publisher


@pytest.fixture
def client(app):
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c


def join(client, room=ROOM, role="b", lang="ar"):
    r = client.post("/api/join", json={"room": room, "role": role, "lang": lang, "device": "phone", "consent": True})
    assert r.status_code == 200, r.text
    return r.json()


def converse(client, a, b, turns=CONSULTATION):
    """Play a conversation through the agent link and wait until core has processed it."""
    ids = {"a": a["identity"], "b": b["identity"]}
    with client.websocket_connect(f"/api/events/{ROOM}") as sub, \
            client.websocket_connect(f"/api/internal/agent/{ROOM}", headers=KEY) as agent:
        agent.receive_json()
        for i, (role, lang, text, tr) in enumerate(turns, 1):
            utt = f"u-{i:04d}"
            agent.send_json(ev("transcript.final", ROOM, {"speaker": ids[role], "lang": lang, "text": text,
                                                          "asr_conf": 0.92}, utt=utt))
            for tl, tt in tr.items():
                agent.send_json(ev("translation.final", ROOM, {"target_lang": tl, "text": tt}, utt=utt))
            agent.send_json(ev("verify.result", ROOM, {"target_lang": next(iter(tr)), "similarity": 0.93,
                                                       "badge": "green"}, utt=utt))
        last = f"u-{len(turns):04d}"
        recv_until(sub, "verify.result", pred=lambda m: m["utt"] == last)


def test_end_builds_bilingual_summary(client, core, pub):
    a = join(client, role="a", lang="de")
    b = join(client, role="b", lang="ar")
    converse(client, a, b)
    with client.websocket_connect(f"/api/events/{ROOM}") as sub, \
            client.websocket_connect(f"/api/internal/agent/{ROOM}", headers=KEY) as agent:
        agent.receive_json()
        t0 = time.perf_counter()
        r = client.post(f"/api/rooms/{ROOM}/end")
        assert time.perf_counter() - t0 < 10  # C-11
        assert r.status_code == 200, r.text
        assert agent.receive_json()["control"]["action"] == "end"
        ready = recv_until(sub, "summary.ready")
        state = recv_until(sub, "session.state", pred=lambda m: m["data"]["status"] == "ended")
        assert state["data"]["session_id"] == a["session_id"]
    s = r.json()
    assert ready["data"] == s
    assert s["template"] == "sprechzimmer" and s["lang_a"] == "de" and s["lang_b"] == "ar"
    assert s["generated_ms"] < 10_000
    sec = {x["key"]: x for x in s["sections"]}
    assert list(sec) == ["medications", "allergies", "next_appointment", "warning_signs", "open_questions"]
    assert sec["medications"]["title"] == {"de": "Medikamente", "ar": "الأدوية", "en": "Medications"}

    def texts(key, lang):
        return [i["text"][lang] for i in sec[key]["items"]]

    assert any("Metformin" in t and "500 mg" in t for t in texts("medications", "de"))
    assert any("ميتفورمين" in t for t in texts("medications", "ar"))  # the session's own words, both languages
    assert any("Penicillin" in t for t in texts("allergies", "de"))
    assert any("Donnerstag" in t for t in texts("next_appointment", "de"))
    assert any("الخميس" in t for t in texts("next_appointment", "ar"))
    assert any("112" in t for t in texts("warning_signs", "de"))
    assert texts("open_questions", "de") == ["Darf ich Kaffee trinken?"]
    assert "Metformin" in s["file_note"]["de"]

    assert pub.of("summary.ready")[-1].data == s
    assert pub.of("session.state")[-1].data["status"] == "ended"
    assert client.get(f"/api/rooms/{ROOM}/summary").json() == s
    stored = core.sessions.rooms[ROOM].session.store.read_all()
    assert stored[-1]["type"] == "summary.ready"  # stored encrypted
    assert client.post(f"/api/rooms/{ROOM}/end").json() == s  # idempotent
    end = [e for e in core.sessions.audit.entries(session=a["session_id"]) if e["event"] == "session_end"]
    assert len(end) == 1 and end[0]["meta"]["reason"] == "end"

    r = client.post(f"/api/rooms/{ROOM}/summary/approve", json={"role": "a"})
    assert r.json()["approved_by"] == ["a"]
    r = client.post(f"/api/rooms/{ROOM}/control", json={"action": "approve_summary", "by": b["identity"]})
    assert r.json()["summary"]["approved_by"] == ["a", "b"]
    approvals = [e["meta"]["by"] for e in core.sessions.audit.entries(session=a["session_id"])
                 if e["event"] == "summary_approved"]
    assert approvals == ["a", "b"]
    assert pub.of("summary.ready")[-1].data["approved_by"] == ["a", "b"]
    assert client.post(f"/api/rooms/{ROOM}/summary/approve", json={"role": "observer"}).status_code == 400


def test_end_without_session(client):
    assert client.post(f"/api/rooms/{ROOM}/end").status_code == 404
    assert client.get(f"/api/rooms/{ROOM}/summary").status_code == 404


def test_burn_destroys_session(client, core, pub):
    a = join(client, role="a", lang="de")
    b = join(client, role="b", lang="ar")
    converse(client, a, b, CONSULTATION[:2])
    client.post(f"/api/rooms/{ROOM}/end")
    session = core.sessions.rooms[ROOM].session
    path = session.store.path
    assert path.exists()
    with client.websocket_connect(f"/api/events/{ROOM}") as sub, \
            client.websocket_connect(f"/api/internal/agent/{ROOM}", headers=KEY) as agent:
        agent.receive_json()
        r = client.post(f"/api/rooms/{ROOM}/burn")
        assert r.status_code == 200
        done = r.json()
        assert agent.receive_json()["control"]["action"] == "burn"
        assert recv_until(sub, "burn.done")["data"] == done
        recv_until(sub, "session.state", pred=lambda m: m["data"]["status"] == "burned")
    assert done["audit_head_hash"] == core.sessions.audit.head_hash()
    assert done["key_destroyed_at"] > 0
    assert not path.exists()
    assert session.store.burned
    with pytest.raises(S.SessionBurned):
        session.store.read_all()
    assert pub.of("burn.done")[-1].data == done
    entries = core.sessions.audit.entries(session=a["session_id"])
    assert entries[-1]["event"] == "key_destroyed"
    report = S.verify_chain(core.cfg.audit_path())
    assert report.ok and report.lines >= len(entries)

    # buffers cleared, summary gone, controls refused
    events = client.get(f"/api/rooms/{ROOM}/events").json()
    assert {e["type"] for e in events} <= {"burn.done", "session.state"}
    assert client.get(f"/api/rooms/{ROOM}/summary").status_code == 404
    assert client.post(f"/api/rooms/{ROOM}/control", json={"action": "off_record", "value": True}).status_code == 409
    assert client.post(f"/api/rooms/{ROOM}/burn").json() == done  # idempotent
    info = {x["room"]: x for x in client.get("/api/rooms").json()}[ROOM]
    assert info["status"] == "burned"

    # the next join starts a fresh session
    again = join(client)
    assert again["session_id"] != a["session_id"]
    assert client.get(f"/api/rooms/{ROOM}").json()["state"]["status"] == "live"
    # the old session's compliance export is still there (audit only, no content)
    old = client.get(f"/api/rooms/{ROOM}/compliance", params={"session": a["session_id"]}).json()
    assert old["status"] == "burned" and old["burn"]["ts"]


def test_burn_via_control(client, pub):
    join(client)
    r = client.post(f"/api/rooms/{ROOM}/control", json={"action": "burn", "by": "a"})
    assert r.status_code == 200 and r.json()["state"]["status"] == "burned"
    assert pub.types()[-2:] == ["burn.done", "session.state"]


def test_compliance_export(client, core):
    a = join(client, role="a", lang="de")
    b = join(client, role="b", lang="ar")
    client.post(f"/api/rooms/{ROOM}/control", json={"action": "off_record", "value": True, "by": "a"})
    client.post(f"/api/rooms/{ROOM}/control", json={"action": "off_record", "value": False, "by": "a"})
    client.post(f"/api/rooms/{ROOM}/control", json={"action": "confirm", "by": "b", "accepted": True,
                                                    "flag_id": "f-1"})
    converse(client, a, b, CONSULTATION[:1])
    client.post(f"/api/rooms/{ROOM}/end")
    client.post(f"/api/rooms/{ROOM}/summary/approve", json={"role": "a"})
    client.post(f"/api/rooms/{ROOM}/burn")
    c = client.get(f"/api/rooms/{ROOM}/compliance").json()
    assert c["session_id"] == a["session_id"] and c["mode"] == "sprechzimmer"
    assert [(x["role"], x["lang"], x["device"]) for x in c["consents"]] == [("a", "de", "phone"), ("b", "ar", "phone")]
    assert all(x["ts"] and x["consent_version"] for x in c["consents"])
    assert c["models"] == core.cfg.engine_names()
    assert len(c["off_record"]) == 1 and c["off_record"][0]["on"] and c["off_record"][0]["off"]
    assert c["confirmations"][0]["accepted"] is True
    assert c["summary_approvals"][0]["by"] == "a"
    assert c["ended"]["reason"] == "end" and c["burn"]["ts"]
    assert c["audit"]["chain_ok"] is True
    dump = json.dumps(c, ensure_ascii=False)
    for word in ("Metformin", "ميتفورمين", "Penicillin"):
        assert word not in dump  # no content ever
    assert client.get("/api/rooms/kz-1111/compliance").status_code == 404


def test_auto_burn_after_idle(client, core):
    join(client)
    join(client, room="vr-3305")
    later = time.monotonic() + 31 * 60  # sprechzimmer burns after 30 min, verhandlungsraum after 60
    burned = client.portal.call(core.sessions.check_idle, later)
    assert burned == [ROOM]
    s = core.sessions.rooms[ROOM].session
    assert s.status == "burned"
    events = [(e["event"], e["meta"].get("reason")) for e in core.sessions.audit.entries(session=s.id)]
    assert ("session_end", "auto_burn") in events and events[-1][0] == "key_destroyed"
    assert core.sessions.rooms["vr-3305"].session.status == "live"


def test_admin_privacy_seal_burn_all(client, core, pub):
    a = join(client, role="a", lang="de")
    client.post(f"/api/rooms/{ROOM}/presence", json={"identity": a["identity"], "rtt_ms": 12})
    join(client, room="kz-1170")
    st = client.get("/api/admin/status").json()
    assert {r["room"] for r in st["rooms"]} == {ROOM, "kz-1170"}
    assert st["devices"][0]["room"] == ROOM and st["devices"][0]["rtt_ms"] == 12
    assert st["models"] == core.cfg.engine_names()
    assert st["gpu"]["models_loaded"] and st["meter"]["third_party_bytes"] >= 0
    assert st["agents"] == {ROOM: False, "kz-1170": False}

    p = client.get("/api/privacy").json()
    assert p["sealed"] is False and p["third_party_bytes"] >= 0 and "tunnel_bytes" in p

    r = client.post("/api/admin/seal", json={"on": True}).json()
    assert r["sealed"] is True
    assert client.get(f"/api/rooms/{ROOM}").json()["state"]["sealed"] is True
    assert pub.of("session.state")[-1].data["sealed"] is True
    assert client.get("/api/privacy").json()["sealed"] is True
    client.post("/api/admin/seal", json={"on": False})
    box = [e["event"] for e in core.sessions.audit.entries() if e["session"] is None]
    assert box == ["sealed_on", "sealed_off"]

    r = client.post("/api/admin/burn-all").json()
    assert sorted(r["burned"]) == ["kz-1170", ROOM] and r["count"] == 2
    assert all(x.session.status == "burned" for x in core.sessions.rooms.values())
    assert core.sessions.audit.entries()[-1]["event"] == "burn_all"
    assert client.get("/api/health").json()["ok"] is True
