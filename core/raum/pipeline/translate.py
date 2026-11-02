"""Translation (plan §6.3, §20.1): ``mock`` (canned lines) and ``ollama`` (Gemma 4, streamed)."""

from __future__ import annotations

import json
import logging
import random
from collections.abc import AsyncIterator
from typing import Any

import httpx

from raum.pipeline import mockdata
from raum.pipeline._util import engine_options, lang_name, load_prompt, render_prompt, sample_ms, sleep_ms
from raum.pipeline.base import TranslateRequest

log = logging.getLogger("raum.pipeline.translate")

OLLAMA_OPTIONS = {"temperature", "num_predict", "top_p", "top_k", "min_p", "num_ctx", "repeat_penalty",
                  "seed", "stop", "num_batch"}


class MockTranslator:
    """Finds the canned line by its source text and streams the target text word by word.

    Latency: ``first_token_ms`` before the first word, then ``token_ms`` per word. Lines with a
    ``mistranslate`` entry come out wrong on purpose (the red path). Unknown text -> ``[<tgt>] <text>``.
    """

    is_mock = True

    def __init__(self, options: dict[str, Any] | None = None, *, model: str | None = None,
                 rng: random.Random | None = None) -> None:
        self.options = options or {}
        self.name = model or "mock-gemma"
        self._rng = rng or random.Random()

    def render(self, req: TranslateRequest) -> str:
        mode = req.turn.mode if req.turn else None
        m = mockdata.find(req.text, mode)
        if m is None:
            return f"[{req.tgt_lang}] {req.text}"
        return m.line.target(req.tgt_lang)

    async def stream(self, req: TranslateRequest) -> AsyncIterator[str]:
        words = self.render(req).split(" ")
        await sleep_ms(sample_ms(self.options.get("first_token_ms"), rng=self._rng))
        yield words[0]
        token_ms = self.options.get("token_ms", 0)
        for w in words[1:]:
            await sleep_ms(sample_ms(token_ms, rng=self._rng))
            yield " " + w


def translate_system_prompt(template: str, req: TranslateRequest) -> str:
    """The system message. Only room-level values, the direction and the (slow-changing) names list go
    in, so it stays byte-identical across turns of a room and the runtime can reuse its prompt cache."""
    return render_prompt(
        template,
        room_name=req.room_name, role_a=req.role_a, role_b=req.role_b,
        lang_a=lang_name(req.lang_a), lang_b=lang_name(req.lang_b),
        src_lang=lang_name(req.src_lang), tgt_lang=lang_name(req.tgt_lang),
        register=req.register, glossary=req.glossary or "(none)", context=req.context or "(none)",
    )


class OllamaTranslator:
    """``POST {url}/api/chat`` with ``stream: true``; prompt from ``prompts/translate.md``."""

    is_mock = False

    def __init__(self, url: str, *, model: str, options: dict[str, Any] | None = None,
                 prompt_path: str = "prompts/translate.md", client: httpx.AsyncClient | None = None) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.name = f"ollama:{model}"
        self.options = options or {}
        self.prompt_path = prompt_path
        self._own = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(float(self.options.get("timeout_s", 30.0)), connect=2.0))

    def body(self, req: TranslateRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": translate_system_prompt(load_prompt(self.prompt_path), req)},
                {"role": "user", "content": req.text},
            ],
            "stream": True,
            "keep_alive": -1,
            "options": engine_options(self.options, OLLAMA_OPTIONS),
        }
        if "think" in self.options:
            body["think"] = self.options["think"]
        return body

    async def stream(self, req: TranslateRequest) -> AsyncIterator[str]:
        started = False
        async with self._client.stream("POST", f"{self.url}/api/chat", json=self.body(req)) as r:
            if r.status_code >= 400:
                await r.aread()
                raise RuntimeError(f"ollama {r.status_code}: {r.text[:200]}")
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                j = json.loads(line)
                if j.get("error"):
                    raise RuntimeError(f"ollama: {j['error']}")
                content = (j.get("message") or {}).get("content") or ""
                if content and not started:
                    content = content.lstrip()
                    started = bool(content)
                if content:
                    yield content
                if j.get("done"):
                    break

    async def warmup(self) -> None:
        """Load the model (keep_alive -1) with an empty request (plan §6.4: models warmed at boot)."""
        r = await self._client.post(f"{self.url}/api/chat",
                                    json={"model": self.model, "messages": [], "keep_alive": -1})
        r.raise_for_status()

    async def aclose(self) -> None:
        if self._own:
            await self._client.aclose()
