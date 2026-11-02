"""GPU load and loaded models for the glass box (G-02).

``nvidia-smi`` when present (GX10 / Spark), else gently moving mock values. Models come from
``raum.yaml``; when engines are Ollama, ``/api/ps`` (short timeout, cached) lists what is loaded.
"""

from __future__ import annotations

import logging
import math
import shutil
import time

import httpx

from raum.config import RaumConfig
from raum.events import GpuStats
from raum.privacy.meter import run_cmd

log = logging.getLogger("raum.gpu")

_QUERY = ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"]
_SKIP_ENGINES = {"off", "none", "passthrough"}


def parse_nvidia_smi(out: str) -> tuple[float, float | None] | None:
    """``"41, 12345\\n"`` -> (util_pct, mem_used_gb). GB10 reports memory as ``[N/A]``."""
    utils: list[float] = []
    mem_mib = 0.0
    mem_known = False
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts or not parts[0]:
            continue
        try:
            utils.append(float(parts[0]))
        except ValueError:
            continue
        if len(parts) > 1:
            try:
                mem_mib += float(parts[1])
                mem_known = True
            except ValueError:
                pass
    if not utils:
        return None
    return sum(utils) / len(utils), (round(mem_mib / 1024, 2) if mem_known else None)


class GpuMonitor:
    def __init__(self, cfg: RaumConfig, *, ps_ttl_s: float = 3.0) -> None:
        self.cfg = cfg
        self.ps_ttl_s = ps_ttl_s
        self._smi = shutil.which("nvidia-smi") is not None
        self._smi_retry_at = 0.0
        self._ps_cache: tuple[float, list[str]] | None = None
        self.last: GpuStats | None = None

    def _ollama_urls(self) -> list[str]:
        urls: list[str] = []
        for stage in ("translate", "verify", "embed", "vision"):
            ec = getattr(self.cfg.models, stage)
            if ec.engine == "ollama":
                u = (ec.url or "http://127.0.0.1:11434").rstrip("/")
                if u not in urls:
                    urls.append(u)
        return urls

    def configured_models(self) -> list[str]:
        out: list[str] = []
        for stage in ("vad", "denoise", "asr", "translate", "verify", "embed", "vision", "tts"):
            ec = getattr(self.cfg.models, stage)
            if ec.engine in _SKIP_ENGINES:
                continue
            name = ec.model or ec.engine
            if name not in out:
                out.append(name)
        return out

    async def _ollama_loaded(self) -> list[str] | None:
        urls = self._ollama_urls()
        if not urls:
            return None
        now = time.monotonic()
        if self._ps_cache and now - self._ps_cache[0] < self.ps_ttl_s:
            return self._ps_cache[1]
        names: list[str] = []
        ok = False
        async with httpx.AsyncClient(timeout=0.5) as client:
            for u in urls:
                try:
                    r = await client.get(f"{u}/api/ps")
                    r.raise_for_status()
                    ok = True
                    for m in r.json().get("models", []) or []:
                        n = m.get("name") or m.get("model")
                        if n and n not in names:
                            names.append(n)
                except (httpx.HTTPError, ValueError) as e:
                    log.debug("ollama /api/ps at %s failed: %s", u, e)
        result = names if ok else None
        self._ps_cache = (now, result or [])
        return result

    async def models_loaded(self) -> list[str]:
        loaded = await self._ollama_loaded()
        if loaded is None:
            return self.configured_models()
        # Ollama tells us what is really in memory; keep the non-Ollama engines from the config.
        others = []
        for stage in ("vad", "denoise", "asr", "tts"):
            ec = getattr(self.cfg.models, stage)
            if ec.engine not in _SKIP_ENGINES and ec.engine != "ollama":
                others.append(ec.model or ec.engine)
        return [*others, *[n for n in loaded if n not in others]]

    async def _smi_read(self) -> tuple[float, float | None] | None:
        if not self._smi or time.monotonic() < self._smi_retry_at:
            return None
        rc, out, err = await run_cmd(*_QUERY, timeout=1.5)
        parsed = parse_nvidia_smi(out) if rc == 0 else None
        if parsed is None:
            log.debug("nvidia-smi failed (rc=%s): %s", rc, err.strip())
            self._smi_retry_at = time.monotonic() + 30
        return parsed

    @staticmethod
    def mock_util(t: float | None = None) -> float:
        t = time.time() if t is None else t
        return round(24 + 9 * math.sin(t / 6.0) + 4 * math.sin(t / 1.7), 1)

    async def read(self) -> GpuStats:
        models = await self.models_loaded()
        smi = await self._smi_read()
        if smi is not None:
            util, mem = smi
            g = GpuStats(util_pct=round(util, 1), models_loaded=models, mem_used_gb=mem, source="nvidia-smi")
        else:
            g = GpuStats(util_pct=self.mock_util(), models_loaded=models, mem_used_gb=None, source="mock")
        self.last = g
        return g
