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

from raum.pipeline import stats

log = logging.getLogger("raum.pipeline.ollama")

WARMUP_TIMEOUT_S = 300.0
RELOAD_LOG_MS = 500.0  # load_duration above this: a (re)load or a wait in Ollama's scheduler, logged at INFO


def ollama_ms(j: dict[str, Any]) -> dict[str, float | None]:
    """The timings of Ollama's final response (ns) in ms, and its token counts; None where absent.

    ``load_ms`` (load_duration) is Ollama's handler start to a runner ready: a wait in its scheduler for the model
    and any (re)load. ``prompt_ms`` / ``eval_ms`` are the engine at work (a wait for a parallel slot in the runner
    and GPU contention show up here). ``server_ms`` (total_duration) is the whole request inside Ollama."""
    def ms(k: str) -> float | None:
        v = j.get(k)
        return v / 1e6 if isinstance(v, (int, float)) else None

    def n(k: str) -> float | None:
        v = j.get(k)
        return float(v) if isinstance(v, (int, float)) else None

    return {"load_ms": ms("load_duration"), "prompt_ms": ms("prompt_eval_duration"), "eval_ms": ms("eval_duration"),
            "server_ms": ms("total_duration"), "prompt_n": n("prompt_eval_count"), "eval_n": n("eval_count")}


def note_ollama(stage: str, j: dict[str, Any] | None, wall_ms: float, model: str, **extra: float | None) -> None:
    """One ``stats.note`` for an Ollama call: client wall time plus the server's own timings from ``j`` (the final
    response; None when it never came: cancelled or failed). ``queue_ms`` = wall − server (floor 0): time outside
    Ollama's handler (our client, the connection; for a stream also the consumer's pace between chunks). Ollama's
    own scheduler queue is inside ``load_ms``. Logs a ``load_ms`` over 500 ms at INFO (prime suspect: a reload)."""
    t = ollama_ms(j) if j else {}
    server = t.get("server_ms")
    queue = max(0.0, wall_ms - server) if server is not None else None
    stats.note(stage, wall_ms=wall_ms, queue_ms=queue, **t, **extra)
    load = t.get("load_ms")
    if load is not None and load > RELOAD_LOG_MS:
        log.info("ollama %s reloaded: load %.0f ms", model, load)


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
