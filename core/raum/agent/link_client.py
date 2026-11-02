"""Agent -> core link (``raum.link``): one WebSocket per room to raum-core.

    link = LinkClient(room, core_url=cfg.network.core_url, key=cfg.network.internal_key, agent_id=...,
                      on_state=pipeline.set_state, on_control=pipeline.handle_control)
    link.start()
    link.send_event(env)      # never blocks; buffered (bounded) while core is down
    await link.aclose()

Reconnects with exponential backoff. Core being down never blocks or crashes the pipeline: events
are kept in a bounded buffer (oldest dropped) and flushed on reconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from raum.events import Control, Envelope, SessionState
from raum.link import AGENT_LINK_PATH, KEY_HEADER, ControlMsg, EventMsg, Hello, StateMsg, parse_link_message

log = logging.getLogger("raum.agent.link")

OnState = Callable[[SessionState], Awaitable[None] | None]
OnControl = Callable[[Control], Awaitable[None] | None]
OnUttSeq = Callable[[int], None]


def link_url(core_url: str, room: str) -> str:
    base = core_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return base + AGENT_LINK_PATH.format(room=room)


class LinkClient:
    def __init__(self, room: str, *, core_url: str, key: str, agent_id: str, on_state: OnState | None = None,
                 on_control: OnControl | None = None, on_utt_seq: OnUttSeq | None = None, max_buffer: int = 500,
                 min_backoff: float = 0.5, max_backoff: float = 10.0) -> None:
        self.room = room
        self.url = link_url(core_url, room)
        self.key = key
        self.agent_id = agent_id
        self.on_state = on_state
        self.on_control = on_control
        self.on_utt_seq = on_utt_seq
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self._buf: deque[Envelope] = deque(maxlen=max_buffer)
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.connected = False
        self.dropped = 0
        self.sent = 0
        self._failures = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"raum-link-{self.room}")

    def send_event(self, env: Envelope) -> None:
        if self._closed:
            return
        if len(self._buf) == self._buf.maxlen:
            self.dropped += 1
        self._buf.append(env)
        self._wake.set()

    async def aclose(self) -> None:
        self._closed = True
        self._wake.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    # ---------------------------------------------------------------- internals

    async def _run(self) -> None:
        from websockets.asyncio.client import connect

        backoff = self.min_backoff
        while not self._closed:
            try:
                async with connect(self.url, additional_headers={KEY_HEADER: self.key}, open_timeout=3,
                                   proxy=None, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                    self.connected = True
                    if self._failures:
                        log.info("link to core up again (%s)", self.url)
                    else:
                        log.info("link to core up (%s)", self.url)
                    self._failures = 0
                    backoff = self.min_backoff
                    await ws.send(Hello(room=self.room, agent=self.agent_id).model_dump_json())
                    sender = asyncio.create_task(self._send_loop(ws))
                    receiver = asyncio.create_task(self._recv_loop(ws))
                    try:
                        await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
                    finally:  # also on cancel (room closing): never leave a loop running on a dead socket
                        for t in (sender, receiver):
                            t.cancel()
                        results = await asyncio.gather(sender, receiver, return_exceptions=True)
                    errors = [r for r in results if isinstance(r, Exception)]  # every exception retrieved
                    if errors:
                        raise errors[0]
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  core down / restarting: retry, never crash the agent
                self._failures += 1
                if self._failures == 1:
                    log.warning("link to core unavailable (%s: %s); retrying with backoff, events buffered",
                                e.__class__.__name__, e)
                else:
                    log.debug("link retry %d failed: %s", self._failures, e)
            finally:
                self.connected = False
            if self._closed:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)

    async def _send_loop(self, ws: Any) -> None:
        while True:
            while self._buf:
                env = self._buf[0]
                await ws.send(EventMsg(event=env).model_dump_json())
                self._buf.popleft()  # only after a successful send
                self.sent += 1
            self._wake.clear()
            if not self._buf:
                await self._wake.wait()

    async def _recv_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                msg = parse_link_message(raw)
            except Exception as e:  # noqa: BLE001
                log.warning("bad link message from core: %s", e)
                continue
            try:
                if isinstance(msg, StateMsg):
                    if msg.utt_seq and self.on_utt_seq:
                        self.on_utt_seq(msg.utt_seq)
                    if self.on_state:
                        await _maybe_await(self.on_state(msg.state))
                elif isinstance(msg, ControlMsg) and self.on_control:
                    await _maybe_await(self.on_control(msg.control))
            except Exception:
                log.exception("link handler for %s failed", msg.kind)


async def _maybe_await(v: Any) -> None:
    if inspect.isawaitable(v):
        await v
