"""Cold-load-safe Ollama warm-up.

A cold model (gpt-oss:120b is ~64 GB; 24-31 s to load on the GX10) must be loaded by a request that
waits long enough: a live call with a short client timeout closes its connection mid-load and Ollama
aborts the load ("client connection closed before llama-server finished loading"). So every engine
warms on its own long-timeout request, and in the agent the checker answers "warming up" instead of
firing short requests until its model is resident.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("raum.pipeline.ollama")

WARMUP_TIMEOUT_S = 300.0


class WarmingUp(RuntimeError):
    """The engine's model is not resident yet; the caller should degrade, not wait."""


async def warm_request(client: httpx.AsyncClient, url: str, path: str, body: dict[str, Any], *,
                       timeout_s: float = WARMUP_TIMEOUT_S) -> None:
    """POST with a per-request long timeout (overrides the engine client's short live-call timeout)."""
    r = await client.post(f"{url}{path}", json=body, timeout=httpx.Timeout(timeout_s, connect=5.0))
    if r.status_code >= 400:
        raise RuntimeError(f"ollama warm-up {r.status_code}: {r.text[:200]}")


class WarmGate:
    """Mixin state: ``ready`` flips after a successful warm-up; ``gate`` (set by the agent) makes live
    calls raise WarmingUp until then. Core (summaries) leaves the gate off and simply waits."""

    ready: bool = False
    gate: bool = False

    def _check_gate(self) -> None:
        if self.gate and not self.ready:
            raise WarmingUp(f"{getattr(self, 'name', 'model')} is still loading")
