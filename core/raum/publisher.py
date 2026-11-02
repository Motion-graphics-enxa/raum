"""Publish core-originated events (session.state, confirm, summary.ready, burn.done) to the room's
LiveKit data channel via the server API (``RoomService.SendData``, topic ``raum.events``, reliable),
so they reach phones even if no agent is connected (see ``raum.link``).

Fail soft: a missing room or a LiveKit outage is logged, never raised.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import warnings
from typing import Any, Protocol

from raum.config import RaumConfig
from raum.events import EVENTS_TOPIC, Envelope

log = logging.getLogger("raum.publisher")


class Publisher(Protocol):
    def publish(self, env: Envelope) -> None:
        """Queue one envelope for the room's data channel. Never blocks, never raises."""
        ...

    async def aclose(self) -> None: ...


class NullPublisher:
    def publish(self, env: Envelope) -> None:
        pass

    async def aclose(self) -> None:
        pass


def http_url(url: str) -> str:
    if url.startswith("wss://"):
        return "https://" + url[len("wss://"):]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://"):]
    return url


class LiveKitPublisher:
    """One background sender per room, so messages keep their order per room and a slow or missing
    room never delays another.

    ``SendData`` to a room LiveKit doesn't know takes seconds to fail (503), so each send is preceded
    by a cached ``ListRooms`` check (a few ms): events for a room nobody has joined yet are dropped.
    """

    def __init__(self, cfg: RaumConfig, *, timeout_s: float = 2.0, max_queue: int = 500,
                 exists_ttl_s: float = 2.0) -> None:
        self.url = http_url(cfg.network.livekit_internal_url)
        self.key = cfg.network.livekit_api_key
        self.secret = cfg.network.livekit_api_secret
        self.timeout_s = timeout_s
        self.max_queue = max_queue
        self.exists_ttl_s = exists_ttl_s
        self._rooms: dict[str, tuple[asyncio.Queue[Envelope], asyncio.Task[None]]] = {}
        self._exists: dict[str, tuple[float, bool]] = {}
        self._down_until = 0.0
        self._api = None
        self._last_warn = 0.0
        self.sent = 0
        self.failed = 0
        self.skipped = 0

    def publish(self, env: Envelope) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        entry = self._rooms.get(env.room)
        if entry is None or entry[1].done():
            q: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=self.max_queue)
            entry = (q, loop.create_task(self._run(q), name=f"raum-livekit-{env.room}"))
            self._rooms[env.room] = entry
        try:
            entry[0].put_nowait(env)
        except asyncio.QueueFull:
            self.failed += 1
            self._warn("publisher queue full for %s, dropping %s", env.room, env.type)

    def _warn(self, msg: str, *args: object) -> None:
        now = time.monotonic()
        if now - self._last_warn > 10:
            self._last_warn = now
            log.warning(msg, *args)
        else:
            log.debug(msg, *args)

    def _client(self):
        if self._api is None:
            import aiohttp
            from livekit import api

            self._api = api.LiveKitAPI(self.url, self.key, self.secret,
                                       timeout=aiohttp.ClientTimeout(total=self.timeout_s), failover=False)
        return self._api

    async def _run(self, q: asyncio.Queue[Envelope]) -> None:
        while True:
            env = await q.get()
            await self.send(env)

    async def room_exists(self, room: str) -> bool:
        from livekit import api

        now = time.monotonic()
        hit = self._exists.get(room)
        if hit and now - hit[0] < (self.exists_ttl_s if not hit[1] else 5 * self.exists_ttl_s):
            return hit[1]
        with warnings.catch_warnings():  # livekit-server --dev uses a short secret
            warnings.filterwarnings("ignore", message=".*HMAC key.*")
            res = await self._client().room.list_rooms(api.ListRoomsRequest(names=[room]))
        ok = any(r.name == room for r in res.rooms)
        self._exists[room] = (now, ok)
        return ok

    async def send(self, env: Envelope) -> bool:
        from livekit import api

        if time.monotonic() < self._down_until:
            self.skipped += 1
            return False
        try:
            if not await self.room_exists(env.room):
                self.skipped += 1
                log.debug("livekit room %s not open yet; %s not published", env.room, env.type)
                return False
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*HMAC key.*")
                await self._client().room.send_data(api.SendDataRequest(
                    room=env.room, data=env.to_json().encode(), kind=api.DataPacket.Kind.RELIABLE,
                    topic=EVENTS_TOPIC))
            self.sent += 1
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - TwirpError (room gone), aiohttp errors (LiveKit down)
            self.failed += 1
            self._exists.pop(env.room, None)
            if not isinstance(getattr(e, "status", None), int):  # transport error: LiveKit is down
                self._down_until = time.monotonic() + 3.0
            self._warn("livekit send_data failed for %s (%s): %s", env.room, env.type, str(e) or type(e).__name__)
            return False

    async def update_participant(self, room: str, identity: str, meta: dict[str, Any]) -> bool:
        """Set the participant's metadata and attributes (same JSON as the token). Fail soft."""
        try:
            import json

            from livekit import api

            await self._client().room.update_participant(api.UpdateParticipantRequest(
                room=room, identity=identity, metadata=json.dumps(meta),
                attributes={k: str(v) for k, v in meta.items()}))
            return True
        except Exception as e:  # noqa: BLE001
            self._warn("update_participant %s/%s failed: %s", room, identity, e)
            return False

    async def aclose(self) -> None:
        tasks = [t for _, t in self._rooms.values()]
        self._rooms.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self._api is not None:
            with contextlib.suppress(Exception):
                await self._api.aclose()
            self._api = None
