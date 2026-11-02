"""Meaning comparison embeddings (plan §6.3, §6.9): ``mock`` and ``ollama`` (bge-m3)."""

from __future__ import annotations

import hashlib
import math
from typing import Any

import httpx

from raum.pipeline._ollama import WarmGate, warm_request
from raum.pipeline._util import sample_ms, sleep_ms


class MockEmbedder:
    """Deterministic hashed character-trigram vectors (L2-normalised). Same text -> same vector on
    every machine (blake2b, not Python's salted ``hash``). Similar spellings -> high cosine."""

    is_mock = True

    def __init__(self, options: dict[str, Any] | None = None, *, model: str | None = None, dim: int = 512) -> None:
        self.options = options or {}
        self.name = model or "mock-bge-m3"
        self.dim = int(self.options.get("dim", dim))

    def vector(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        s = f"  {text.casefold().strip()}  "
        for i in range(len(s) - 2):
            h = hashlib.blake2b(s[i:i + 3].encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            v[idx] += sign
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        await sleep_ms(sample_ms(self.options.get("latency_ms"), 0.0))
        return [self.vector(t) for t in texts]


class OllamaEmbedder(WarmGate):
    """``POST {url}/api/embed`` -> ``{"embeddings": [[...], ...]}``."""

    is_mock = False

    def __init__(self, url: str, *, model: str, options: dict[str, Any] | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.name = f"ollama:{model}"
        self.options = options or {}
        self._own = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(float(self.options.get("timeout_s", 15.0)), connect=2.0))

    async def warmup(self) -> None:
        await warm_request(self._client, self.url, "/api/embed", {"model": self.model, "input": ["warm"], "keep_alive": -1})
        self.ready = True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self._check_gate()
        r = await self._client.post(f"{self.url}/api/embed",
                                    json={"model": self.model, "input": texts, "keep_alive": -1})
        if r.status_code >= 400:
            raise RuntimeError(f"ollama embed {r.status_code}: {r.text[:200]}")
        j = r.json()
        emb = j.get("embeddings")
        if not isinstance(emb, list) or len(emb) != len(texts):
            raise RuntimeError(f"ollama embed: unexpected response {str(j)[:200]}")
        return [[float(x) for x in e] for e in emb]

    async def aclose(self) -> None:
        if self._own:
            await self._client.aclose()
