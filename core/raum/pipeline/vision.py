"""Document camera (F-07): ``mock`` (canned documents) and ``ollama`` (Gemma 4 vision).

    reading = await vision.read(image_bytes, lang="ar", mode="sprechzimmer")

The engine reads a photographed letter, contract or term sheet and answers in the reader's language:
a title, a plain-language account and every date, amount, dose and deadline (as printed and in the
reader's language). The image only ever lives in memory: it is base64-encoded into one request
body and dropped when the call returns.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import yaml

from raum.pipeline._ollama import warm_request
from raum.pipeline._util import (
    engine_options,
    lang_code,
    lang_name,
    load_prompt,
    render_prompt,
    sample_ms,
    sleep_ms,
)
from raum.pipeline.translate import OLLAMA_OPTIONS
from raum.pipeline.verifier import parse_json_object

log = logging.getLogger("raum.pipeline.vision")

PROMPT = "prompts/document.md"
FACT_KINDS = ("date", "deadline", "amount", "dose", "time", "duration", "percent", "number")
_KIND_ALIASES = {"frist": "deadline", "due": "deadline", "due_date": "deadline", "money": "amount", "price": "amount",
                 "sum": "amount", "medication": "dose", "dosage": "dose", "appointment": "date"}

# Ollama structured output: the model can only answer in this shape.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "source_lang": {"type": "string"},
        "text": {"type": "string"},
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["date", "deadline", "amount", "dose"]},
                    "value": {"type": "string"},
                    "source": {"type": "string"},
                    "uncertain": {"type": "boolean"},
                },
                "required": ["kind", "value"],
            },
        },
    },
    "required": ["title", "text", "facts"],
}


@dataclass
class DocumentReading:
    title: str
    text: str
    facts: list[dict[str, Any]] = field(default_factory=list)  # {kind, value, source?, uncertain?}
    source_lang: str | None = None


def _kind(v: Any) -> str:
    k = str(v or "").strip().lower().replace(" ", "_")
    k = _KIND_ALIASES.get(k, k)
    return k if k in FACT_KINDS else "number"


def _clean_fact(f: Any) -> dict[str, Any] | None:
    if isinstance(f, str):
        f = {"kind": "number", "value": f}
    if not isinstance(f, dict):
        return None
    value = str(f.get("value") or f.get("source") or "").strip()
    if not value:
        return None
    source = f.get("source")
    unc = f.get("uncertain")
    return {
        "kind": _kind(f.get("kind")),
        "value": value,
        "source": str(source).strip() if source not in (None, "") else None,
        "uncertain": unc is True or str(unc).lower() in ("true", "yes", "1"),
    }


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_reading(raw: str | dict[str, Any]) -> DocumentReading:
    """Tolerant parse of the model's answer: JSON (optionally in a code fence) or, failing that, the
    whole answer as the text with no facts."""
    if isinstance(raw, dict):
        d: dict[str, Any] | None = raw
    else:
        body = _FENCE.sub("", raw or "").strip()
        try:
            d = parse_json_object(body) if body else None
        except (ValueError, TypeError):
            d = None
        if d is None:
            return DocumentReading(title="", text=body)
    raw_facts = d.get("facts")
    facts = [c for c in map(_clean_fact, raw_facts if isinstance(raw_facts, list) else []) if c]
    src =lang_code(str(d.get("source_lang") or "")) if d.get("source_lang") else None
    return DocumentReading(title=str(d.get("title") or "").strip(), text=str(d.get("text") or "").strip(),
                           facts=facts, source_lang=src)


# ---------------------------------------------------------------- mock


@lru_cache(maxsize=1)
def _mock_documents() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "mockdata" / "documents.yaml"
    with path.open(encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("documents", {})


def _pick(m: dict[str, str] | str | None, lang: str) -> tuple[str, bool]:
    """Text in ``lang``; ``(en text, False)`` when the canned document has no such language."""
    if isinstance(m, str) or m is None:
        return m or "", True
    if lang in m:
        return m[lang], True
    return m.get("en") or next(iter(m.values()), ""), False


class MockVision:
    """Returns the room's canned demo document (the doctor's letter, the dismissal letter, the term
    sheet) in the reader's language, after ``latency_ms``. The image is only checked for size."""

    is_mock = True

    def __init__(self, options: dict[str, Any] | None = None, *, model: str | None = None,
                 rng: random.Random | None = None) -> None:
        self.options = options or {}
        self.name = model or "mock-gemma-vision"
        self._rng = rng or random.Random()

    async def read(self, image: bytes, *, lang: str, mode: str | None = None) -> DocumentReading:
        if not image:
            raise ValueError("empty image")
        await sleep_ms(sample_ms(self.options.get("latency_ms"), rng=self._rng))
        docs = _mock_documents()
        doc = docs.get(mode or "") or docs["sprechzimmer"]
        title, ok = _pick(doc.get("title"), lang)
        text, _ = _pick(doc.get("text"), lang)
        prefix = "" if ok else f"[{lang}] "
        facts = []
        for f in doc.get("facts") or []:
            value, _ = _pick(f.get("value"), lang)
            facts.append({"kind": f["kind"], "value": value, "source": f.get("source"),
                          "uncertain": bool(f.get("uncertain"))})
        return DocumentReading(title=prefix + title, text=prefix + text, facts=facts,
                               source_lang=doc.get("source_lang"))


# ---------------------------------------------------------------- ollama


class OllamaVision:
    """``POST {url}/api/chat`` (non-streamed) with the photo as a base64 ``images`` entry, the prompt
    from ``prompts/document.md`` and a JSON schema as ``format``. ``think``, ``num_ctx`` and the other
    options come from the profile (use the translator's ``num_ctx``: the same gemma4 model is shared, a
    different context size would make Ollama reload it)."""

    is_mock = False

    def __init__(self, url: str, *, model: str, options: dict[str, Any] | None = None, prompt_path: str = PROMPT,
                 client: httpx.AsyncClient | None = None) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.name = f"ollama:{model}"
        self.options = options or {}
        self.prompt_path = prompt_path
        self._no_think = False
        self._own = client is None
        # a cold model or a long letter can take a while; the phone shows "reading" meanwhile
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(float(self.options.get("timeout_s", 120.0)), connect=2.0))

    def _options(self) -> dict[str, Any]:
        opts: dict[str, Any] = {"temperature": 0.1, "num_predict": 1024}
        opts.update(engine_options(self.options, OLLAMA_OPTIONS))
        return opts

    def body(self, image_b64: str, lang: str) -> dict[str, Any]:
        system = render_prompt(load_prompt(self.prompt_path), lang=lang_name(lang))
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"Read this document for me. Answer in {lang_name(lang)}.",
                 "images": [image_b64]},
            ],
            "stream": False,
            "keep_alive": -1,
            "format": SCHEMA,
            "options": self._options(),
        }
        if "think" in self.options and not self._no_think:
            body["think"] = self.options["think"]
        return body

    async def read(self, image: bytes, *, lang: str, mode: str | None = None) -> DocumentReading:
        if not image:
            raise ValueError("empty image")
        b64 = base64.b64encode(image).decode("ascii")
        try:
            r = await self._client.post(f"{self.url}/api/chat", json=self.body(b64, lang))
            if r.status_code >= 400 and "think" in r.text and not self._no_think and "think" in self.options:
                self._no_think = True  # the model has no thinking switch: drop the field and retry once
                r = await self._client.post(f"{self.url}/api/chat", json=self.body(b64, lang))
        finally:
            del b64
        if r.status_code >= 400:
            raise RuntimeError(f"ollama {r.status_code}: {r.text[:200]}")
        j = r.json()
        if j.get("error"):
            raise RuntimeError(f"ollama: {j['error']}")
        content = str((j.get("message") or {}).get("content") or "")
        try:
            return parse_reading(json.loads(content))
        except (json.JSONDecodeError, TypeError, AttributeError):
            return parse_reading(content)

    async def warmup(self) -> None:
        """Load the model (keep_alive -1, same options as the reading calls) on a long-timeout request."""
        body: dict[str, Any] = {"model": self.model, "messages": [], "keep_alive": -1, "options": self._options()}
        if "think" in self.options and not self._no_think:
            body["think"] = self.options["think"]
        await warm_request(self._client, self.url, "/api/chat", body)

    async def aclose(self) -> None:
        if self._own:
            await self._client.aclose()
