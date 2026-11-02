"""``WS /api/events/{room}``: the table screen's and the event API's live feed.

On connect: the session's buffered envelopes (``?since=<ts>`` to skip older ones), then the current
``session.state``, then every new envelope (agent and core-originated) plus ``privacy.meter`` and
``gpu.stats`` every second. Messages from the client are ignored.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from raum.api.deps import core_of
from raum.events import make_event
from raum.modes import ModeError
from raum.sessions import Subscriber

log = logging.getLogger("raum.api.events")

router = APIRouter(tags=["events"])


async def _pump(ws: WebSocket, sub: Subscriber) -> None:
    while True:
        env = await sub.queue.get()
        await ws.send_text(env.to_json())


@router.websocket("/api/events/{room}")
async def events(ws: WebSocket, room: str, since: int | None = None) -> None:
    core = core_of(ws)
    try:
        r = core.sessions.room(room)
    except ModeError:
        await ws.close(code=4404, reason="unknown room")
        return
    await ws.accept()
    sub, backlog = core.sessions.subscribe(room, since)
    pump: asyncio.Task[None] | None = None
    try:
        for env in backlog:
            await ws.send_text(env.to_json())
        await ws.send_text(make_event("session.state", room, core.sessions.state(r)).to_json())
        pump = asyncio.create_task(_pump(ws, sub))
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if pump.done():
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        core.sessions.unsubscribe(room, sub)
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
