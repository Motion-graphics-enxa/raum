"""``WS /api/events/{room}``: the table screen's and the event API's live feed.

On connect: the session's buffered envelopes (``?since=<ts>`` to skip older ones), then the current
``session.state``, then every new envelope (agent and core-originated) plus ``privacy.meter`` and
``gpu.stats`` every second. The client may send the text ``ping``; the answer is the text ``pong`` (an
app-level heartbeat: a phone's socket can look open after the OS dropped it). Anything else from the
client is ignored. uvicorn also sends protocol pings (``raum core``: every 20 s, 20 s to answer).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from raum.api.deps import core_of
from raum.events import make_event
from raum.modes import ModeError
from raum.sessions import Subscriber

log = logging.getLogger("raum.api.events")

router = APIRouter(tags=["events"])

PING = "ping"
PONG = "pong"


async def _pump(send: Callable[[str], Awaitable[None]], sub: Subscriber) -> None:
    while True:
        env = await sub.queue.get()
        await send(env.to_json())


@router.websocket("/api/events/{room}")
async def events(ws: WebSocket, room: str, since: int | None = None) -> None:
    core = core_of(ws)
    try:
        r = core.sessions.room(room)
    except ModeError:
        await ws.close(code=4404, reason="unknown room")
        return
    await ws.accept()
    lock = asyncio.Lock()  # the pump and the pong share the socket

    async def send(text: str) -> None:
        async with lock:
            await ws.send_text(text)

    sub, backlog = core.sessions.subscribe(room, since)
    pump: asyncio.Task[None] | None = None
    try:
        for env in backlog:
            await send(env.to_json())
        await send(make_event("session.state", room, core.sessions.state(r)).to_json())
        pump = asyncio.create_task(_pump(send, sub))
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if pump.done():
                break
            if msg.get("text") == PING:
                await send(PONG)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        core.sessions.unsubscribe(room, sub)
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
