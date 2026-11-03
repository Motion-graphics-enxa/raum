"""raum-core API: join (C-03), tokens, path (G-03), rooms, control, presence, agent link, event WS."""

from __future__ import annotations

import json
import re
import uuid

import pytest
from core_doubles import FakePublisher, ev, install_security, make_config, recv_until
from fastapi.testclient import TestClient
from livekit import api
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from raum.api.join import classify_ip, detect_path, public_livekit_url
from raum.api.server import create_app
from raum.link import KEY_HEADER

pytestmark = pytest.mark.filterwarnings("ignore:The HMAC key")  # livekit --dev secret

ROOM = "sz-4821"
KEY = {KEY_HEADER: "dev-internal"}


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


def join(client, room=ROOM, role="b", lang="ar", **kw):
    body = {"room": room, "role": role, "lang": lang, "device": "phone", "consent": True, **kw}
    r = client.post("/api/join", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def audit_events(core, session_id=None):
    return [e["event"] for e in core.sessions.audit.entries(session=session_id)]


# ---------------------------------------------------------------- join


def test_join_requires_consent(client):
    for body in ({"room": ROOM, "role": "b", "lang": "ar", "consent": False},
                 {"room": ROOM, "role": "b", "lang": "ar"}):
        r = client.post("/api/join", json=body)
        assert r.status_code == 400
        assert "consent" in r.json()["detail"]
    assert client.get("/api/rooms").json() == []  # nothing started without consent


def test_join_token_metadata_and_grants(client):
    j = join(client, lang="ar-EG")
    assert re.fullmatch(r"b-[a-z0-9]{4}", j["identity"])
    assert j["room"] == ROOM and j["role"] == "b" and j["lang"] == "ar" and j["path"] == "direct"
    assert j["mode"]["id"] == "sprechzimmer"
    uuid.UUID(j["session_id"])
    claims = api.TokenVerifier("devkey", "secret").verify(j["token"])
    assert claims.identity == j["identity"]
    meta = {"role": "b", "lang": "ar", "device": "phone", "path": "direct", "audio_out": "speaker", "voice": "own"}
    assert json.loads(claims.metadata) == meta
    assert claims.attributes == meta
    v = claims.video
    assert v.room_join and v.room == ROOM and v.can_publish and v.can_subscribe and v.can_publish_data

    obs = join(client, role="observer", lang="de")
    oc = api.TokenVerifier("devkey", "secret").verify(obs["token"])
    assert oc.video.can_publish is False and oc.video.can_subscribe and oc.video.can_publish_data


def test_join_voice_choice_reaches_token_and_consent_audit(client, core):
    j = join(client, voice="standard")
    claims = api.TokenVerifier("devkey", "secret").verify(j["token"])
    assert json.loads(claims.metadata)["voice"] == "standard" and claims.attributes["voice"] == "standard"
    consent = [e["meta"] for e in core.sessions.audit.entries(session=j["session_id"]) if e["event"] == "consent"]
    assert consent[-1]["voice"] == "standard"
    assert client.get(f"/api/rooms/{ROOM}/compliance").json()["consents"][-1]["voice"] == "standard"
    r = client.post("/api/join", json={"room": ROOM, "role": "b", "lang": "ar", "consent": True, "voice": "clone"})
    assert r.status_code == 422


def test_join_lang_defaults_from_mode(client):
    assert join(client, role="a", lang="auto")["lang"] == "de"  # sprechzimmer a: default_lang de
    assert join(client, role="b", lang="")["lang"] == "en"  # b: auto -> en


def test_join_unknown_room_404(client):
    for room in ("xx-1234", "hello", "SZ-1"):
        r = client.post("/api/join", json={"room": room, "role": "b", "lang": "ar", "consent": True})
        assert r.status_code == 404, room


def test_join_one_session_per_room_and_audit(client, core):
    a = join(client, role="a", lang="de")
    b = join(client, role="b", lang="ar")
    assert a["session_id"] == b["session_id"]
    assert a["identity"] != b["identity"]
    entries = core.sessions.audit.entries(session=a["session_id"])
    assert [e["event"] for e in entries] == ["session_start", "models", "consent", "consent"]
    assert entries[1]["meta"]["models"] == core.cfg.engine_names()
    consents = [e["meta"] for e in entries if e["event"] == "consent"]
    assert {(c["role"], c["lang"], c["device"]) for c in consents} == {("a", "de", "phone"), ("b", "ar", "phone")}
    assert all(c["consent_version"] for c in consents)
    other = join(client, room="kz-1170", role="b", lang="tr")
    assert other["session_id"] != a["session_id"] and other["mode"]["id"] == "kanzlei"


def _req(peer: str, headers: dict[str, str] | None = None, scheme: str = "http") -> Request:
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "POST", "scheme": scheme, "path": "/api/join", "query_string": b"",
                    "headers": hdrs, "client": (peer, 1234), "server": ("127.0.0.1", 8000)})


@pytest.mark.parametrize("peer,headers,path", [
    ("192.168.1.20", {}, "direct"),
    ("10.0.0.7", {}, "direct"),
    ("127.0.0.1", {}, "direct"),
    ("100.101.102.103", {}, "tunnel"),
    ("8.8.8.8", {}, "relayed"),
    ("127.0.0.1", {"X-Forwarded-For": "100.64.1.2"}, "tunnel"),  # Caddy on the box
    ("127.0.0.1", {"X-Forwarded-For": "1.2.3.4, 100.64.1.2"}, "tunnel"),  # right-most = added by our proxy
    ("127.0.0.1", {"X-Forwarded-For": "8.8.4.4"}, "relayed"),
    ("192.168.1.5", {"X-Forwarded-For": "100.64.1.2"}, "direct"),  # not a loopback proxy: ignored
    ("8.8.8.8", {"X-Forwarded-For": "192.168.1.2"}, "relayed"),
])
def test_path_detection(peer, headers, path):
    assert detect_path(_req(peer, headers)) == path


def test_userspace_tunnel_arrives_from_the_box_itself(monkeypatch):
    """A-04: a userspace tailscaled (netstack) subnet router hands remote clients over with the box's own
    address as the source; that address (config or RAUM_TUNNEL_SELF_IPS) means "tunnel"."""
    from raum.config import RaumConfig

    box = "10.24.249.204"
    assert classify_ip(box) == "direct"
    assert classify_ip(box, {box}) == "tunnel"
    assert classify_ip("10.24.249.17", {box}) == "direct"  # a phone on the LAN
    assert classify_ip("::ffff:10.24.249.204", {box}) == "tunnel"
    cfg = RaumConfig()
    monkeypatch.delenv("RAUM_TUNNEL_SELF_IPS", raising=False)
    assert detect_path(_req(box, {}), cfg) == "direct"
    cfg.network.tunnel_self_ips = [box]
    assert detect_path(_req(box, {}), cfg) == "tunnel"
    assert detect_path(_req("127.0.0.1", {"X-Forwarded-For": box}), cfg) == "tunnel"  # through Caddy
    cfg.network.tunnel_self_ips = []
    monkeypatch.setenv("RAUM_TUNNEL_SELF_IPS", f" {box} , ")
    assert detect_path(_req(box, {}), cfg) == "tunnel"


def test_classify_ip_edges():
    assert classify_ip("fd7a:115c:a1e0::1234") == "tunnel"
    assert classify_ip("::ffff:192.168.1.9") == "direct"
    assert classify_ip("::1") == "direct"
    assert classify_ip("2001:4860:4860::8888") == "relayed"
    assert classify_ip("testclient") == "direct"
    assert classify_ip(None) == "direct"


def test_join_path_end_to_end(app):
    with TestClient(app, client=("100.80.1.2", 4000)) as c:
        j = join(c)
    assert j["path"] == "tunnel"
    claims = api.TokenVerifier("devkey", "secret").verify(j["token"])
    assert json.loads(claims.metadata)["path"] == "tunnel" and claims.attributes["path"] == "tunnel"


def test_livekit_url(core):
    cfg = core.cfg
    assert public_livekit_url(_req("192.168.1.30", {"Host": "192.168.1.20:5173"}), cfg) == "ws://192.168.1.20:5173"
    # Vite's proxy rewrites Host (changeOrigin); the browser's Origin wins
    assert public_livekit_url(_req("127.0.0.1", {"Host": "127.0.0.1:8000", "Origin": "https://192.168.1.20:5173"}),
                              cfg) == "wss://192.168.1.20:5173"
    # Caddy: forwarded headers from a loopback proxy
    assert public_livekit_url(_req("127.0.0.1", {"Host": "127.0.0.1:8000", "X-Forwarded-Proto": "https",
                                                 "X-Forwarded-Host": "raum.tail1234.ts.net"}), cfg) \
        == "wss://raum.tail1234.ts.net"
    # ... but not from anyone else
    assert public_livekit_url(_req("192.168.1.30", {"Host": "box:8000", "X-Forwarded-Proto": "https"}), cfg) \
        == "ws://box:8000"
    cfg.network.livekit_url = "wss://box.local:7443"
    assert public_livekit_url(_req("192.168.1.30", {"Host": "x"}), cfg) == "wss://box.local:7443"


def test_join_response_livekit_url(client):
    j = client.post("/api/join", headers={"Origin": "https://192.168.1.20:5173"},
                    json={"room": ROOM, "role": "b", "lang": "ar", "consent": True}).json()
    assert j["livekit_url"] == "wss://192.168.1.20:5173"


# ---------------------------------------------------------------- rooms


def test_modes_and_rooms(client):
    modes = client.get("/api/modes").json()
    assert {m["id"] for m in modes} == {"sprechzimmer", "kanzlei", "verhandlungsraum", "gespraech"}
    r = client.post("/api/rooms", json={"mode": "kanzlei"})
    assert r.status_code == 200
    room = r.json()["room"]
    assert re.fullmatch(r"kz-\d{4}", room)
    listed = {x["room"]: x for x in client.get("/api/rooms").json()}
    assert listed[room]["status"] == "idle" and listed[room]["mode"] == "kanzlei"
    assert listed[room]["agent_connected"] is False
    assert client.post("/api/rooms", json={"mode": "toaster"}).status_code == 404

    info = client.get(f"/api/rooms/{ROOM}").json()
    assert info["mode"]["id"] == "sprechzimmer" and info["state"] is None
    j = join(client)
    info = client.get(f"/api/rooms/{ROOM}").json()
    assert info["state"]["session_id"] == j["session_id"] and info["state"]["status"] == "live"
    assert client.get("/api/rooms/xx-0000").status_code == 404


def test_control_off_record(client, core, pub):
    j = join(client)
    sid = j["session_id"]
    r = client.post(f"/api/rooms/{ROOM}/control", json={"action": "off_record", "value": True, "by": j["identity"]})
    assert r.status_code == 200
    assert r.json()["state"]["off_record"] is True
    states = pub.of("session.state")
    assert states[-1].data["off_record"] is True
    on = [e for e in core.sessions.audit.entries(session=sid) if e["event"] == "off_record_on"]
    assert on and on[0]["meta"]["by"] == "b"  # identity resolved to the role
    client.post(f"/api/rooms/{ROOM}/control", json={"action": "off_record", "value": False, "by": "a"})
    assert audit_events(core, sid)[-1] == "off_record_off"
    assert pub.of("session.state")[-1].data["off_record"] is False


def test_control_confirm_and_human_interpreter(client, core, pub):
    j = join(client)
    sid = j["session_id"]
    r = client.post(f"/api/rooms/{ROOM}/control",
                    json={"action": "confirm", "by": "b", "flag_id": "f-1", "accepted": True, "utt": "u-0001"})
    assert r.status_code == 200
    c = pub.of("confirm")[-1]
    assert c.data == {"by": "b", "accepted": True, "flag_id": "f-1", "via": "button", "answer_utt": None}
    assert c.utt == "u-0001"
    entry = core.sessions.audit.entries(session=sid)[-1]
    assert entry["event"] == "confirm" and entry["meta"]["accepted"] is True and entry["meta"]["via"] == "button"
    stored = core.sessions.rooms[ROOM].session.store.read_all()
    assert [x["type"] for x in stored] == ["confirm"]

    r = client.post(f"/api/rooms/{ROOM}/control", json={"action": "human_interpreter", "by": "a"})
    assert r.json()["state"]["human_interpreter_requested"] is True
    assert audit_events(core, sid)[-1] == "human_interpreter"


def test_control_unknown_room_and_mute(client, core):
    assert client.post("/api/rooms/sz-9999/control", json={"action": "off_record"}).status_code == 404
    assert client.post(f"/api/rooms/{ROOM}/control", json={"action": "teleport"}).status_code == 422
    j = join(client)
    r = client.post(f"/api/rooms/{ROOM}/control", json={"action": "mute", "by": j["identity"], "value": True})
    parts = r.json()["state"]["participants"]
    assert parts[0]["id"] == j["identity"] and parts[0]["muted"] is True


def test_presence(client, pub):
    j = join(client)
    r = client.post(f"/api/rooms/{ROOM}/presence", json={"identity": j["identity"], "rtt_ms": 42})
    assert r.status_code == 200 and r.json()["rtt_ms"] == 42 and r.json()["path"] == "direct"
    state = client.get(f"/api/rooms/{ROOM}").json()["state"]
    assert state["participants"][0]["rtt_ms"] == 42
    assert pub.of("session.state")[-1].data["participants"][0]["id"] == j["identity"]
    assert client.post(f"/api/rooms/{ROOM}/presence", json={"identity": "b-zzzz", "rtt_ms": 1}).status_code == 404


# ---------------------------------------------------------------- agent link + event WS


def test_agent_link_rejects_bad_key(client):
    with pytest.raises(WebSocketDisconnect) as e, \
            client.websocket_connect(f"/api/internal/agent/{ROOM}", headers={KEY_HEADER: "nope"}):
        pass
    assert e.value.code == 4401
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"/api/internal/agent/{ROOM}"):
        pass
    with pytest.raises(WebSocketDisconnect) as e, client.websocket_connect("/api/internal/agent/zz-1", headers=KEY):
        pass
    assert e.value.code == 4404


def test_agent_link_persist_fanout_control(client, core):
    a = join(client, role="a", lang="de")
    b = join(client, role="b", lang="ar")
    with client.websocket_connect(f"/api/events/{ROOM}") as sub, \
            client.websocket_connect(f"/api/internal/agent/{ROOM}", headers=KEY) as agent:
        first = agent.receive_json()
        assert first["kind"] == "state" and first["state"]["session_id"] == a["session_id"]
        agent.send_json({"kind": "hello", "room": ROOM, "agent": "w-1"})
        agent.send_json(ev("participant.joined", ROOM, {"id": b["identity"], "role": "b", "lang": "ar",
                                                        "device": "phone", "path": "tunnel"}))
        assert recv_until(sub, "participant.joined")["data"]["id"] == b["identity"]
        st = recv_until(sub, "session.state", pred=lambda m: m["data"]["participants"])
        assert st["data"]["participants"][0]["path"] == "tunnel"
        assert agent.receive_json()["kind"] == "state"  # state change pushed to the agent

        agent.send_json(ev("utterance.start", ROOM, {"speaker": b["identity"], "lang": "ar"}, utt="u-0001"))
        agent.send_json(ev("transcript.final", ROOM, {"speaker": b["identity"], "lang": "ar",
                                                      "text": "أتناول 500 ملغ ميتفورمين", "asr_conf": 0.93},
                           utt="u-0001"))
        agent.send_json(ev("translation.final", ROOM, {"target_lang": "de", "text": "Ich nehme 500 mg Metformin."},
                           utt="u-0001"))
        agent.send_json({"kind": "event", "event": {"v": 1, "type": "flag", "room": ROOM, "data": {}}})  # invalid
        agent.send_json(ev("translation.final", "sz-0000", {"target_lang": "de", "text": "x"}))  # other room
        got = recv_until(sub, "translation.final")
        assert got["data"]["text"] == "Ich nehme 500 mg Metformin." and got["utt"] == "u-0001"

        stored = core.sessions.rooms[ROOM].session.store.read_all()
        assert [x["type"] for x in stored] == ["transcript.final", "translation.final"]  # content only, encrypted
        path = core.sessions.rooms[ROOM].session.store.path
        assert path.exists() and b"Metformin" not in path.read_bytes()

        r = client.post(f"/api/rooms/{ROOM}/control", json={"action": "repeat_back", "by": "a"})
        assert r.json()["forwarded"] is True
        msg = agent.receive_json()
        assert msg == {"kind": "control", "control": {"action": "repeat_back", "by": "a", "value": None,
                                                      "flag_id": None, "accepted": None, "utt": None, "text": None}}
        client.post(f"/api/rooms/{ROOM}/control", json={"action": "off_record", "value": True})
        assert agent.receive_json()["control"] == {"action": "off_record", "by": None, "value": True, "flag_id": None,
                                                   "accepted": None, "utt": None, "text": None}
        st = agent.receive_json()
        assert st["kind"] == "state" and st["state"]["off_record"] is True
        # while off the record nothing is stored
        agent.send_json(ev("transcript.final", ROOM, {"speaker": b["identity"], "lang": "ar", "text": "سر",
                                                      "asr_conf": 0.9}, utt="u-0002"))
        recv_until(sub, "transcript.final", pred=lambda m: m["utt"] == "u-0002")
        assert len(core.sessions.rooms[ROOM].session.store.read_all()) == 2

        assert client.get("/api/rooms").json()[0]["agent_connected"] is True
        agent.send_json(ev("participant.left", ROOM, {"id": b["identity"]}))
        recv_until(sub, "session.state", pred=lambda m: not m["data"]["participants"])
    assert client.get("/api/rooms").json()[0]["agent_connected"] is False


def test_agent_without_session_starts_one(client, core):
    with client.websocket_connect("/api/internal/agent/vr-3305", headers=KEY) as agent:
        st = agent.receive_json()["state"]
        assert st["mode"] == "verhandlungsraum" and st["session_id"]
    assert audit_events(core, st["session_id"])[:2] == ["session_start", "models"]


def test_events_ws_replay_and_backlog(client):
    b = join(client)
    with client.websocket_connect(f"/api/internal/agent/{ROOM}", headers=KEY) as agent:
        agent.receive_json()
        for i, text in enumerate(["eins", "zwei", "drei"], 1):
            agent.send_json(ev("transcript.final", ROOM, {"speaker": b["identity"], "lang": "de", "text": text,
                                                          "asr_conf": 0.9}, utt=f"u-{i:04d}"))
        with client.websocket_connect(f"/api/events/{ROOM}") as sub:
            recv_until(sub, "transcript.final", pred=lambda m: m["data"]["text"] == "drei")
    events = client.get(f"/api/rooms/{ROOM}/events").json()
    finals = [e for e in events if e["type"] == "transcript.final"]
    assert [e["data"]["text"] for e in finals] == ["eins", "zwei", "drei"]
    since = finals[0]["ts"]
    later = client.get(f"/api/rooms/{ROOM}/events", params={"since": since}).json()
    assert all(e["ts"] > since for e in later)

    with client.websocket_connect(f"/api/events/{ROOM}") as sub:
        replay = [sub.receive_json() for _ in range(len(events))]
        assert [e["type"] for e in replay] == [e["type"] for e in events]  # backlog first, in order
        current = sub.receive_json()
        assert current["type"] == "session.state" and current["data"]["session_id"] == b["session_id"]
    with client.websocket_connect(f"/api/events/{ROOM}?since={finals[-1]['ts']}") as sub:
        assert sub.receive_json()["type"] == "session.state"  # nothing newer than the last final


def test_events_ws_meter_and_gpu_ticks(client):
    with client.websocket_connect(f"/api/events/{ROOM}") as sub:
        meter = recv_until(sub, "privacy.meter")
        gpu = recv_until(sub, "gpu.stats")
    assert meter["room"] == ROOM and meter["data"]["third_party_bytes"] >= 0 and "sealed" in meter["data"]
    assert isinstance(gpu["data"]["models_loaded"], list) and gpu["data"]["util_pct"] >= 0
    with pytest.raises(WebSocketDisconnect) as e, client.websocket_connect("/api/events/nope") as sub:
        sub.receive_json()
    assert e.value.code == 4404


def test_events_ws_answers_the_app_heartbeat(client):
    """The app's liveness check: "ping" gets "pong" between the envelopes; other text is ignored."""
    with client.websocket_connect(f"/api/events/{ROOM}") as sub:
        sub.send_text("hello")  # ignored, the socket stays up
        sub.send_text("ping")
        for _ in range(200):
            msg = sub.receive_text()
            if msg == "pong":
                break
            assert json.loads(msg)["v"] == 1  # everything else is an envelope
        else:
            pytest.fail("no pong")
        sub.send_text("ping")
        assert any(sub.receive_text() == "pong" for _ in range(200))


def test_health_reports_voice_clone_only_for_a_cloning_engine(client, core):
    from raum.config import EngineConfig

    assert client.get("/api/health").json()["features"]["voice_clone"] is False  # mock profile
    core.cfg.models.tts = EngineConfig(engine="http", url="http://127.0.0.1:8090")
    assert client.get("/api/health").json()["features"]["voice_clone"] is True
    core.cfg.models.tts = EngineConfig(engine="http", options={"voice_ref": False})  # sample capture off
    assert client.get("/api/health").json()["features"]["voice_clone"] is False
    for engine in ("piper", "mock", "off"):
        core.cfg.models.tts = EngineConfig(engine=engine)
        assert client.get("/api/health").json()["features"]["voice_clone"] is False, engine


def test_voice_switch_pushes_metadata_and_is_audited(client, core, pub):
    pushed: list[dict] = []

    async def update_participant(room, identity, meta):
        pushed.append(meta)
        return True

    pub.update_participant = update_participant
    j = join(client)
    r = client.post(f"/api/rooms/{ROOM}/participant", json={"identity": j["identity"], "voice": "standard"})
    assert r.status_code == 200 and r.json()["voice"] == "standard"
    assert pushed == [{"role": "b", "lang": "ar", "device": "phone", "path": "direct", "audio_out": "speaker",
                       "voice": "standard", "original_voice": False}]
    upd = [e["meta"] for e in core.sessions.audit.entries(session=j["session_id"])
           if e["event"] == "participant_update"]
    assert upd == [{"role": "b", "lang": "ar", "voice": "standard"}]
    # call mode (pipeline/livevoice.py): the agent reads it from the same metadata
    r = client.post(f"/api/rooms/{ROOM}/participant", json={"identity": j["identity"], "original_voice": True})
    assert r.json()["original_voice"] is True and pushed[-1]["original_voice"] is True


def test_participant_update_changes_lang_and_audio_out(tmp_path):
    from fastapi.testclient import TestClient

    from raum.api.server import create_app

    app = create_app()
    with TestClient(app) as c:
        j = c.post("/api/join", json={"room": "sz-7001", "role": "b", "lang": "en", "consent": True,
                                      "audio_out": "headphones"}).json()
        r = c.post("/api/rooms/sz-7001/participant", json={"identity": j["identity"], "lang": "ar-SY"})
        assert r.status_code == 200 and r.json()["lang"] == "ar" and r.json()["audio_out"] == "headphones"
        r = c.post("/api/rooms/sz-7001/participant", json={"identity": j["identity"], "audio_out": "speaker"})
        assert r.json()["audio_out"] == "speaker" and r.json()["lang"] == "ar"
        assert r.json()["voice"] == "own"  # the default
        r = c.post("/api/rooms/sz-7001/participant", json={"identity": j["identity"], "voice": "standard"})
        assert r.json()["voice"] == "standard" and r.json()["lang"] == "ar" and r.json()["audio_out"] == "speaker"
        r = c.post("/api/rooms/sz-7001/participant", json={"identity": j["identity"], "lang": "tr"})
        assert r.json()["voice"] == "standard"  # kept
        assert c.post("/api/rooms/sz-7001/participant", json={"identity": j["identity"], "voice": "x"}).status_code == 422
        assert c.post("/api/rooms/sz-7001/participant", json={"identity": "b-nope"}).status_code == 404
