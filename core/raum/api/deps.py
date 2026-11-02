"""Shared state of one raum-core app (``app.state.core``)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Request
from starlette.requests import HTTPConnection

from raum.config import RaumConfig
from raum.events import GpuStats, PrivacyMeter
from raum.gpu import GpuMonitor
from raum.privacy.meter import PrivacyMeterReader
from raum.privacy.seal import SealController
from raum.sessions import SessionManager


@dataclass
class Core:
    cfg: RaumConfig
    sessions: SessionManager
    meter: PrivacyMeterReader
    gpu: GpuMonitor
    seal: SealController
    tick_s: float = 1.0  # privacy.meter + gpu.stats cadence on /api/events
    idle_check_s: float = 30.0  # auto-burn sweep (T-03)
    started_at: float = field(default_factory=time.time)

    async def meter_now(self) -> PrivacyMeter:
        return await self.meter.read(self.sessions.sealed)

    async def gpu_now(self) -> GpuStats:
        return await self.gpu.read()


def core_of(conn: HTTPConnection) -> Core:
    return conn.app.state.core


def get_core(request: Request) -> Core:
    return core_of(request)


CoreDep = Annotated[Core, Depends(get_core)]
