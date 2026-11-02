"""``WS /api/internal/agent/{room}``: core side of the agent link (``raum.link``).

Requires header ``x-raum-key: <network.internal_key>``. The agent sends ``hello`` and ``event``
messages; core persists content events encrypted, tracks participants, fans out to
``/api/events/{room}``, and sends ``state`` (on connect and on change) and ``control`` messages.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from raum.api.deps import core_of
from raum.events import parse_event
from raum.link import AGENT_LINK_PATH, KEY_HEADER, EventMsg, Hello, parse_link_message
from raum.modes import ModeError, mode_for_room
from raum.sessions import AgentConn

log = logging.getLogger("raum.api.internal")

router = APIRouter(tags=["internal"])


@router.websocket(AGENT_LINK_PATH)
async def agent_link(ws: WebSocket, room: str) -> None:
    core = core_of(ws)
    key = ws.headers.get(KEY_HEADER, "")
    if not secrets.compare_digest(key.encode(), core.cfg.network.internal_key.encode()):
        log.warning("agent link for %s rejected: bad %s", room, KEY_HEADER)
        await ws.close(code=4401, reason="bad key")
        return
    try:
        mode_for_room(room)
    except ModeError:
        await ws.close(code=4404, reason="unknown room")
        return
    await ws.accept()
    conn = AgentConn(ws.send_text)
    await core.sessions.attach_agent(room, conn)
    log.info("agent linked to %s", room)
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            raw = msg.get("text")
            if raw is None and msg.get("bytes") is not None:
                raw = msg["bytes"].decode("utf-8", errors="replace")
            if not raw:
                continue
            try:
                link = parse_link_message(raw)
                if isinstance(link, Hello):
                    conn.agent_id = link.agent
                    continue
                if not isinstance(link, EventMsg):
                    log.debug("ignoring %s message from the agent in %s", link.kind, room)
                    continue
                env = parse_event(link.event.model_dump(mode="json"))
            except (ValueError, ValidationError) as e:
                log.warning("bad link message from the agent in %s: %s", room, str(e)[:200])
                continue
            if env.room != room:
                log.warning("agent in %s sent an event for %s; dropped", room, env.room)
                continue
            await core.sessions.ingest(room, env)
    except WebSocketDisconnect:
        pass
    finally:
        core.sessions.detach_agent(room, conn)
        log.info("agent link to %s closed", room)
