"""Privacy meter, admin status, sealed mode, burn-all, health."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from raum.api.deps import CoreDep

router = APIRouter(prefix="/api", tags=["admin"])


class Seal(BaseModel):
    on: bool


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("raum")
    except Exception:  # noqa: BLE001 - not installed as a package
        return "0.1.0"


@router.get("/privacy")
async def privacy(core: CoreDep) -> dict[str, Any]:
    """T-06: bytes to third parties (nft counters), tunnel bytes, sealed state."""
    m = await core.meter_now()
    return {**m.model_dump(mode="json"), "seal": core.seal.status()}


@router.get("/admin/status")
async def status(core: CoreDep) -> dict[str, Any]:
    s = core.sessions
    gpu = await core.gpu_now()
    meter = await core.meter_now()
    return {
        "profile": core.cfg.profile,
        "version": _version(),
        "uptime_s": round(time.time() - core.started_at, 1),
        "rooms": [s.room_info(r) for r in s.rooms.values()],
        "devices": s.devices(),
        "models": core.cfg.engine_names(),
        "gpu": gpu.model_dump(mode="json"),
        "meter": meter.model_dump(mode="json"),
        "sealed": s.sealed,
        "seal": core.seal.status(),
        "agents": {r.id: r.agent is not None for r in s.rooms.values()},
        "audit": {"head_hash": s.audit.head_hash()},
    }


@router.post("/admin/seal")
async def seal(body: Seal, core: CoreDep) -> dict[str, Any]:
    """T-07: drop all outbound traffic from RAUM services (nft rule ``raum-seal``)."""
    res = await core.seal.set(body.on)
    await core.sessions.set_sealed(body.on, applied=res.get("applied"))
    return res


@router.post("/admin/burn-all")
async def burn_all(core: CoreDep) -> dict[str, Any]:
    return await core.sessions.burn_all()


@router.get("/health")
async def health(core: CoreDep) -> dict[str, Any]:
    return {"ok": True, "service": "raum-core", "profile": core.cfg.profile, "version": _version(),
            "rooms": len(core.sessions.rooms), "uptime_s": round(time.time() - core.started_at, 1),
            "features": {"document_camera": core.cfg.models.vision.enabled}}
