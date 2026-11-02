"""F-05 admin console API: status per room, live latency p50/p95, audit chain verification."""

from __future__ import annotations

import json

import pytest
from core_doubles import FakePublisher, ev, install_security, make_config, recv_until
from fastapi.testclient import TestClient

from raum.api.admin import percentile
from raum.api.server import create_app
from raum.link import KEY_HEADER

pytestmark = pytest.mark.filterwarnings("ignore:The HMAC key")
ROOM = "sz-4821"
KEY = {KEY_HEADER: "dev-internal"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("RAUM_PROFILE", raising=False)
    install_security(monkeypatch, tmp_path)
    app = create_app(make_config(tmp_path))
    app.state.core.sessions.publisher = FakePublisher()
    return app


@pytest.fixture
def client(app):
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c


def test_percentile_matches_the_app():
    assert percentile([], 50) is None
    assert percentile([300.0, 100.0], 50) == 300.0  # JS Math.round(0.5) = 1
    assert percentile([float(x) for x in range(1, 101)], 95) == 95.0


def test_latency_status_and_audit_verify(client, app):
    client.post("/api/join", json={"room": ROOM, "role": "b", "lang": "ar", "consent": True})
    with client.websocket_connect(f"/api/events/{ROOM}") as sub, \
            client.websocket_connect(f"/api/internal/agent/{ROOM}", headers=KEY) as agent:
        agent.receive_json()
        agent.send_json({"kind": "hello", "room": ROOM, "agent": "worker-1/job-9"})
        agent.send_json(ev("participant.joined", ROOM, {"id": "a-spk", "role": "a", "lang": "de",
                                                        "device": "speakerphone", "audio_out": "room"}))
        for i, first in enumerate((600, 800, 1200, 700)):
            agent.send_json(ev("pipeline.timing", ROOM, {"vad_ms": 300, "asr_ms": 150, "mt_first_ms": first,
                                                         "mt_full_ms": first + 400, "target_lang": "ar"},
                               utt=f"u-{i + 1:04d}"))
        recv_until(sub, "pipeline.timing", pred=lambda m: m["utt"] == "u-0004")

        lat = client.get("/api/admin/latency").json()
        r = lat["rooms"][ROOM]
        assert lat["budget_ms"] == 1000 and r["n"] == 4 and r["over_budget"] == 1
        assert r["mt_first_ms"] == {"n": 4, "p50": 800.0, "p95": 1200.0}
        assert r["tts_first_ms"] == {"n": 0, "p50": None, "p95": None}
        assert client.get("/api/admin/latency", params={"room": "kz-1"}).json()["rooms"] == {}

        st = client.get("/api/admin/status").json()
        room = st["rooms"][0]
        assert room["agent_connected"] and room["agent_id"] == "worker-1/job-9" and room["agent_since"]
        assert room["auto_burn_minutes"] == 30 and room["idle_s"] is not None
        dev = next(d for d in st["devices"] if d["id"] == "a-spk")
        assert (dev["device"], dev["audio_out"], dev["path"]) == ("speakerphone", "room", "direct")

    v = client.get("/api/admin/audit/verify").json()
    assert v["ok"] is True and v["lines"] >= 3 and v["first_bad"] is None
    assert v["head_hash"] == st["audit"]["head_hash"]

    path = app.state.core.cfg.audit_path()
    lines = path.read_text().splitlines()
    e = json.loads(lines[1])
    e["ts"] += 1  # edit one line
    lines[1] = json.dumps(e)
    path.write_text("\n".join(lines) + "\n")
    v = client.get("/api/admin/audit/verify").json()
    assert v["ok"] is False and v["first_bad"] == 2 and "hash" in v["reason"]
