"""raum-core: FastAPI app (tokens, rooms, sessions, events, admin). ``raum core`` runs it.

OpenAPI docs at ``/api/docs``. Routers: ``join``, ``rooms``, ``events_ws``, ``admin``, ``internal``.
Background tasks: privacy meter + GPU stats every second to ``/api/events`` subscribers, and the
auto-burn sweep (T-03).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from raum.api import admin, events_ws, internal, join, rooms
from raum.api.deps import Core
from raum.config import RaumConfig, get_config
from raum.gpu import GpuMonitor
from raum.modes import ModeError
from raum.privacy.meter import PrivacyMeterReader
from raum.privacy.seal import SealController
from raum.publisher import LiveKitPublisher
from raum.sessions import BadRequest, Conflict, NotFound, SessionManager

log = logging.getLogger("raum.api")


async def _ticker(core: Core) -> None:
    while True:
        await asyncio.sleep(core.tick_s)
        if not core.sessions.has_subscribers():
            continue
        try:
            meter, gpu = await asyncio.gather(core.meter_now(), core.gpu_now())
            core.sessions.broadcast_ephemeral("privacy.meter", meter)
            core.sessions.broadcast_ephemeral("gpu.stats", gpu)
        except Exception:  # the meter must never stop the box
            log.exception("meter/gpu tick failed")


async def _idle_sweep(core: Core) -> None:
    while True:
        await asyncio.sleep(core.idle_check_s)
        try:
            burned = await core.sessions.check_idle()
            if burned:
                log.info("auto-burned idle sessions: %s", ", ".join(burned))
        except Exception:
            log.exception("auto-burn sweep failed")


def create_app(config: RaumConfig | None = None) -> FastAPI:
    cfg = config or get_config()
    core = Core(
        cfg=cfg,
        sessions=SessionManager(cfg, publisher=LiveKitPublisher(cfg)),
        meter=PrivacyMeterReader.from_config(cfg.network.tunnel),
        gpu=GpuMonitor(cfg),
        seal=SealController(),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        tasks = [asyncio.create_task(_ticker(core), name="raum-ticker"),
                 asyncio.create_task(_idle_sweep(core), name="raum-auto-burn")]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            await core.sessions.publisher.aclose()
            await core.sessions.aclose()

    app = FastAPI(
        title="RAUM core",
        summary="Private AI interpreter box: tokens, rooms, sessions, events, admin.",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.core = core

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ModeError)
    async def _mode_error(request: Request, exc: ModeError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(Conflict)
    async def _conflict(request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(BadRequest)
    async def _bad_request(request: Request, exc: BadRequest) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    for r in (join.router, rooms.router, events_ws.router, admin.router, internal.router):
        app.include_router(r)
    return app


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="raum core", description="Run raum-core (FastAPI).")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true", help="restart on code changes (development)")
    p.add_argument("--log-level", default="info")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn

    # proxy headers are handled in raum.api.join (X-Forwarded-For trusted from loopback only)
    uvicorn.run("raum.api.server:create_app", factory=True, host=args.host, port=args.port, reload=args.reload,
                log_level=args.log_level, proxy_headers=False)
    return 0
