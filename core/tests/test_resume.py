"""C-12 reconnect/resume: the session lives on the box while phones drop, rejoin, or the agent restarts.

End-to-end with LiveKit: scripts/e2e/reconnect.py (bots) and reconnect-browser.mjs (Chrome).
"""

from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace

import pytest
from core_doubles import FakePublisher, ev, install_security, make_config
from fastapi.testclient import TestClient
from livekit import api, rtc

from raum.agent import worker
from raum.agent.link_client import LinkClient
from raum.api.server import create_app
from raum.config import load_config
from raum.events import SessionState
from raum.link import KEY_HEADER, StateMsg
from raum.modes import mode_for_room

pytestmark = pytest.mark.filterwarnings("ignore:The HMAC key")
ROOM = "sz-4821"
KEY = {KEY_HEADER: "dev-internal"}


def _p(identity, meta):
    return SimpleNamespace(identity=identity, metadata=json.dumps(meta), attributes={},
                           kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD)


class _Room:
    def isconnected(self):
        return False


def _agent(monkeypatch) -> worker.RoomAgent:
    cfg = load_config()
    ctx = SimpleNamespace(job=SimpleNamespace(room=SimpleNamespace(name=ROOM), id="job-1"), worker_id="w-1",
                          room=_Room())
    return worker.RoomAgent(ctx, cfg, mode_for_room(ROOM), vad=None)


def test_participant_info_audio_out():
    p = worker.participant_info(_p("a-spk", {"role": "a", "lang": "de", "device": "speakerphone",
                                             "audio_out": "room"}))
    assert (p.device, p.audio_out) == ("speakerphone", "room")
    assert worker.participant_info(_p("b-1", {"role": "b", "audio_out": "moon"})).audio_out == "speaker"


async def test_departed_phone_stays_a_target_for_the_grace(monkeypatch):
    monkeypatch.setattr(worker, "REJOIN_GRACE_S", 0.1)
    ag = _agent(monkeypatch)
    a = _p("a-doc1", {"role": "a", "lang": "de"})
    b = _p("b-pat1", {"role": "b", "lang": "ar"})
    ag._on_participant_connected(a)
    ag._on_participant_connected(b)
    ag._on_participant_disconnected(b)  # Wi-Fi gone
    assert set(ag.pipeline.participants) == {"a-doc1", "b-pat1"}  # still translated into Arabic
    assert "b-pat1" not in ag.infos
    ag._on_participant_connected(b)  # back within the grace
    assert not ag.departed and set(ag.pipeline.participants) == {"a-doc1", "b-pat1"}
    ag._on_participant_disconnected(b)
    await asyncio.sleep(0.25)  # grace over: resynced by the timer, no membership change needed
    assert set(ag.pipeline.participants) == {"a-doc1"}
    await asyncio.sleep(0)  # let the participant.* emits run
    await ag.pipeline.aclose()


async def test_restarted_agent_continues_utterance_ids(monkeypatch):
    ag = _agent(monkeypatch)
    ag._on_participant_connected(_p("a-doc1", {"role": "a", "lang": "de"}))
    ag._continue_utts(7)
    assert await ag.pipeline.speech_start("a-doc1") == "u-0008"
    ag._continue_utts(3)  # never backwards
    assert getattr(ag.pipeline, "_utt_n", 8) == 8
    await ag.pipeline.aclose()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_link_client_passes_utt_seq():
    from websockets.asyncio.server import serve

    seqs: list[int] = []
    states: list[SessionState] = []
    got = asyncio.Event()

    async def core(ws):
        st = SessionState(off_record=False, sealed=False, participants=[], mode="sprechzimmer")
        await ws.send(StateMsg(state=st, utt_seq=12).model_dump_json())
        await ws.send(StateMsg(state=st).model_dump_json())
        async for _ in ws:
            pass

    port = _free_port()
    async with serve(core, "127.0.0.1", port):
        link = LinkClient(ROOM, core_url=f"http://127.0.0.1:{port}", key="k", agent_id="w/j",
                          on_state=lambda s: (states.append(s), got.set() if len(states) == 2 else None),
                          on_utt_seq=seqs.append, min_backoff=0.01)
        link.start()
        await asyncio.wait_for(got.wait(), 2)
        await link.aclose()
    assert seqs == [12] and len(states) == 2


# ---------------------------------------------------------------- core


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("RAUM_PROFILE", raising=False)
    monkeypatch.delenv("RAUM_AGENT_NAME", raising=False)
    install_security(monkeypatch, tmp_path)
    app = create_app(make_config(tmp_path))
    app.state.core.sessions.publisher = FakePublisher()
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c


def _join(client, role="b", lang="ar"):
    r = client.post("/api/join", json={"room": ROOM, "role": role, "lang": lang, "consent": True})
    assert r.status_code == 200, r.text
    return r.json()


def test_core_hands_a_new_agent_the_utterance_count(client):
    b = _join(client)
    with client.websocket_connect(f"/api/internal/agent/{ROOM}", headers=KEY) as agent:
        first = agent.receive_json()
        assert first["kind"] == "state" and first.get("utt_seq") is None
        agent.send_json(ev("participant.joined", ROOM, {"id": b["identity"], "role": "b", "lang": "ar"}))
        assert agent.receive_json().get("utt_seq") is None
        for n in (1, 2, 5):
            agent.send_json(ev("utterance.start", ROOM, {"speaker": b["identity"], "lang": "ar"}, utt=f"u-{n:04d}"))
        agent.send_json(ev("participant.left", ROOM, {"id": b["identity"]}))
        assert agent.receive_json()["utt_seq"] == 5  # state change after participant.left
    # the room emptied past LiveKit's departure timeout; a new agent job links the same session
    with client.websocket_connect(f"/api/internal/agent/{ROOM}", headers=KEY) as agent:
        st = agent.receive_json()
        assert st["utt_seq"] == 5 and st["state"]["session_id"] == b["session_id"]
    info = client.get(f"/api/rooms/{ROOM}").json()
    assert info["state"]["session_id"] == b["session_id"] and info["state"]["status"] == "live"


def test_join_token_room_config(client, monkeypatch):
    claims = api.TokenVerifier("devkey", "secret").verify(_join(client)["token"])
    assert claims.room_config.departure_timeout == 120 and claims.room_config.empty_timeout == 300
    assert not claims.room_config.agents  # auto-dispatch
    monkeypatch.setenv("RAUM_AGENT_NAME", "raum-c12")
    claims = api.TokenVerifier("devkey", "secret").verify(_join(client)["token"])
    assert [d.agent_name for d in claims.room_config.agents] == ["raum-c12"]
