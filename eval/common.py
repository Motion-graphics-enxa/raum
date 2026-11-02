"""Shared helpers for the eval scripts (run_eval, bakeoff, load_test, report).

Run every eval script with the core environment, from the repo root:

    uv run --project core python eval/<script>.py --help

Nothing here talks to the network unless a function is given a URL.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
SETS_DIR = EVAL_DIR / "sets"
OUT_DIR = EVAL_DIR / "out"

if str(ROOT / "core") not in sys.path:  # same trick as core/tests/conftest.py
    sys.path.insert(0, str(ROOT / "core"))
os.environ.setdefault("RAUM_HOME", str(ROOT))

PERTURBATIONS = ("wrong_number", "wrong_unit", "negation", "allergy_omitted", "date_shifted", "semantic")
GUARD_TYPES = ("wrong_number", "wrong_unit", "date_shifted")  # §12.2: "fact guard on perturbed numbers"
SEMANTIC_TYPES = ("negation", "allergy_omitted", "semantic")  # §12.2: "checker on semantic perturbations"

LANG_NAMES = {
    "de": "German", "en": "English", "ru": "Russian", "tr": "Turkish", "ar": "Arabic", "uk": "Ukrainian",
    "fa": "Persian", "pl": "Polish", "ro": "Romanian", "es": "Spanish", "fr": "French", "it": "Italian",
}


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code, code)


# ------------------------------------------------------------------ sets


def load_sets(langs: Iterable[str] | None = None, rooms: Iterable[str] | None = None,
              approved_only: bool = False, sets_dir: Path = SETS_DIR) -> list[dict[str, Any]]:
    """All records from ``eval/sets/*.jsonl``, filtered. Adds ``lang`` (the non-German side),
    ``sid`` (sentence id shared across languages) and ``direction`` ("de>xx" / "xx>de")."""
    langs = {x for x in (langs or []) if x}
    rooms = {x for x in (rooms or []) if x}
    out: list[dict[str, Any]] = []
    for path in sorted(sets_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            rec["lang"] = rec["tgt_lang"] if rec["src_lang"] == "de" else rec["src_lang"]
            rec["sid"] = rec["id"].rsplit("-", 1)[0]
            rec["direction"] = f"{rec['src_lang']}>{rec['tgt_lang']}"
            if langs and rec["lang"] not in langs:
                continue
            if rooms and rec["room"] not in rooms:
                continue
            if approved_only and not rec.get("approved"):
                continue
            out.append(rec)
    return out


def split_csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


# ------------------------------------------------------------------ fact guard (contract: raum.verify.guard)


@dataclass
class GuardOutcome:
    red: bool | None  # None = guard unavailable / errored
    mismatches: list[dict[str, Any]]
    confirm: list[dict[str, Any]]
    ms: float
    error: str | None = None


class Guard:
    """Wraps ``raum.verify.guard`` with the room's kinds, confirm_on and glossary.
    Survives the contract stub (``NotImplementedError``) so evals run before the guard lands."""

    def __init__(self) -> None:
        from raum.glossary import Glossary
        from raum.modes import load_modes

        self.modes = load_modes()
        self.glossaries = {m.id: Glossary.load(m.glossary) for m in self.modes.values()}
        self.available, self.reason = self._probe()

    @staticmethod
    def _probe() -> tuple[bool, str]:
        from raum.verify import guard

        try:
            guard.check("500 mg", "de", "500 mg", "de")
        except NotImplementedError:
            return False, "raum.verify.guard.check is not implemented yet (contract stub)"
        except Exception as e:  # noqa: BLE001 - report, don't crash the eval
            return False, f"raum.verify.guard.check failed on a trivial input: {e!r}"
        return True, ""

    def check(self, room: str, src: str, src_lang: str, tgt: str, tgt_lang: str) -> GuardOutcome:
        if not self.available:
            return GuardOutcome(None, [], [], 0.0, self.reason)
        from raum.verify import guard

        mode = self.modes[room]
        t0 = time.perf_counter()
        try:
            res = guard.check(src, src_lang, tgt, tgt_lang, kinds=mode.guard.kinds,
                              confirm_on=mode.guard.confirm_on, glossary=self.glossaries[room])
        except Exception as e:  # noqa: BLE001
            return GuardOutcome(None, [], [], (time.perf_counter() - t0) * 1000, repr(e))
        ms = (time.perf_counter() - t0) * 1000
        mism = [f.model_dump() for f in res.facts if not f.match]
        return GuardOutcome(bool(res.red), mism, [f.model_dump() for f in res.confirm], res.ms or ms)

    def extract(self, room: str, text: str, lang: str) -> list[dict[str, str]] | None:
        if not self.available:
            return None
        from raum.verify import guard

        mode = self.modes[room]
        try:
            facts = guard.extract(text, lang, kinds=mode.guard.kinds, glossary=self.glossaries[room])
        except Exception:  # noqa: BLE001
            return None
        return [{"kind": f.kind, "value": f.value} for f in facts]


def fact_recall(expected: list[dict[str, str]], found: list[dict[str, str]]) -> tuple[int, list[dict[str, str]]]:
    """How many expected facts (multiset by kind+value, case-insensitive) the guard extracted."""
    pool = defaultdict(int)
    for f in found:
        pool[(f["kind"], f["value"].casefold())] += 1
    hit, missing = 0, []
    for f in expected:
        key = (f["kind"], f["value"].casefold())
        if pool[key] > 0:
            pool[key] -= 1
            hit += 1
        else:
            missing.append(f)
    return hit, missing


# ------------------------------------------------------------------ similarity / badges (§6.9)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def badge(similarity: float | None, green: float, amber: float, guard_red: bool | None = False) -> str:
    """Fact-guard red always wins; then the similarity thresholds."""
    if guard_red:
        return "red"
    if similarity is None:
        return "grey"
    if similarity >= green:
        return "green"
    if similarity >= amber:
        return "amber"
    return "red"


def percentile(values: Iterable[float | None], q: float) -> float | None:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    k = (len(v) - 1) * q / 100
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def rate(num: int, den: int) -> float | None:
    return num / den if den else None


def fmt_pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def fmt_ms(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x:.2f} ms" if x < 10 else f"{x:.0f} ms"


# ------------------------------------------------------------------ prompts


def render_prompt(name: str, **fields: Any) -> str:
    text = (ROOT / "prompts" / name).read_text(encoding="utf-8")

    class _Keep(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    return text.format_map(_Keep(**fields)).strip()


def translate_fields(rec: dict[str, Any], mode: Any, glossary: Any) -> dict[str, str]:
    lang_b = rec["lang"]
    return dict(
        room_name=mode.display_name("en"),
        role_a=mode.label("a", "en"),
        role_b=mode.label("b", "en"),
        lang_a=lang_name("de"),
        lang_b=lang_name(lang_b),
        src_lang=lang_name(rec["src_lang"]),
        tgt_lang=lang_name(rec["tgt_lang"]),
        register=mode.register,
        glossary=glossary.prompt_lines(rec["src_lang"], rec["tgt_lang"]) if glossary else "(none)",
        context="(none)",
    )


# ------------------------------------------------------------------ Ollama (direct; bake-off and fallback)


class Ollama:
    """Minimal async Ollama client: streamed chat with timings, embeddings, tags."""

    def __init__(self, url: str = "http://127.0.0.1:11434", timeout: float = 300.0) -> None:
        import httpx

        self.url = url.rstrip("/")
        self.client = httpx.AsyncClient(base_url=self.url, timeout=httpx.Timeout(timeout, connect=5.0))

    async def aclose(self) -> None:
        await self.client.aclose()

    async def tags(self) -> list[str]:
        r = await self.client.get("/api/tags")
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    @staticmethod
    def think_for(model: str) -> Any:
        m = model.lower()
        if m.startswith("gpt-oss"):
            return "low"
        if m.startswith(("qwen3", "deepseek-r1")):
            return False
        return None

    async def chat_stream(self, model: str, system: str, user: str, options: dict[str, Any] | None = None,
                          think: Any = "auto", keep_alive: str | int | None = None) -> AsyncIterator[str]:
        body: dict[str, Any] = {
            "model": model,
            "stream": True,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": options or {},
        }
        th = self.think_for(model) if think == "auto" else think
        if th is not None:
            body["think"] = th
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        for attempt in (1, 2):
            async with self.client.stream("POST", "/api/chat", json=body) as r:
                if r.status_code == 400 and "think" in body and attempt == 1:
                    await r.aread()
                    body.pop("think")  # model does not support the think switch: retry without it
                    continue
                if r.status_code >= 400:
                    await r.aread()
                    raise RuntimeError(f"ollama {model}: HTTP {r.status_code}: {r.text[:300]}")
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    msg = json.loads(line)
                    if msg.get("error"):
                        raise RuntimeError(f"ollama {model}: {msg['error']}")
                    delta = (msg.get("message") or {}).get("content") or ""
                    if delta:
                        yield delta
                    if msg.get("done"):
                        return
            return

    async def chat(self, model: str, system: str, user: str, options: dict[str, Any] | None = None,
                   think: Any = "auto") -> str:
        return "".join([d async for d in self.chat_stream(model, system, user, options, think)]).strip()

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        r = await self.client.post("/api/embed", json={"model": model, "input": texts})
        r.raise_for_status()
        return r.json()["embeddings"]


async def timed_stream(stream: AsyncIterator[str]) -> tuple[str, float | None, float]:
    """Consume a delta stream: (text, first_token_ms, full_ms)."""
    t0 = time.perf_counter()
    first: float | None = None
    parts: list[str] = []
    async for d in stream:
        if first is None and d.strip():
            first = (time.perf_counter() - t0) * 1000
        parts.append(d)
    return "".join(parts).strip(), first, (time.perf_counter() - t0) * 1000


# Engine adapters with the raum.pipeline.base protocol shape, used when build_engines is unavailable.


class DirectTranslator:
    def __init__(self, ollama: Ollama, model: str, options: dict[str, Any] | None = None) -> None:
        self.ollama, self.model, self.options = ollama, model, dict(options or {})
        self.name = f"ollama:{model}"

    def stream(self, req: Any) -> AsyncIterator[str]:
        system = render_prompt("translate.md", room_name=req.room_name, role_a=req.role_a, role_b=req.role_b,
                               lang_a=req.lang_a, lang_b=req.lang_b, src_lang=req.src_lang,
                               tgt_lang=req.tgt_lang, register=req.register, glossary=req.glossary,
                               context=req.context)
        return self.ollama.chat_stream(self.model, system, req.text, self.options)


class DirectVerifier:
    def __init__(self, ollama: Ollama, model: str, options: dict[str, Any] | None = None) -> None:
        opts = dict(options or {})
        self.reasoning = opts.pop("reasoning", "low")
        self.ollama, self.model, self.options = ollama, model, opts
        self.name = f"ollama:{model}"

    async def back_translate(self, text: str, text_lang: str, into_lang: str, turn: Any = None) -> str:
        system = render_prompt("backtranslate.md", tgt_lang=lang_name(text_lang), src_lang=lang_name(into_lang))
        think = self.reasoning if self.model.startswith("gpt-oss") else "auto"
        return await self.ollama.chat(self.model, system, text, self.options, think=think)


class DirectEmbedder:
    def __init__(self, ollama: Ollama, model: str) -> None:
        self.ollama, self.model = ollama, model
        self.name = f"ollama:{model}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self.ollama.embed(self.model, texts)


# ------------------------------------------------------------------ output


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
