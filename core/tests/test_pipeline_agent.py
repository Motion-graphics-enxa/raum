"""Agent side without LiveKit: link client (loopback WebSocket stub of core) and worker helpers."""

from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace

import numpy as np
from livekit import rtc

from raum.agent.link_client import LinkClient, link_url
from raum.agent.worker import is_agent, participant_info
from raum.events import Control, SessionState, make_event
from raum.link import KEY_HEADER, ControlMsg, StateMsg


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_link_url():
    assert link_url("http://127.0.0.1:8000", "sz-4821") == "ws://127.0.0.1:8000/api/internal/agent/sz-4821"
    assert link_url("https://box.local/", "kz-1") == "wss://box.local/api/internal/agent/kz-1"


async def test_core_down_never_blocks():
    link = LinkClient("sz-1", core_url=f"http://127.0.0.1:{free_port()}", key="k", agent_id="t",
                      max_buffer=3, min_backoff=0.01, max_backoff=0.02)
    link.start()
    for i in range(5):
        link.send_event(make_event("utterance.start", "sz-1", {"speaker": "a", "lang": "de"}, utt=f"u-{i}"))
    await asyncio.sleep(0.1)
    assert not link.connected and len(link._buf) == 3 and link.dropped == 2
    await asyncio.wait_for(link.aclose(), 1)


async def test_link_roundtrip_with_core_stub():
    from websockets.asyncio.server import serve

    got: list[dict] = []
    headers: dict[str, str] = {}
    states: list[SessionState] = []
    controls: list[Control] = []
    controls_seen = asyncio.Event()

    async def core(ws):
        headers.update({KEY_HEADER: ws.request.headers.get(KEY_HEADER), "path": ws.request.path})
        state = SessionState(off_record=True, sealed=False, participants=[], mode="sprechzimmer")
        await ws.send(StateMsg(state=state).model_dump_json())
        await ws.send(ControlMsg(control=Control(action="repeat_back", by="a")).model_dump_json())
        async for raw in ws:
            got.append(json.loads(raw))
            if len(got) == 3:
                await ws.close()

    port = free_port()
    async with serve(core, "127.0.0.1", port):
        def on_state(s):
            states.append(s)

        async def on_control(c):
            controls.append(c)
            controls_seen.set()

        link = LinkClient("sz-4821", core_url=f"http://127.0.0.1:{port}", key="secret-key", agent_id="w/j",
                          on_state=on_state, on_control=on_control, min_backoff=0.01)
        link.send_event(make_event("utterance.start", "sz-4821", {"speaker": "a", "lang": "de"}, utt="u-0001"))
        link.start()
        link.send_event(make_event("utterance.end", "sz-4821", {"speaker": "a", "lang": "de"}, utt="u-0001"))
        await asyncio.wait_for(controls_seen.wait(), 2)
        for _ in range(100):
            if len(got) >= 3:
                break
            await asyncio.sleep(0.01)
        await link.aclose()

    assert headers == {KEY_HEADER: "secret-key", "path": "/api/internal/agent/sz-4821"}
    assert got[0] == {"kind": "hello", "room": "sz-4821", "agent": "w/j"}
    assert [m["event"]["type"] for m in got[1:3]] == ["utterance.start", "utterance.end"]  # buffered, in order
    assert states[0].off_record is True and controls[0].action == "repeat_back"


def _p(identity, metadata="", attributes=None, kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD):
    return SimpleNamespace(identity=identity, metadata=metadata, attributes=attributes or {}, kind=kind)


def test_participant_info_from_metadata_attributes_and_identity():
    p = participant_info(_p("b-k3x9", json.dumps({"role": "b", "lang": "ar", "device": "phone", "path": "tunnel"})))
    assert (p.id, p.role, p.lang, p.device, p.path) == ("b-k3x9", "b", "ar", "phone", "tunnel")
    p = participant_info(_p("x-1", "", {"role": "a", "lang": "de"}))
    assert (p.role, p.lang) == ("a", "de")
    p = participant_info(_p("a-doc", "not json"))
    assert (p.role, p.lang, p.path) == ("a", "auto", "direct")
    p = participant_info(_p("t-1", json.dumps({"role": "boss", "path": "moon"})))
    assert (p.role, p.path) == ("b", "direct")


def test_is_agent():
    assert is_agent(_p("whatever", kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT))
    assert is_agent(_p("raum-agent"))
    assert not is_agent(_p("b-k3x9"))


def _attrs(meta: dict) -> dict[str, str]:
    return {k: str(v) for k, v in meta.items()}


def test_participant_info_reads_audio_out_and_prefers_the_changed_source():
    meta = {"role": "b", "lang": "ar", "device": "phone", "path": "direct", "audio_out": "headphones"}
    p = participant_info(_p("b-k3x9", json.dumps(meta), _attrs(meta)))
    assert (p.lang, p.audio_out) == ("ar", "headphones")
    assert participant_info(_p("b-1", json.dumps({"role": "b", "lang": "ar"}))).audio_out == "speaker"  # default
    assert participant_info(_p("b-1", json.dumps({"audio_out": "loud"}))).audio_out == "speaker"
    # core sets metadata and attributes to the same JSON; each change event reads its own source first
    half = _p("b-k3x9", json.dumps(meta), {**_attrs(meta), "lang": "tr", "audio_out": "room"})
    assert participant_info(half).lang == "ar"
    via_attrs = participant_info(half, prefer="attributes")
    assert (via_attrs.lang, via_attrs.audio_out) == ("tr", "room")


def _agent():
    from pipeline_helpers import fast_config

    from raum.agent.worker import RoomAgent
    from raum.modes import get_mode

    ctx = SimpleNamespace(job=SimpleNamespace(room=SimpleNamespace(name="sz-1000"), id="job-1"), worker_id="w-1",
                          room=SimpleNamespace(isconnected=lambda: False))
    agent = RoomAgent(ctx, fast_config(), get_mode("sprechzimmer"), vad=None)
    events = []

    async def emit(env):
        events.append(env)

    agent.emit = emit  # type: ignore[method-assign]
    return agent, events


async def test_live_participant_change_reaches_the_pipeline_and_the_devices():
    agent, events = _agent()
    prepared: list[list[str]] = []
    agent.engines.tts = SimpleNamespace(prepare=lambda langs: prepared.append(list(langs)))
    meta = {"role": "b", "lang": "ar", "device": "phone", "path": "direct", "audio_out": "speaker"}
    p = _p("b-k3x9", json.dumps(meta), _attrs(meta))
    agent._on_participant_connected(p)
    assert prepared[-1] == ["de", "ar"]  # resident voices for the room's languages

    new = {**meta, "lang": "tr", "audio_out": "headphones"}
    p.metadata = json.dumps(new)  # POST /api/rooms/{room}/participant: metadata first ...
    agent._on_participant_changed(p)
    p.attributes = _attrs(new)  # ... then attributes: nothing new
    agent._on_participant_changed(p, prefer="attributes")
    await asyncio.sleep(0)
    info = agent.pipeline.participants["b-k3x9"]
    assert (info.lang, info.audio_out) == ("tr", "headphones")
    joined = [e.data for e in events if e.type == "participant.joined"]
    assert [(j["lang"], j["audio_out"]) for j in joined] == [("ar", "speaker"), ("tr", "headphones")]
    assert prepared[-1] == ["de", "tr"]

    back = {**new, "lang": "ar"}
    p.attributes = _attrs(back)  # attributes first this time
    agent._on_participant_changed(p, prefer="attributes")
    p.metadata = json.dumps(back)
    agent._on_participant_changed(p)
    await asyncio.sleep(0)
    assert agent.pipeline.participants["b-k3x9"].lang == "ar"
    assert [e.data["lang"] for e in events if e.type == "participant.joined"] == ["ar", "tr", "ar"]

    agent._on_participant_changed(_p("b-unknown", json.dumps(back)))  # never connected: ignored
    assert "b-unknown" not in agent.pipeline.participants


async def test_mic_frames_of_a_paused_device_reach_the_vad_as_silence():
    agent, _ = _agent()
    for ident, meta in (("a-doc1", {"role": "a", "lang": "de"}), ("b-k3x9", {"role": "b", "lang": "ar"})):
        agent._on_participant_connected(_p(ident, json.dumps(meta)))
    await asyncio.sleep(0)
    pcm = np.full(320, 1000, dtype=np.int16)
    frame = rtc.AudioFrame(data=pcm.tobytes(), sample_rate=16000, num_channels=1, samples_per_channel=320)
    agent.pipeline.echo.play("ar", 1.0, agent.pipeline.participants.values())  # the Arabic voice plays
    assert agent._mic_frame("a-doc1", frame) is frame  # the doctor is heard
    quiet = agent._mic_frame("b-k3x9", frame)  # the patient's phone hears its own speaker: silence
    assert quiet.samples_per_channel == 320 and not np.frombuffer(quiet.data, dtype=np.int16).any()
