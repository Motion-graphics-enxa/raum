"""Privacy meter, admin status, latency, audit verify, sealed mode, burn-all, health."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from raum.api.deps import CoreDep
from raum.events import now_ms

LATENCY_KEYS = ("vad_ms", "asr_ms", "mt_first_ms", "mt_full_ms", "tts_first_ms", "check_ms")
LATENCY_WINDOW = 200  # newest pipeline.timing rows per room
BUDGET_MS = 1000  # first translated words after the speaker stopped (plan §3)

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


def percentile(xs: list[float], p: float) -> float | None:
    """Same definition as the app's ``percentile`` (store.ts): nearest rank on the sorted values."""
    v = sorted(x for x in xs if math.isfinite(x))
    if not v:
        return None
    return v[min(len(v) - 1, max(0, math.floor(p / 100 * (len(v) - 1) + 0.5)))]  # JS Math.round


def room_latency(events: Any) -> dict[str, Any]:
    rows = [e.data for e in events if e.type == "pipeline.timing"][-LATENCY_WINDOW:]
    out: dict[str, Any] = {"n": len(rows)}
    for k in LATENCY_KEYS:
        xs = [float(r[k]) for r in rows if isinstance(r.get(k), (int, float))]
        out[k] = {"n": len(xs), "p50": percentile(xs, 50), "p95": percentile(xs, 95)}
    firsts = [float(r["mt_first_ms"]) for r in rows if isinstance(r.get("mt_first_ms"), (int, float))]
    out["over_budget"] = sum(1 for x in firsts if x > BUDGET_MS)
    return out


@router.get("/admin/latency")
async def latency(core: CoreDep, room: str | None = None) -> dict[str, Any]:
    """G-04 / F-05: p50 and p95 per room of every ``pipeline.timing`` field (ms, the newest 200 rows of
    the current session's event buffer; first words are measured from the moment the speaker stopped)."""
    rooms = {}
    for r in core.sessions.rooms.values():
        if room and r.id != room:
            continue
        rooms[r.id] = room_latency(r.session.events if r.session else [])
    return {"budget_ms": BUDGET_MS, "window": LATENCY_WINDOW, "rooms": rooms}


@router.get("/admin/audit/verify")
async def audit_verify(core: CoreDep) -> dict[str, Any]:
    """T-04: recompute the audit log's SHA-256 hash chain (``raum.security.audit.verify_chain``)."""
    t0 = time.perf_counter()
    report = await asyncio.to_thread(core.sessions.chain_report)
    return {
        "ok": None if report is None else report.ok,
        "lines": None if report is None else report.lines,
        "first_bad": None if report is None else report.first_bad,
        "reason": None if report is None else report.reason,
        "head_hash": core.sessions.audit.head_hash(),
        "checked_at": now_ms(),
        "ms": round((time.perf_counter() - t0) * 1000, 1),
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
