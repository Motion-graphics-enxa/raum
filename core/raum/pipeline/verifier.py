"""Second model (plan §6.3, §6.9, §20.2-20.5): ``mock`` and ``ollama`` (gpt-oss, a different family
from the translator so its errors are independent).

Used for back-translation (the second check), repeat-back, explain simply and summaries.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any

import httpx

from raum.pipeline import mockdata
from raum.pipeline._ollama import WarmGate, note_ollama, warm_request
from raum.pipeline._util import engine_options, lang_name, load_prompt, render_prompt, sample_ms, sleep_ms
from raum.pipeline.base import TurnContext
from raum.pipeline.translate import OLLAMA_OPTIONS

log = logging.getLogger("raum.pipeline.verifier")

_NUM = re.compile(r"\d+(?:[.,]\d+)?")

# gpt-oss:20b's low reasoning ate a 64-token floor: 37.5 % of the checks hit it and retried at 256 (p50 2.5 s vs
# 0.9 s); with 128, 6 of the same 320 recorded checks would retry (eval/results/2026-11-03/checker-20b)
BT_NUM_PREDICT_MIN = 128
BT_NUM_PREDICT_MAX = 256


def backtranslate_num_predict(text: str, ceiling: int = BT_NUM_PREDICT_MAX) -> int:
    """Token budget for one back-translation: about twice the text's token estimate (len / 3), at least
    128 (room for the low reasoning), at most 256 or the profile's own ``num_predict``."""
    ceiling = min(ceiling, BT_NUM_PREDICT_MAX)
    return min(ceiling, max(BT_NUM_PREDICT_MIN, 2 * -(-len(text) // 3)))


class MockVerifier:
    """Back-translates by mapping the text back to its canned line, keeping any mistranslation
    (so "40 mg" comes back as "40 mg"). Latency from ``latency_ms``."""

    is_mock = True

    def __init__(self, options: dict[str, Any] | None = None, *, model: str | None = None,
                 rng: random.Random | None = None) -> None:
        self.options = options or {}
        self.name = model or "mock-gpt-oss"
        self._rng = rng or random.Random()

    async def _delay(self) -> None:
        await sleep_ms(sample_ms(self.options.get("latency_ms"), rng=self._rng))

    async def back_translate(self, text: str, text_lang: str, into_lang: str,
                             turn: TurnContext | None = None) -> str:
        await self._delay()
        m = mockdata.find(text, turn.mode if turn else None)
        if m is None:
            span = mockdata.locate(text, turn.mode if turn else None)  # a segment (simultaneous mode)
            if span is not None:
                return span.render(into_lang, faithful=not span.mistranslated)
            return re.sub(r"^\[[a-z]{2,3}\]\s*", "", text)
        if m.mistranslated:
            return m.line.target(into_lang)
        return m.line.target(into_lang, faithful=True)

    async def simple_version(self, text: str, src_lang: str, tgt_lang: str, mode: str | None = None) -> str:
        """Canned plain-language version (explain simply, K-02); the faithful translation if none."""
        await self._delay()
        m = mockdata.find(text, mode)
        plain = m.line.plain(tgt_lang) if m else None
        if plain:
            return plain
        return m.line.target(tgt_lang, faithful=True) if m else text

    async def complete_json(self, prompt: str, *, purpose: str) -> dict[str, Any]:
        await self._delay()
        if purpose == "repeat_back":
            inst = re.search(r"Instruction[^:]*:\s*(.+)", prompt)
            rest = re.search(r"restatement[^:]*:\s*(.+)", prompt, re.IGNORECASE)
            a = _NUM.findall(inst.group(1)) if inst else []
            b = _NUM.findall(rest.group(1)) if rest else []
            missing = [x for x in a if x not in b]
            wrong = [x for x in b if x not in a]
            understood = "yes" if not missing and not wrong else ("partly" if len(missing) < len(a) else "no")
            return {"understood": understood, "missing": missing, "wrong": wrong}
        return {}

    async def complete_text(self, prompt: str, *, purpose: str) -> str:
        await self._delay()
        return ""


class OllamaVerifier(WarmGate):
    """``POST {url}/api/chat`` (non-streamed). ``options.reasoning`` -> ``think`` for gpt-oss."""

    is_mock = False

    def __init__(self, url: str, *, model: str, options: dict[str, Any] | None = None,
                 backtranslate_prompt: str = "prompts/backtranslate.md",
                 client: httpx.AsyncClient | None = None) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.name = f"ollama:{model}"
        self.options = options or {}
        self.backtranslate_prompt = backtranslate_prompt
        self._own = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(float(self.options.get("timeout_s", 30.0)), connect=2.0))

    def _body(self, messages: list[dict[str, str]], *, fmt: str | None = None,
              num_predict: int | None = None) -> dict[str, Any]:
        opts = {"temperature": 0.0, "num_predict": 256}
        opts.update(engine_options(self.options, OLLAMA_OPTIONS))
        if num_predict is not None:
            opts["num_predict"] = num_predict
        body: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False,
                                "keep_alive": -1, "options": opts}
        reasoning = self.options.get("reasoning")
        if "think" in self.options:
            body["think"] = self.options["think"]
        elif reasoning and "gpt-oss" in self.model:
            body["think"] = reasoning
        if fmt:
            body["format"] = fmt
        return body

    async def warmup(self) -> None:
        """Load gpt-oss on a long-timeout request (keep_alive -1, same options as live calls)."""
        body = self._body([])
        body.pop("format", None)
        await warm_request(self._client, self.url, "/api/chat", body)
        self.ready = True

    async def _post(self, messages: list[dict[str, str]], *, fmt: str | None = None,
                    num_predict: int | None = None, purpose: str | None = None) -> dict[str, Any]:
        """Stage ``check`` (back-translation) or ``check.<purpose>`` (pipeline/stats.py): ``wall_ms`` and the
        server timings (``_ollama.note_ollama``); ``error=1`` / ``cancelled=1`` when no answer came."""
        self._check_gate()
        t0 = time.monotonic()
        j: dict[str, Any] | None = None
        end: dict[str, float | None] = {}
        try:
            r = await self._client.post(f"{self.url}/api/chat",
                                        json=self._body(messages, fmt=fmt, num_predict=num_predict))
            if r.status_code >= 400:
                raise RuntimeError(f"ollama {r.status_code}: {r.text[:200]}")
            j = r.json()
            if j.get("error"):
                raise RuntimeError(f"ollama: {j['error']}")
            return j
        except Exception:
            end["error"] = 1
            raise
        except BaseException:
            end["cancelled"] = 1
            raise
        finally:
            note_ollama(f"check.{purpose}" if purpose else "check", j if not end else None,
                        (time.monotonic() - t0) * 1000.0, self.model, **end)

    @staticmethod
    def _content(j: dict[str, Any]) -> str:
        return str((j.get("message") or {}).get("content") or "").strip()

    async def _chat(self, messages: list[dict[str, str]], *, fmt: str | None = None,
                    purpose: str | None = None) -> str:
        return self._content(await self._post(messages, fmt=fmt, purpose=purpose))

    async def back_translate(self, text: str, text_lang: str, into_lang: str,
                             turn: TurnContext | None = None) -> str:
        system = render_prompt(load_prompt(self.backtranslate_prompt),
                               tgt_lang=lang_name(text_lang), src_lang=lang_name(into_lang))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": text}]
        full = min(int(self.options.get("num_predict") or BT_NUM_PREDICT_MAX), BT_NUM_PREDICT_MAX)
        cap = backtranslate_num_predict(text, full)
        j = await self._post(messages, num_predict=cap)
        if j.get("done_reason") == "length" and cap < full:
            # the (low) reasoning ate the short budget: once more with the full one rather than a cut-off line
            log.info("back-translation hit num_predict=%d; retrying at %d", cap, full)
            j = await self._post(messages, num_predict=full)
        return self._content(j)

    async def complete_json(self, prompt: str, *, purpose: str) -> dict[str, Any]:
        raw = await self._chat([{"role": "user", "content": prompt}], fmt="json", purpose=purpose)
        return parse_json_object(raw)

    async def complete_text(self, prompt: str, *, purpose: str) -> str:
        return await self._chat([{"role": "user", "content": prompt}], purpose=purpose)

    async def aclose(self) -> None:
        if self._own:
            await self._client.aclose()


def parse_json_object(raw: str) -> dict[str, Any]:
    try:
        v = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise ValueError(f"model returned no JSON object: {raw[:120]!r}") from None
        v = json.loads(m.group(0))
    if not isinstance(v, dict):
        raise TypeError(f"model returned JSON that is not an object: {raw[:120]!r}")
    return v
