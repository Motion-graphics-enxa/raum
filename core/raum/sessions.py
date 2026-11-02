"""Rooms and live sessions in raum-core (plan §6.10, C-11, C-12, T-01..T-05).

One live session per room id, created on the first join (or when an agent links a room that has
none, e.g. after a core restart). A session owns:

- ``session_id`` (uuid4) and an encrypted ``SessionStore(session_id, <data_dir>/sessions)``: every
  content event (``raum.events.CONTENT_EVENTS``) is appended encrypted, nothing else;
- an in-memory ring buffer of envelopes for replay (``/api/events``) and the backlog endpoint;
- state: off_record, status live|ended|burned, human_interpreter_requested, started_at.

Room-level (they outlive a burn): the mode, the LiveKit participants (as reported by the agent or
by presence pings), issued join tokens, WebSocket subscribers and the agent link.

The audit log (``AuditLog(cfg.audit_path())``) is shared by all rooms and never sees content.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import secrets
import string
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from raum.config import RaumConfig
from raum.documents import to_event
from raum.events import (
    CONTENT_EVENTS,
    BurnDone,
    Confirm,
    Control,
    DocumentReady,
    Envelope,
    ExplainSent,
    ParticipantInfo,
    SessionState,
    SummaryReady,
    SummarySection,
    make_event,
    now_ms,
)
from raum.glossary import Glossary, GlossaryError, Term, parse_terms, term_to_dict
from raum.link import ControlMsg, StateMsg
from raum.modes import ModeConfig, get_mode, mode_for_room, new_room_id
from raum.publisher import NullPublisher, Publisher
from raum.security.audit import GENESIS, AuditLog, VerifyReport, verify_chain
from raum.security.burn import burn_session
from raum.security.crypto import SessionBurned, SessionStore
from raum.summary import build_summary, section_title

log = logging.getLogger("raum.sessions")

EVENT_BUFFER = 5000  # envelopes kept per session for replay
SUBSCRIBER_QUEUE = 2000
CONSENT_VERSION = "ai-act-50-v1"
VISION_TIMEOUT_S = 150.0  # one document reading, including a cold model load
SessionStatus = Literal["live", "ended", "burned"]


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


class BadRequest(ValueError):
    pass


class Unavailable(RuntimeError):
    """A feature switched off in this profile (HTTP 503)."""


class UpstreamError(RuntimeError):
    """A model server failed or timed out (HTTP 502)."""


# ---------------------------------------------------------------- in-memory fallbacks
# Used only while raum.security is still the contract stub (NotImplementedError), so core runs
# standalone. They never write anything to disk.


class MemoryStore:
    def __init__(self, session_id: str, directory: str | Path) -> None:
        self._sid = session_id
        self._path = Path(directory) / f"{session_id}.bin"
        self._records: list[dict[str, Any]] = []
        self._burned = False

    @property
    def session_id(self) -> str:
        return self._sid

    @property
    def path(self) -> Path:
        return self._path

    @property
    def burned(self) -> bool:
        return self._burned

    def append(self, record: dict[str, Any]) -> None:
        if self._burned:
            raise SessionBurned(self._sid)
        self._records.append(json.loads(json.dumps(record)))

    def read_all(self) -> list[dict[str, Any]]:
        if self._burned:
            raise SessionBurned(self._sid)
        return [json.loads(json.dumps(r)) for r in self._records]

    def burn(self) -> None:
        self._records.clear()
        self._burned = True


class MemoryAudit:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries: list[dict[str, Any]] = []

    @staticmethod
    def _hash(prev: str, line: dict[str, Any]) -> str:
        canon = json.dumps(line, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256((prev + canon).encode()).hexdigest()

    def append(self, event: str, session: str | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        line = {"ts": now_ms(), "session": session, "event": event, "meta": dict(meta or {}),
                "prev": self.head_hash()}
        entry = {**line, "hash": self._hash(line["prev"], line)}
        self._entries.append(entry)
        return entry

    def head_hash(self) -> str:
        return self._entries[-1]["hash"] if self._entries else GENESIS

    def entries(self, session: str | None = None) -> list[dict[str, Any]]:
        return [dict(e) for e in self._entries if session is None or e.get("session") == session]

    def verify(self) -> VerifyReport:
        prev = GENESIS
        for i, e in enumerate(self._entries, 1):
            line = {k: v for k, v in e.items() if k != "hash"}
            if line.get("prev") != prev or self._hash(prev, line) != e.get("hash"):
                return VerifyReport(ok=False, lines=len(self._entries), first_bad=i, reason="hash mismatch")
            prev = e["hash"]
        return VerifyReport(ok=True, lines=len(self._entries))


def open_store(session_id: str, directory: Path) -> Any:
    with contextlib.suppress(OSError):
        directory.mkdir(parents=True, exist_ok=True)
    try:
        return SessionStore(session_id, directory)
    except NotImplementedError:
        log.error("raum.security.crypto.SessionStore is not implemented yet: session %s is kept in memory "
                  "only (no disk, no encryption at rest)", session_id)
        return MemoryStore(session_id, directory)


def open_audit(path: Path) -> Any:
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return AuditLog(path)
    except NotImplementedError:
        log.error("raum.security.audit.AuditLog is not implemented yet: audit log kept in memory only")
        return MemoryAudit(path)


def burn_store(store: Any, audit: Any) -> BurnDone:
    try:
        return burn_session(store, audit)
    except NotImplementedError:
        store.burn()
        audit.append("key_destroyed", store.session_id, {})
        return BurnDone(key_destroyed_at=now_ms(), audit_head_hash=audit.head_hash())


# ---------------------------------------------------------------- connections


class AgentConn:
    """Core side of one agent link (``raum.link``). Sends are serialised."""

    def __init__(self, send_text: Callable[[str], Awaitable[None]], agent_id: str | None = None) -> None:
        self._send_text = send_text
        self._lock = asyncio.Lock()
        self.agent_id = agent_id
        self.connected_at = now_ms()
        self.closed = False

    async def send(self, msg: BaseModel) -> bool:
        if self.closed:
            return False
        try:
            async with self._lock:
                await self._send_text(msg.model_dump_json())
            return True
        except Exception as e:  # noqa: BLE001 - the link dropped; the receive loop cleans up
            log.warning("agent link send failed: %s", e)
            self.closed = True
            return False


class Subscriber:
    """One ``/api/events`` WebSocket. Bounded; a slow screen loses its oldest events, not the box."""

    def __init__(self, maxsize: int = SUBSCRIBER_QUEUE) -> None:
        self.queue: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=maxsize)

    def push(self, env: Envelope) -> None:
        try:
            self.queue.put_nowait(env)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(env)


# ---------------------------------------------------------------- rooms and sessions


@dataclass
class Session:
    id: str
    room: str
    mode: ModeConfig
    store: Any
    started_at: int = field(default_factory=now_ms)
    status: SessionStatus = "live"
    off_record: bool = False
    human_interpreter_requested: bool = False
    events: deque[Envelope] = field(default_factory=lambda: deque(maxlen=EVENT_BUFFER))
    summary: SummaryReady | None = None
    ended_at: int | None = None
    burn: BurnDone | None = None
    last_activity: float = field(default_factory=time.monotonic)
    # N-02: the room's glossary for this session (mode file + uploads + V-05 corrections), in memory
    # only and dropped on burn; the uploads are re-sent to an agent that (re)links.
    glossary: Glossary | None = None
    glossary_uploads: Glossary = field(default_factory=Glossary)  # every upload of this session, merged
    utt_seq: int = 0  # highest utterance number seen (``u-0007`` -> 7); a restarted agent continues after it

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def saw_utt(self, utt: str | None) -> None:
        if utt and utt.startswith("u-"):
            with contextlib.suppress(ValueError):
                self.utt_seq = max(self.utt_seq, int(utt[2:]))


@dataclass(eq=False)
class Room:
    id: str
    mode: ModeConfig
    created_at: int = field(default_factory=now_ms)
    session: Session | None = None
    participants: dict[str, ParticipantInfo] = field(default_factory=dict)
    issued: dict[str, ParticipantInfo] = field(default_factory=dict)
    subscribers: set[Subscriber] = field(default_factory=set)
    agent: AgentConn | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_presence_broadcast: float = 0.0


def _participant(data: dict[str, Any]) -> ParticipantInfo:
    return ParticipantInfo.model_validate(data)


class SessionManager:
    def __init__(self, cfg: RaumConfig, *, publisher: Publisher | None = None, audit: Any = None,
                 engines: Any = None, vision: Any = None) -> None:
        self.cfg = cfg
        self.publisher: Publisher = publisher or NullPublisher()
        self.audit = audit if audit is not None else open_audit(cfg.audit_path())
        self.sessions_dir = cfg.data_dir() / "sessions"
        self.rooms: dict[str, Room] = {}
        self.past: dict[str, Session] = {}  # earlier sessions of a room (no content left after burn)
        self.sealed = False
        self._engines = engines
        self._engines_ready = engines is not None
        self._vision = vision
        self._vision_ready = vision is not None
        # document readings share the GPU with live translation: at most background_slots at a time
        self._vision_slots = asyncio.Semaphore(max(1, cfg.scheduler.background_slots))

    # ------------------------------------------------------------ helpers

    def _audit(self, event: str, session: str | None, **meta: Any) -> dict[str, Any] | None:
        try:
            return self.audit.append(event, session=session, meta=meta)
        except Exception as e:  # noqa: BLE001 - a broken audit line must not take the conversation down
            log.error("audit append %s failed: %s", event, e)
            return None

    def find_room(self, room_id: str) -> Room:
        room = self.rooms.get(room_id)
        if room is None:
            raise NotFound(f"unknown room: {room_id}")
        return room

    def room(self, room_id: str) -> Room:
        """Get or register a room. Raises ``ModeError`` for an invalid id or unknown mode prefix."""
        room = self.rooms.get(room_id)
        if room is None:
            room = Room(id=room_id, mode=mode_for_room(room_id))
            self.rooms[room_id] = room
        return room

    def create_room(self, mode_id: str) -> Room:
        get_mode(mode_id)
        for _ in range(100):
            rid = new_room_id(mode_id)
            if rid not in self.rooms:
                return self.room(rid)
        raise Conflict("no free room id")

    def live_session(self, room: Room) -> Session:
        s = room.session
        if s is None:
            raise NotFound(f"no session in room {room.id}")
        if s.status == "burned":
            raise Conflict(f"session in room {room.id} is burned")
        return s

    def role_of(self, room: Room, by: str | None) -> str | None:
        if not by:
            return None
        if by in ("a", "b", "observer"):
            return by
        p = room.participants.get(by) or room.issued.get(by)
        return p.role if p else by

    def _new_identity(self, room: Room, role: str) -> str:
        alphabet = string.ascii_lowercase + string.digits
        while True:
            ident = f"{role}-{''.join(secrets.choice(alphabet) for _ in range(4))}"
            if ident not in room.issued and ident not in room.participants:
                return ident

    # ------------------------------------------------------------ sessions

    def _start_session(self, room: Room, reason: str) -> Session:
        sid = str(uuid.uuid4())
        s = Session(id=sid, room=room.id, mode=room.mode, store=open_store(sid, self.sessions_dir))
        if room.session is not None:
            self.past[room.session.id] = room.session
        room.session = s
        self._audit("session_start", sid, room=room.id, mode=room.mode.id, profile=self.cfg.profile, reason=reason)
        self._audit("models", sid, models=self.cfg.engine_names())
        log.info("session %s started in %s (%s)", sid, room.id, reason)
        return s

    def ensure_session(self, room: Room, reason: str = "join") -> tuple[Session, bool]:
        if room.session is None or room.session.status == "burned":
            return self._start_session(room, reason), True
        return room.session, False

    def state(self, room: Room) -> SessionState:
        s = room.session
        return SessionState(
            off_record=bool(s and s.off_record),
            sealed=self.sealed,
            participants=list(room.participants.values()),
            mode=room.mode.id,
            status=s.status if s else "live",
            session_id=s.id if s else None,
            human_interpreter_requested=bool(s and s.human_interpreter_requested),
            started_at=s.started_at if s else None,
        )

    def room_info(self, room: Room) -> dict[str, Any]:
        s = room.session
        return {
            "room": room.id,
            "mode": room.mode.id,
            "name": room.mode.name,
            "status": s.status if s else "idle",
            "session_id": s.id if s else None,
            "started_at": s.started_at if s else None,
            "off_record": bool(s and s.off_record),
            "participants": [p.model_dump(mode="json") for p in room.participants.values()],
            "agent_connected": room.agent is not None,
            "agent_id": room.agent.agent_id if room.agent else None,
            "agent_since": room.agent.connected_at if room.agent else None,
            "subscribers": len(room.subscribers),
            "created_at": room.created_at,
            "idle_s": round(time.monotonic() - s.last_activity, 1) if s else None,
            "auto_burn_minutes": room.mode.retention.auto_burn_minutes,
        }

    # ------------------------------------------------------------ fan-out

    def _fanout(self, room: Room, env: Envelope, *, buffer: bool = True) -> None:
        if buffer and room.session is not None:
            room.session.events.append(env)
        for sub in list(room.subscribers):
            sub.push(env)

    def _persist(self, room: Room, env: Envelope, *, force: bool = False) -> bool:
        """Append a content event to the encrypted store (never while off the record, unless forced)."""
        s = room.session
        if env.type not in CONTENT_EVENTS or s is None or s.status == "burned":
            return False
        if not force and (s.status != "live" or s.off_record):
            return False
        try:
            s.store.append(env.model_dump(mode="json"))
            return True
        except SessionBurned:
            return False
        except Exception as e:  # noqa: BLE001
            log.error("session %s: could not store %s: %s", s.id, env.type, e)
            return False

    def emit(self, room: Room, type: str, data: BaseModel | dict[str, Any], *, utt: str | None = None,
             persist: bool = False, force: bool = False, publish: bool = True) -> Envelope:
        """A core-originated event: buffer, WebSocket subscribers and the LiveKit data channel."""
        env = make_event(type, room.id, data, utt=utt)
        if persist:
            self._persist(room, env, force=force)
        self._fanout(room, env)
        if publish:
            self.publisher.publish(env)
        return env

    async def state_changed(self, room: Room, *, publish: bool = True) -> SessionState:
        st = self.state(room)
        self.emit(room, "session.state", st, publish=publish)
        if room.agent is not None:
            await room.agent.send(self._state_msg(room, st))
        return st

    def _state_msg(self, room: Room, st: SessionState | None = None) -> StateMsg:
        s = room.session
        return StateMsg(state=st or self.state(room), utt_seq=s.utt_seq if s and s.utt_seq else None)

    async def forward(self, room: Room, ctl: Control) -> bool:
        if room.agent is None:
            return False
        return await room.agent.send(ControlMsg(control=ctl))

    def broadcast_ephemeral(self, type: str, data: BaseModel) -> int:
        """privacy.meter / gpu.stats: WebSocket subscribers only, not buffered, not published."""
        n = 0
        for room in self.rooms.values():
            if room.subscribers:
                env = make_event(type, room.id, data)
                for sub in list(room.subscribers):
                    sub.push(env)
                    n += 1
        return n

    def has_subscribers(self) -> bool:
        return any(r.subscribers for r in self.rooms.values())

    def backlog(self, room: Room, since: int | None = None) -> list[Envelope]:
        if room.session is None:
            return []
        return [e for e in room.session.events if since is None or e.ts > since]

    def subscribe(self, room_id: str, since: int | None = None) -> tuple[Subscriber, list[Envelope]]:
        room = self.room(room_id)
        sub = Subscriber()
        backlog = self.backlog(room, since)  # no await between snapshot and subscribe: no gap, no dupes
        room.subscribers.add(sub)
        return sub, backlog

    def unsubscribe(self, room_id: str, sub: Subscriber) -> None:
        room = self.rooms.get(room_id)
        if room is not None:
            room.subscribers.discard(sub)

    # ------------------------------------------------------------ join, presence

    async def join(self, room_id: str, *, role: str, lang: str, device: str, path: str,
                   consent_version: str = CONSENT_VERSION,
                   audio_out: str = "speaker", voice: str = "own") -> tuple[Room, Session, ParticipantInfo]:
        room = self.room(room_id)
        s, new = self.ensure_session(room)
        p = ParticipantInfo(id=self._new_identity(room, role), role=role, lang=lang, device=device, path=path,
                            audio_out=audio_out, voice=voice)
        room.issued[p.id] = p
        s.touch()
        self._audit("consent", s.id, role=role, lang=lang, device=device, consent_version=consent_version,
                    voice=p.voice)
        if new:
            await self.state_changed(room)
        return room, s, p

    async def update_participant(self, room_id: str, identity: str, *, lang: str | None = None,
                                 audio_out: str | None = None, voice: str | None = None) -> ParticipantInfo:
        """C-02 after joining: change language / voice output / whose voice (C-13). Pushes new metadata to
        LiveKit so the agent re-reads the participant from the next utterance (a switch to the standard
        voice drops the device's voice sample at once)."""
        room = self.find_room(room_id)
        p = room.participants.get(identity) or room.issued.get(identity)
        if p is None:
            raise NotFound(f"unknown participant {identity} in {room_id}")
        changes: dict[str, Any] = {}
        if lang:
            changes["lang"] = lang
        if audio_out:
            changes["audio_out"] = audio_out
        if voice:
            changes["voice"] = voice
        p = _participant({**p.model_dump(), **changes})
        if identity in room.participants:
            room.participants[identity] = p
        room.issued[identity] = p
        if room.session is not None:
            self._audit("participant_update", room.session.id, role=p.role, lang=p.lang, voice=p.voice)
        upd = getattr(self.publisher, "update_participant", None)
        if upd is not None:
            meta = {"role": p.role, "lang": p.lang, "device": p.device, "path": p.path, "audio_out": p.audio_out,
                    "voice": p.voice}
            await upd(room_id, identity, meta)
        await self.state_changed(room, publish=True)
        return p

    async def presence(self, room_id: str, identity: str, rtt_ms: int | None) -> ParticipantInfo:
        room = self.find_room(room_id)
        p = room.participants.get(identity)
        new = p is None
        if p is None:
            p = room.issued.get(identity)
            if p is None:
                raise NotFound(f"unknown participant {identity} in {room_id}")
        p = _participant({**p.model_dump(), "rtt_ms": rtt_ms})
        room.participants[identity] = p
        now = time.monotonic()
        if new or now - room.last_presence_broadcast >= 1.0:
            room.last_presence_broadcast = now
            await self.state_changed(room, publish=new)
        return p

    # ------------------------------------------------------------ agent link

    async def attach_agent(self, room_id: str, conn: AgentConn) -> Room:
        room = self.room(room_id)
        old, room.agent = room.agent, conn
        if old is not None and old is not conn:
            old.closed = True
        if room.session is None:
            self._start_session(room, reason="agent")
        await conn.send(self._state_msg(room))
        s = room.session
        if s is not None and s.status != "burned" and s.glossary_uploads.terms:  # N-02: a fresh agent has the file only
            terms = [term_to_dict(t) for t in s.glossary_uploads.terms]
            await conn.send(ControlMsg(control=Control(action="glossary", value={"terms": terms})))
        return room

    def detach_agent(self, room_id: str, conn: AgentConn) -> None:
        room = self.rooms.get(room_id)
        if room is not None and room.agent is conn:
            room.agent = None

    async def ingest(self, room_id: str, env: Envelope) -> None:
        """An event the agent published on the data channel: persist (content), track, fan out."""
        room = self.room(room_id)
        changed = False
        if env.type == "participant.joined":
            p = _participant(env.payload().model_dump())
            old = room.participants.get(p.id)
            if old is not None and p.rtt_ms is None and old.rtt_ms is not None:
                p = _participant({**p.model_dump(), "rtt_ms": old.rtt_ms})
            room.participants[p.id] = p
            changed = True
        elif env.type == "participant.left":
            changed = room.participants.pop(str(env.data.get("id")), None) is not None
        elif env.type in ("session.state", "summary.ready", "burn.done"):
            log.debug("ignoring core-owned %s from the agent in %s", env.type, room_id)
            return
        if room.session is not None:
            room.session.touch()
            room.session.saw_utt(env.utt)
            if env.type == "voice.echo":  # the voice said another figure than the text: the kind only, no content
                self._audit("voice.echo_mismatch", room.session.id, kind=str(env.data.get("kind") or ""),
                            target_lang=env.data.get("target_lang"))
        if self._persist(room, env) and room.session is not None:
            self._audit_fact(room.session.id, env)
        self._fanout(room, env)
        if changed:
            await self.state_changed(room)

    def _audit_fact(self, session_id: str, env: Envelope) -> None:
        """Checked facts in the audit log (no content: ids, kinds, how each was handled; raum.pipeline.readback):
        noted (nobody asked) or asked (read back), and a spoken yes/no from the agent (a tap is audited in
        ``control``)."""
        d = env.data
        if env.type == "flag" and d.get("status") in ("noted", "unconfirmed"):
            self._audit("fact_noted" if d["status"] == "noted" else "fact_asked", session_id, flag_id=d.get("id"),
                        kinds=sorted({str(f.get("kind")) for f in d.get("facts") or []}), spoken=bool(d.get("spoken")),
                        why=list(d.get("why") or []))
        elif env.type == "confirm" and d.get("via") == "voice":
            self._audit("confirm", session_id, by=d.get("by"), accepted=d.get("accepted"), flag_id=d.get("flag_id"),
                        via="voice")

    # ------------------------------------------------------------ controls

    async def control(self, room_id: str, ctl: Control) -> dict[str, Any]:
        room = self.find_room(room_id)
        a = ctl.action
        if a == "end":
            summary = await self.end(room_id)
            return {"ok": True, "action": a, "summary": summary, "state": self.state(room)}
        if a == "burn":
            done = await self.burn(room_id, by=self.role_of(room, ctl.by))
            return {"ok": True, "action": a, "burn": done, "state": self.state(room)}
        if a == "approve_summary":
            role = ctl.value if isinstance(ctl.value, str) and ctl.value else ctl.by
            summary = await self.approve_summary(room_id, role)
            return {"ok": True, "action": a, "summary": summary, "state": self.state(room)}

        s = self.live_session(room)
        s.touch()
        by = self.role_of(room, ctl.by)
        forwarded = False
        if a == "off_record":
            on = (not s.off_record) if ctl.value is None else bool(ctl.value)
            if on != s.off_record:
                s.off_record = on
                self._audit("off_record_on" if on else "off_record_off", s.id, by=by)
            forwarded = await self.forward(room, ctl.model_copy(update={"value": on}))
            await self.state_changed(room)
        elif a == "confirm":
            accepted = ctl.accepted if ctl.accepted is not None else (True if ctl.value is None else bool(ctl.value))
            self.emit(room, "confirm", Confirm(by=by or "?", accepted=accepted, flag_id=ctl.flag_id, via="button"),
                      utt=ctl.utt, persist=True)
            self._audit("confirm", s.id, by=by, accepted=accepted, flag_id=ctl.flag_id, via="button")
            # the agent then takes no spoken yes/no for that read-back any more (raum.pipeline.readback)
            forwarded = await self.forward(room, ctl.model_copy(update={"accepted": accepted}))
        elif a == "glossary":  # same as POST /glossary with terms in the YAML shape
            raw = ctl.value.get("terms") if isinstance(ctl.value, dict) else ctl.value
            try:
                terms = parse_terms(raw)
            except GlossaryError as e:
                raise BadRequest(str(e)) from None
            forwarded = bool((await self.upload_glossary(room_id, terms, by=ctl.by))["forwarded"])
        elif a in ("repeat_back", "explain_simply", "explain_approve", "correct_term", "voice_lock_reset"):
            forwarded = await self.forward(room, ctl)
            if a == "explain_approve":
                self._audit("explain_approved", s.id, by=by)
                if not forwarded and ctl.text:
                    # K-02: the agent is away (restarting); the approved text is in the request, so send it
                    self.emit(room, "explain.sent", ExplainSent(source_utt=ctl.utt or "",
                                                                target_lang=self._explain_lang(room, ctl),
                                                                text=ctl.text), utt=ctl.utt, persist=True)
            elif a == "correct_term":
                self._mirror_correction(room, s, ctl.value)
        elif a == "human_interpreter":
            req = True if ctl.value is None else bool(ctl.value)
            if req != s.human_interpreter_requested:
                s.human_interpreter_requested = req
                self._audit("human_interpreter", s.id, by=by, requested=req)
            await self.state_changed(room)
        elif a == "mute":
            muted = True if ctl.value is None else bool(ctl.value)
            targets = [i for i, p in room.participants.items()
                       if i == ctl.by or (ctl.by in ("a", "b", "observer") and p.role == ctl.by)]
            if not targets and ctl.by in room.issued:
                room.participants[ctl.by] = room.issued[ctl.by]
                targets = [ctl.by]
            for i in targets:
                room.participants[i] = _participant({**room.participants[i].model_dump(), "muted": muted})
            await self.state_changed(room)
        return {"ok": True, "action": a, "forwarded": forwarded, "state": self.state(room)}

    def _explain_lang(self, room: Room, ctl: Control) -> str:
        v = ctl.value
        if isinstance(v, dict) and v.get("lang"):
            return str(v["lang"])
        if isinstance(v, str) and v:
            return v
        for p in [*room.participants.values(), *reversed(list(room.issued.values()))]:
            if p.role == "b" and p.lang not in ("", "auto"):
                return p.lang
        return "en"

    # ------------------------------------------------------------ glossary (N-02, V-05)

    def session_glossary(self, s: Session) -> Glossary:
        """The session's glossary as the agent has it: mode file, then uploads and corrections."""
        if s.glossary is None:
            try:
                s.glossary = Glossary.load(s.mode.glossary)
            except Exception:
                log.exception("glossary %s failed to load", s.mode.glossary)
                s.glossary = Glossary()
        return s.glossary

    def _mirror_correction(self, room: Room, s: Session, value: Any) -> None:
        if not isinstance(value, dict) or not all(value.get(k) for k in ("src", "tgt", "lang")):
            return
        default = room.mode.roles["a"].default_lang
        src_lang = str(value.get("src_lang") or (default if default != "auto" else "de"))
        self.session_glossary(s).add_correction(str(value["src"]), str(value["tgt"]), str(value["lang"]),
                                                src_lang=src_lang)

    async def upload_glossary(self, room_id: str, terms: list[Term], *, by: str | None = None) -> dict[str, Any]:
        """N-02: merge an uploaded glossary into the room's session glossary and forward it to the agent.
        Works before anyone joined (the session starts here; the agent gets the terms when it links)."""
        room = self.room(room_id)
        s, new = self.ensure_session(room, reason="glossary")
        role = self.role_of(room, by)
        g = self.session_glossary(s)
        added, updated = g.merge(terms)
        wire = [term_to_dict(t) for t in terms]
        s.glossary_uploads.merge(terms)
        s.touch()
        self._audit("glossary_upload", s.id, by=role, terms=len(terms), added=added, updated=updated)
        forwarded = await self.forward(room, Control(action="glossary", by=role, value={"terms": wire}))
        if new:
            await self.state_changed(room)
        return {"ok": True, "room": room.id, "terms": len(terms), "added": added, "updated": updated,
                "total": len(g.terms), "langs": sorted({"de", *(lang for t in terms for lang in t.forms)}),
                "forwarded": forwarded, "uploaded": wire}

    def glossary_info(self, room_id: str) -> dict[str, Any]:
        room = self.room(room_id)
        s = room.session
        if s is not None and s.status != "burned":
            g = self.session_glossary(s)
        else:
            try:
                g = Glossary.load(room.mode.glossary)
            except Exception:  # noqa: BLE001
                g = Glossary()
        return {"room": room.id, "source": room.mode.glossary,
                "uploaded": len(s.glossary_uploads.terms) if s is not None else 0,
                "langs": g.langs(), "terms": [term_to_dict(t) for t in g.terms]}

    # ------------------------------------------------------------ document camera (F-07)

    def vision(self) -> Any:
        if not self._vision_ready:
            from raum.pipeline.engines import build_vision

            self._vision, self._vision_ready = build_vision(self.cfg), True
        return self._vision

    async def document(self, room_id: str, image: bytes, *, by: str, lang: str) -> DocumentReady:
        """Read a photographed document for ``lang`` and send ``document.ready`` to the room (stored
        encrypted like any content event). ``image`` stays in memory and is never stored or logged."""
        room = self.find_room(room_id)
        s = self.live_session(room)
        role = self.role_of(room, by) or by
        engine = self.vision()
        if engine is None:
            raise Unavailable("the document camera is off in this profile (models.vision.engine: off)")
        t0 = time.monotonic()
        try:
            async with self._vision_slots:
                reading = await asyncio.wait_for(engine.read(image, lang=lang, mode=room.mode.id), VISION_TIMEOUT_S)
        except TimeoutError:
            log.warning("document reading timed out in %s", room_id)
            raise UpstreamError("reading the document took too long") from None
        except Exception as e:  # noqa: BLE001 - the model server failed; say so, keep the room going
            log.warning("document reading failed in %s: %s: %s", room_id, e.__class__.__name__, str(e)[:200])
            raise UpstreamError("the document could not be read") from None
        doc = to_event(reading, by=role, lang=lang, ms=(time.monotonic() - t0) * 1000.0,
                       model=getattr(engine, "name", None), glossary=self.session_glossary(s))
        s.touch()
        self.emit(room, "document.ready", doc, persist=True)
        self._audit("document", s.id, by=role, lang=lang, facts=len(doc.facts), ms=doc.ms, model=doc.model)
        return doc

    # ------------------------------------------------------------ end, summary

    def languages(self, room: Room, records: list[dict[str, Any]]) -> tuple[str, str]:
        def pick(role: str) -> str | None:
            for p in room.participants.values():
                if p.role == role:
                    return p.lang
            for p in reversed(list(room.issued.values())):
                if p.role == role:
                    return p.lang
            for r in records:
                if r.get("type") == "transcript.final":
                    d = r.get("data") or {}
                    if str(d.get("speaker", "")).startswith(f"{role}-") and d.get("lang"):
                        return str(d["lang"])
            d = room.mode.roles[role].default_lang  # type: ignore[index]
            return None if d == "auto" else d

        a = pick("a") or "de"
        b = pick("b") or ("en" if a != "en" else "de")
        return a, b

    async def engines(self) -> Any:
        if self._engines_ready:
            return self._engines
        ver = self.cfg.models.summary or self.cfg.models.verify
        if not ver.enabled or ver.engine == "mock":
            self._engines_ready = True
            return None
        try:
            from raum.pipeline.engines import build_engines

            res = build_engines(self.cfg)
            if inspect.isawaitable(res):
                res = await res
            self._engines, self._engines_ready = res, True
        except Exception as e:  # noqa: BLE001 - not built yet (parallel workstream) or a model server is down
            log.warning("engines unavailable for the summary (%s); using the deterministic summary", e)
            return None
        return self._engines

    async def end(self, room_id: str, *, reason: str = "end") -> SummaryReady:
        room = self.find_room(room_id)
        async with room.lock:
            s = self.live_session(room)
            if s.status == "ended" and s.summary is not None:
                return s.summary
            try:
                records = s.store.read_all()
            except Exception as e:  # noqa: BLE001
                log.error("session %s: could not read records: %s", s.id, e)
                records = []
            lang_a, lang_b = self.languages(room, records)
            try:
                summary = await build_summary(room.mode, records, await self.engines(), lang_a, lang_b)
            except Exception:
                log.exception("summary failed in %s; sending an empty one", room_id)
                summary = SummaryReady(template=room.mode.id, lang_a=lang_a, lang_b=lang_b, sections=[
                    SummarySection(key=k, title=section_title(k, (lang_a, lang_b)), items=[])
                    for k in room.mode.summary.sections])
            s.summary = summary
            s.status = "ended"
            s.ended_at = now_ms()
            s.touch()
            self._audit("session_end", s.id, reason=reason, duration_ms=s.ended_at - s.started_at,
                        generated_ms=summary.generated_ms)
            self.emit(room, "summary.ready", summary, persist=True, force=True)
            await self.forward(room, Control(action="end"))
            await self.state_changed(room)
            return summary

    def summary(self, room_id: str) -> SummaryReady:
        room = self.find_room(room_id)
        s = room.session
        if s is None or s.summary is None:
            raise NotFound(f"no summary in room {room_id}")
        return s.summary

    async def approve_summary(self, room_id: str, role: str | None) -> SummaryReady:
        room = self.find_room(room_id)
        s = self.live_session(room)
        if s.summary is None:
            raise NotFound(f"no summary in room {room_id}")
        r = self.role_of(room, role)
        if r not in ("a", "b"):
            raise BadRequest("approve_summary needs role 'a' or 'b'")
        if r not in s.summary.approved_by:
            s.summary.approved_by.append(r)
            self._audit("summary_approved", s.id, by=r)
        s.touch()
        self.emit(room, "summary.ready", s.summary, persist=True, force=True)
        return s.summary

    # ------------------------------------------------------------ burn

    async def burn(self, room_id: str, *, reason: str = "burn", by: str | None = None) -> BurnDone:
        room = self.find_room(room_id)
        async with room.lock:
            s = room.session
            if s is None:
                raise NotFound(f"no session in room {room_id}")
            if s.status == "burned" and s.burn is not None:
                return s.burn
            if s.status == "live":
                self._audit("session_end", s.id, reason=reason, duration_ms=now_ms() - s.started_at)
            done = burn_store(s.store, self.audit)
            s.status = "burned"
            s.burn = done
            s.summary = None
            s.off_record = False
            s.events.clear()
            s.glossary = None
            s.glossary_uploads = Glossary()
            log.info("session %s in %s burned (%s)", s.id, room_id, reason)
            self.emit(room, "burn.done", done)
            await self.forward(room, Control(action="burn", by=by))
            await self.state_changed(room)
            return done

    async def burn_all(self) -> dict[str, Any]:
        rooms = []
        for room in list(self.rooms.values()):
            if room.session is not None and room.session.status != "burned":
                await self.burn(room.id, reason="burn_all")
                rooms.append(room.id)
        self._audit("burn_all", None, count=len(rooms))
        return {"burned": rooms, "count": len(rooms), "audit_head_hash": self.audit.head_hash()}

    async def check_idle(self, now: float | None = None) -> list[str]:
        """T-03: burn sessions idle for longer than the room's ``retention.auto_burn_minutes``."""
        now = time.monotonic() if now is None else now
        burned = []
        for room in list(self.rooms.values()):
            s = room.session
            mins = room.mode.retention.auto_burn_minutes
            if s is None or s.status == "burned" or not mins:
                continue
            if now - s.last_activity >= mins * 60:
                await self.burn(room.id, reason="auto_burn")
                burned.append(room.id)
        return burned

    # ------------------------------------------------------------ sealed mode

    async def set_sealed(self, on: bool, *, applied: bool | None = None) -> None:
        if on != self.sealed:
            self.sealed = on
            self._audit("sealed_on" if on else "sealed_off", None, applied=applied)
        for room in list(self.rooms.values()):
            if room.session is not None or room.subscribers or room.agent is not None:
                await self.state_changed(room)

    # ------------------------------------------------------------ compliance (T-05)

    def chain_report(self) -> VerifyReport | None:
        try:
            return verify_chain(self.cfg.audit_path())
        except NotImplementedError:
            v = getattr(self.audit, "verify", None)
            return v() if callable(v) else None
        except Exception as e:  # noqa: BLE001
            return VerifyReport(ok=False, lines=0, reason=str(e))

    def compliance(self, room_id: str, session_id: str | None = None) -> dict[str, Any]:
        room = self.find_room(room_id)
        s = room.session
        if session_id and (s is None or s.id != session_id):
            s = self.past.get(session_id)
            if s is None or s.room != room_id:
                raise NotFound(f"unknown session {session_id} in {room_id}")
        if s is None:
            raise NotFound(f"no session in room {room_id}")
        entries = self.audit.entries(session=s.id)

        def meta(e: dict[str, Any]) -> dict[str, Any]:
            m = e.get("meta")
            return m if isinstance(m, dict) else {}

        consents, off_record, interp, confirms, approvals, facts = [], [], [], [], [], []
        models: dict[str, Any] = {}
        started = ended = burn = None
        for e in entries:
            ev, m, ts = e.get("event"), meta(e), e.get("ts")
            if ev == "session_start":
                started = {"ts": ts, **{k: m.get(k) for k in ("room", "mode", "profile", "reason")}}
            elif ev == "consent":
                consents.append({"ts": ts, **{k: m.get(k) for k in ("role", "lang", "device", "consent_version",
                                                                     "voice")}})
            elif ev == "models":
                models = m.get("models") or {k: v for k, v in m.items()}
            elif ev == "off_record_on":
                off_record.append({"on": ts, "off": None, "by": m.get("by")})
            elif ev == "off_record_off":
                if off_record and off_record[-1]["off"] is None:
                    off_record[-1]["off"] = ts
                else:
                    off_record.append({"on": None, "off": ts, "by": m.get("by")})
            elif ev == "human_interpreter":
                interp.append({"ts": ts, "by": m.get("by"), "requested": m.get("requested", True)})
            elif ev == "confirm":
                confirms.append({"ts": ts, "by": m.get("by"), "accepted": m.get("accepted"),
                                 "flag_id": m.get("flag_id"), "via": m.get("via")})
            elif ev in ("fact_noted", "fact_asked"):
                facts.append({"ts": ts, "flag_id": m.get("flag_id"), "kinds": m.get("kinds") or [],
                              "status": "noted" if ev == "fact_noted" else "unconfirmed", "spoken": m.get("spoken")})
            elif ev == "summary_approved":
                approvals.append({"ts": ts, "by": m.get("by")})
            elif ev == "session_end":
                ended = {"ts": ts, "reason": m.get("reason"), "duration_ms": m.get("duration_ms")}
            elif ev == "key_destroyed":
                burn = {"ts": ts, "hash": e.get("hash")}
        report = self.chain_report()
        answers = {c["flag_id"]: c["accepted"] for c in confirms if c.get("flag_id") and c.get("accepted") is not None}
        for f in facts:  # an asked fact the listener answered
            if f["status"] == "unconfirmed" and f["flag_id"] in answers:
                f["status"] = "confirmed" if answers[f["flag_id"]] else "rejected"
        return {
            "kind": "raum.compliance.v1",
            "generated_at": now_ms(),
            "room": room_id,
            "mode": s.mode.id,
            "mode_name": s.mode.name,
            "session_id": s.id,
            "status": s.status,
            "profile": self.cfg.profile,
            "ai_disclosure": "AI interpreter (EU AI Act Art. 50); no recording; content encrypted (AES-256-GCM), "
                             "key in memory only",
            "started": started,
            "consents": consents,
            "models": models,
            "off_record": off_record,
            "human_interpreter": interp,
            "confirmations": confirms,
            "checked_facts": facts,  # noted / unconfirmed / confirmed / rejected, no content
            "summary_approvals": approvals,
            "ended": ended,
            "burn": burn,
            "audit": {
                "entries": len(entries),
                "head_hash": self.audit.head_hash(),
                "chain_ok": None if report is None else report.ok,
                "chain_lines": None if report is None else report.lines,
            },
            "entries": entries,
        }

    # ------------------------------------------------------------ status

    def devices(self) -> list[dict[str, Any]]:
        out = []
        for room in self.rooms.values():
            for p in room.participants.values():
                out.append({"room": room.id, **p.model_dump(mode="json")})
        return out

    async def aclose(self) -> None:
        if self._engines is not None:
            with contextlib.suppress(Exception):
                await self._engines.aclose()
        close = getattr(self._vision, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()


__all__ = [
    "AgentConn", "BadRequest", "Conflict", "MemoryAudit", "MemoryStore", "NotFound", "Room", "Session", "SessionManager",
    "Subscriber", "Unavailable", "UpstreamError",
]
