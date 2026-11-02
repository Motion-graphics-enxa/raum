#!/usr/bin/env python3
"""eval-gemma Part A: one translator on de<->en, de<->ru, en<->ru (30 sentences per direction).

RAUM's own OllamaTranslator, prompts/translate.md and the spark options (think false, temperature 0.1,
num_predict 256, num_ctx 8192); --prompt native = TranslateGemma's own prompt instead. Sequential, after a
warm-up, grouped by room and direction like a live room (same system prefix -> Ollama prompt cache).
Guard (raum.verify.guard via eval/common.Guard) on every output and on the reference.

    uv run --no-sync --project core python bench_a.py --model gemma4:e4b --tag e4b
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx

from common_gemma import HERE, OLLAMA, a_items, common, pct, should_stop, write

from raum.pipeline.base import TranslateRequest, TurnContext  # noqa: E402
from raum.pipeline.translate import OllamaTranslator  # noqa: E402

SPARK = {"think": False, "temperature": 0.1, "num_predict": 256, "num_ctx": 8192}
NAMES = {"de": "German", "en": "English", "ru": "Russian"}
# TranslateGemma's documented prompt (ollama.com/library/translategemma): one user turn, two blank lines before the text
NATIVE = ("You are a professional {S} ({s}) to {T} ({t}) translator. Your goal is to accurately convey the meaning "
          "and nuances of the original {S} text while adhering to {T} grammar, vocabulary, and cultural "
          "sensitivities.\nProduce only the {T} translation, without any additional explanations or commentary. "
          "Please translate the following {S} text into {T}:\n\n\n{text}")


def request(it: dict, guard: common.Guard) -> TranslateRequest:
    mode, gl = guard.modes[it["room"]], guard.glossaries[it["room"]]
    s, t = it["src_lang"], it["tgt_lang"]
    la, lb = ("de", t if s == "de" else s) if "de" in (s, t) else ("en", "ru")
    return TranslateRequest(
        text=it["src"], src_lang=s, tgt_lang=t, room_name=mode.display_name("en"), role_a=mode.label("a", "en"),
        role_b=mode.label("b", "en"), lang_a=la, lang_b=lb, register=mode.register,
        glossary=gl.prompt_lines(s, t) if gl else "(none)", context="(none)",
        turn=TurnContext(room=f"{mode.prefix}-eval", mode=mode.id, speaker="eval", role="a" if s == la else "b",
                         utt=it["sid"]))


async def native_stream(client: httpx.AsyncClient, model: str, it: dict):
    body = {"model": model, "stream": True, "keep_alive": -1,
            "options": {k: SPARK[k] for k in ("temperature", "num_predict", "num_ctx")},
            "messages": [{"role": "user", "content": NATIVE.format(S=NAMES[it["src_lang"]], s=it["src_lang"],
                                                                     T=NAMES[it["tgt_lang"]], t=it["tgt_lang"],
                                                                     text=it["src"])}]}
    async with client.stream("POST", f"{OLLAMA}/api/chat", json=body) as r:
        if r.status_code >= 400:
            await r.aread()
            raise RuntimeError(f"ollama {r.status_code}: {r.text[:200]}")
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            j = json.loads(line)
            if j.get("error"):
                raise RuntimeError(j["error"])
            c = (j.get("message") or {}).get("content") or ""
            if c:
                yield c
            if j.get("done"):
                break


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--prompt", choices=("raum", "native"), default="raum")
    ap.add_argument("--deadline", type=float, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--conc", type=int, default=1, help=">1 = accuracy run (timings not meaningful)")
    ap.add_argument("--pick", type=int, default=0, help="latency run: this many sentences per direction")
    a = ap.parse_args()
    guard = common.Guard()
    items = a_items()
    if a.pick:
        per = {}
        for it in items:
            per.setdefault((it["src_lang"], it["tgt_lang"]), []).append(it)
        items = [x for v in per.values() for x in v[:: max(1, len(v) // a.pick)][: a.pick]]
    if a.limit:
        items = items[: a.limit]
    tr = OllamaTranslator(OLLAMA, model=a.model, options=dict(SPARK, timeout_s=60))
    client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5))
    out = {"model": a.model, "tag": a.tag, "prompt": a.prompt, "options": SPARK, "started": time.time(), "items": []}
    t0 = time.perf_counter()
    try:
        await tr.warmup()  # loads the model (long timeout), same options as live calls
    except RuntimeError as e:
        if "think" not in str(e):
            raise
        tr._no_think = True  # no thinking switch (TranslateGemma = Gemma 3): RAUM drops the field the same way
        await tr.warmup()
    out["load_s"] = round(time.perf_counter() - t0, 2)

    def stream(it):
        return native_stream(client, a.model, it) if a.prompt == "native" else tr.stream(request(it, guard))

    for it in items[:3]:  # warm-up: first calls (think-switch retry, prompt cache) are not measured
        await common.timed_stream(stream(it))
    print(f"[{a.tag}] loaded in {out['load_s']} s; running {len(items)}", flush=True)
    out["conc"] = a.conc
    sem = asyncio.Semaphore(a.conc)

    async def one(it):
        if should_stop(a.deadline):
            out["stopped"] = should_stop(a.deadline)
            return None
        async with sem:
            return await one_(it)

    async def one_(it):
        row = dict(it)
        try:
            text, first, full = await common.timed_stream(stream(it))
            g = guard.check(it["room"], it["src"], it["src_lang"], text, it["tgt_lang"])
            gr = guard.check(it["room"], it["src"], it["src_lang"], it["ref"], it["tgt_lang"])
            row |= {"mt": text, "first_ms": first and round(first, 1), "full_ms": round(full, 1), "guard_red": g.red,
                    "mismatches": g.mismatches, "ref_guard_red": gr.red, "ref_mismatches": gr.mismatches}
        except Exception as e:  # noqa: BLE001
            row["error"] = repr(e)
        return row

    if a.conc == 1:
        for it in items:
            r = await one(it)
            if r is None:
                break
            out["items"].append(r)
    else:
        out["items"] = [r for r in await asyncio.gather(*(one(it) for it in items)) if r is not None]
    out["finished"] = time.time()
    write(HERE / "out" / f"a-{a.tag}.json", out)
    ok = [r for r in out["items"] if "error" not in r]
    print(f"[{a.tag}] n={len(ok)} err={len(out['items']) - len(ok)} first p50 {pct([r['first_ms'] for r in ok], 50) or 0:.0f} "
          f"full p50 {pct([r['full_ms'] for r in ok], 50) or 0:.0f} p90 {pct([r['full_ms'] for r in ok], 90) or 0:.0f} ms; "
          f"guard red {sum(1 for r in ok if r['guard_red'])}", flush=True)
    await client.aclose()
    await tr.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
