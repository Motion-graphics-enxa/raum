"""Modes, rooms, control, presence, end/summary, burn, compliance export."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from raum.api.deps import CoreDep
from raum.events import BurnDone, Control, Envelope, SummaryReady
from raum.modes import ModeError, load_modes, mode_for_room

router = APIRouter(prefix="/api", tags=["rooms"])


class CreateRoom(BaseModel):
    mode: str = Field(examples=["sprechzimmer"])


class Presence(BaseModel):
    identity: str
    rtt_ms: int | None = Field(None, ge=0, le=60_000)


class Approve(BaseModel):
    role: str = Field(examples=["a"])


@router.get("/modes")
async def modes() -> list[dict[str, Any]]:
    """Every room config in ``modes/*.yaml``."""
    return [m.model_dump(mode="json") for m in load_modes().values()]


@router.get("/rooms")
async def list_rooms(core: CoreDep) -> list[dict[str, Any]]:
    """Known rooms: mode, participants, status (idle|live|ended|burned), agent connected."""
    return [core.sessions.room_info(r) for r in core.sessions.rooms.values()]


@router.post("/rooms")
async def create_room(body: CreateRoom, core: CoreDep) -> dict[str, str]:
    try:
        room = core.sessions.create_room(body.mode)
    except ModeError as e:
        raise HTTPException(404, str(e)) from e
    return {"room": room.id, "mode": room.mode.id}


@router.get("/rooms/{room}")
async def get_room(room: str, core: CoreDep) -> dict[str, Any]:
    """Mode and current session state of any valid room id (state is null before the first join)."""
    mode = mode_for_room(room)
    r = core.sessions.rooms.get(room)
    return {
        "room": room,
        "mode": mode.model_dump(mode="json"),
        "state": core.sessions.state(r).model_dump(mode="json") if r and r.session else None,
        "agent_connected": bool(r and r.agent),
    }


@router.get("/rooms/{room}/events")
async def backlog(room: str, core: CoreDep, since: int | None = None) -> list[Envelope]:
    """Buffered envelopes of the current session with ``ts > since`` (event API backlog)."""
    mode_for_room(room)
    r = core.sessions.rooms.get(room)
    return core.sessions.backlog(r, since) if r else []


@router.post("/rooms/{room}/control")
async def control(room: str, body: Control, core: CoreDep) -> dict[str, Any]:
    """Device actions: off_record, confirm, repeat_back, explain_simply, explain_approve, correct_term,
    human_interpreter, end, approve_summary, burn, mute."""
    return await core.sessions.control(room, body)


class ParticipantUpdate(BaseModel):
    identity: str
    lang: str | None = None
    audio_out: Literal["headphones", "speaker", "room"] | None = None
    voice: Literal["own", "standard"] | None = None
    original_voice: bool | None = None  # call mode: this device plays the other person's own voice (livevoice.py)


@router.post("/rooms/{room}/participant")
async def participant(room: str, body: ParticipantUpdate, core: CoreDep) -> dict[str, Any]:
    """Change a joined device's language, voice output or voice (C-02 one tap, C-14, C-13). The agent picks
    it up from the next utterance via the participant's LiveKit metadata; ``voice: standard`` drops the
    device's voice sample at once."""
    lang = body.lang.lower().split("-")[0] if body.lang else None
    p = await core.sessions.update_participant(room, body.identity, lang=lang, audio_out=body.audio_out,
                                               voice=body.voice, original_voice=body.original_voice)
    return p.model_dump(mode="json")


@router.post("/rooms/{room}/presence")
async def presence(room: str, body: Presence, core: CoreDep) -> dict[str, Any]:
    """Phone heartbeat with its connection latency (G-03 badge)."""
    p = await core.sessions.presence(room, body.identity, body.rtt_ms)
    return p.model_dump(mode="json")


@router.post("/rooms/{room}/end", response_model=SummaryReady)
async def end(room: str, core: CoreDep) -> SummaryReady:
    """End the conversation: bilingual summary from the decrypted records (< 10 s, C-11)."""
    return await core.sessions.end(room)


@router.get("/rooms/{room}/summary", response_model=SummaryReady)
async def summary(room: str, core: CoreDep) -> SummaryReady:
    return core.sessions.summary(room)


@router.post("/rooms/{room}/summary/approve", response_model=SummaryReady)
async def approve(room: str, body: Approve, core: CoreDep) -> SummaryReady:
    return await core.sessions.approve_summary(room, body.role)


@router.post("/rooms/{room}/burn", response_model=BurnDone)
async def burn(room: str, core: CoreDep) -> BurnDone:
    """Burn by key deletion (T-02). The next join starts a fresh session."""
    return await core.sessions.burn(room)


@router.get("/rooms/{room}/compliance")
async def compliance(room: str, core: CoreDep, session: str | None = None) -> dict[str, Any]:
    """T-05 export from the audit log: consent, models, off-record times, approvals, burn. No content."""
    return core.sessions.compliance(room, session)
