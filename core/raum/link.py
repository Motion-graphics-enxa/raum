"""Agent <-> core link protocol (internal; not part of the public event API).

raum-agent opens one WebSocket per room to raum-core:

    ws://127.0.0.1:8000/api/internal/agent/{room}      header  x-raum-key: <network.internal_key>

agent -> core  (JSON, one object per message)
    {"kind": "hello", "room": "sz-4821", "agent": "<worker id>"}
    {"kind": "event", "event": <Envelope>}          every event the agent publishes on the data channel
                                                    (core persists content events encrypted, updates
                                                    participants, fans out to /api/events/{room})

core -> agent
    {"kind": "state", "state": <SessionState>}      on connect and whenever it changes
    {"kind": "control", "control": <Control>}       device actions the pipeline must act on:
                                                    off_record, repeat_back, explain_simply,
                                                    explain_approve, correct_term, end, burn

Core-originated events (session.state, confirm, summary.ready, burn.done) are published to the
room's data channel by core itself via the LiveKit server API (RoomService.SendData, topic
raum.events), so they reach phones even if no agent is connected.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from raum.events import Control, Envelope, SessionState

AGENT_LINK_PATH = "/api/internal/agent/{room}"
KEY_HEADER = "x-raum-key"


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Hello(_Msg):
    kind: Literal["hello"] = "hello"
    room: str
    agent: str


class EventMsg(_Msg):
    kind: Literal["event"] = "event"
    event: Envelope


class StateMsg(_Msg):
    kind: Literal["state"] = "state"
    state: SessionState


class ControlMsg(_Msg):
    kind: Literal["control"] = "control"
    control: Control


def parse_link_message(raw: str | bytes | dict[str, Any]) -> Hello | EventMsg | StateMsg | ControlMsg:
    import json

    d = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    kind = d.get("kind")
    cls = {"hello": Hello, "event": EventMsg, "state": StateMsg, "control": ControlMsg}.get(kind)
    if cls is None:
        raise ValueError(f"unknown link message kind: {kind!r}")
    return cls.model_validate(d)
