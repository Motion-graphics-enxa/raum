#!/usr/bin/env python3
"""eval-gemma Part B: second-AI checker candidates on the g3b items (120 records x ref / perturbed / live MT).

  --mode bt     RAUM's OllamaVerifier.back_translate (prompts/backtranslate.md, num_predict cap 64-256 + retry)
                + RAUM's OllamaEmbedder (bge-m3) cosine with the source: exactly the live check, other model
  --mode btraw  same prompt, own call with a big num_predict (thinking models that RAUM's cap would cut off)
  --mode judge  one call: source + translation -> JSON {"same_meaning": bool, "diff": str}; no embedding

The first --seq items (fixed shuffle, mixed kinds and languages) run one at a time for latency; the rest run
--conc at a time (throughput only). Writes out/b-<tag>.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time

import httpx

from common_gemma import HERE, OLLAMA, b_items, common, pct, should_stop, write

from raum.pipeline._util import lang_name, load_prompt, render_prompt  # noqa: E402
from raum.pipeline.embed import OllamaEmbedder  # noqa: E402
from raum.pipeline.verifier import OllamaVerifier, parse_json_object  # noqa: E402

JUDGE_SYSTEM = (
    "You check an interpreter's translation. Compare the MEANING of the translation with the source.\n"
    "Ignore wording, synonyms, word order, style, formal/informal address, punctuation, and how numbers, "
    "dates, units and currencies are written.\n"
    "Report a difference only if the meaning changed: a negation added or dropped; a different person, subject "
    "or institution; a changed number, unit, dose, date, time, duration or amount; before/after, from/until, "
    "at least/at most, more/less, over/under, gross/net, discount/surcharge swapped; content left out or added.\n"
    'Answer only JSON: {"same_meaning": true or false, "diff": "the changed words of the translation, or empty"}')
JUDGE_SCHEMA = {"type": "object", "properties": {"same_meaning": {"type": "boolean"}, "diff": {"type": "string"}},
                "required": ["same_meaning", "diff"]}


def think_value(s: str | None):
    return {None: None, "none": None, "false": False, "true": True}.get(s, s)  # "low"/"medium"/"high" pass through


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--mode", choices=("bt", "btraw", "judge"), required=True)
    ap.add_argument("--think", default="none", help="none (field not sent) | false | true | low")
    ap.add_argument("--num-predict", type=int, default=256)
    ap.add_argument("--seq", type=int, default=60)
    ap.add_argument("--conc", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="", help="comma list of kinds/perturbations to keep, e.g. ref,semantic,negation")
    ap.add_argument("--deadline", type=float, default=0)
    a = ap.parse_args()
    th = think_value(a.think)

    items = b_items()
    random.Random(7).shuffle(items)
    if a.only:
        keep = set(a.only.split(","))
        items = [it for it in items if it["kind"] in keep or (it["kind"] == "pert" and it["perturbation"] in keep)]
    if a.limit:
        items = items[: a.limit]

    opts = {"num_predict": a.num_predict, "timeout_s": 120}
    if th is not None:
        opts["think"] = th
    ver = OllamaVerifier(OLLAMA, model=a.model, options=opts)
    emb = OllamaEmbedder(OLLAMA, model="bge-m3", options={"timeout_s": 60})
    client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5))
    out = {"model": a.model, "tag": a.tag, "mode": a.mode, "think": th, "num_predict": a.num_predict,
           "judge_system": JUDGE_SYSTEM if a.mode == "judge" else None, "started": time.time(), "items": []}

    async def chat(messages, *, fmt=None, num_predict=None):
        body = {"model": a.model, "messages": messages, "stream": False, "keep_alive": -1,
                "options": {"temperature": 0.0, "num_predict": num_predict or a.num_predict, "num_ctx": 8192}}
        if th is not None:
            body["think"] = th
        if fmt is not None:
            body["format"] = fmt
        r = await client.post(f"{OLLAMA}/api/chat", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"ollama {r.status_code}: {r.text[:200]}")
        return r.json()

    async def one(it: dict, seq: bool) -> dict:
        row = {k: it[k] for k in ("key", "id", "lang", "room", "perturbation", "kind", "src", "src_lang", "tgt",
                                  "tgt_lang", "base")}
        row["seq"] = seq
        t0 = time.perf_counter()
        try:
            if a.mode in ("bt", "btraw"):
                if a.mode == "bt":
                    back = await ver.back_translate(it["tgt"], it["tgt_lang"], it["src_lang"])
                    j = {}
                else:
                    system = render_prompt(load_prompt(ver.backtranslate_prompt), tgt_lang=lang_name(it["tgt_lang"]),
                                           src_lang=lang_name(it["src_lang"]))
                    j = await chat([{"role": "system", "content": system}, {"role": "user", "content": it["tgt"]}])
                    back = str((j.get("message") or {}).get("content") or "").strip()
                t1 = time.perf_counter()
                va, vb = await emb.embed([it["src"], back])
                t2 = time.perf_counter()
                row |= {"back": back, "similarity": round(common.cosine(va, vb), 4), "bt_ms": round((t1 - t0) * 1e3, 1),
                        "embed_ms": round((t2 - t1) * 1e3, 1), "ms": round((t2 - t0) * 1e3, 1)}
                if j:
                    row |= {"eval_count": j.get("eval_count"), "done_reason": j.get("done_reason"),
                            "thinking_chars": len((j.get("message") or {}).get("thinking") or "")}
            else:
                user = (f"Source ({lang_name(it['src_lang'])}): {it['src']}\n"
                        f"Translation ({lang_name(it['tgt_lang'])}): {it['tgt']}")
                msgs = [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}]
                j = await chat(msgs, fmt=JUDGE_SCHEMA)
                if j.get("done_reason") == "length":
                    j = await chat(msgs, fmt=JUDGE_SCHEMA, num_predict=4 * a.num_predict)
                raw = str((j.get("message") or {}).get("content") or "").strip()
                if "{" not in raw:  # empty under the format constraint: once more without it
                    j = await chat(msgs, num_predict=4 * a.num_predict)
                    raw = str((j.get("message") or {}).get("content") or "").strip()
                    row["format_retry"] = True
                try:
                    v = parse_json_object(raw)
                    same = v.get("same_meaning")
                    same = same if isinstance(same, bool) else str(same).strip().lower() in ("true", "yes")
                    row |= {"same": same, "diff": v.get("diff")}
                except Exception as e:  # noqa: BLE001
                    row |= {"same": None, "parse_error": repr(e)}
                row |= {"raw": raw[:400], "ms": round((time.perf_counter() - t0) * 1e3, 1), "eval_count": j.get("eval_count"),
                        "done_reason": j.get("done_reason"),
                        "thinking_chars": len((j.get("message") or {}).get("thinking") or "")}
        except Exception as e:  # noqa: BLE001
            row |= {"error": repr(e), "ms": round((time.perf_counter() - t0) * 1e3, 1)}
        return row

    # load / warm the model on a long-timeout request, then 2 unmeasured calls
    t0 = time.perf_counter()
    await chat([{"role": "user", "content": "hi"}], num_predict=1)
    out["load_s"] = round(time.perf_counter() - t0, 2)
    for it in items[-2:]:
        await one(it, False)
    print(f"[{a.tag}] loaded {out['load_s']} s; {len(items)} items, {a.seq} sequential", flush=True)

    for it in items[: a.seq]:
        if should_stop(a.deadline):
            out["stopped"] = should_stop(a.deadline)
            break
        out["items"].append(await one(it, True))
    write(HERE / "out" / f"b-{a.tag}.json", out)

    sem = asyncio.Semaphore(a.conc)

    async def guarded(it):
        async with sem:
            if should_stop(a.deadline):
                return None
            return await one(it, False)

    if "stopped" not in out:
        rest = await asyncio.gather(*(guarded(it) for it in items[a.seq:]))
        out["items"] += [r for r in rest if r is not None]
        if any(r is None for r in rest):
            out["stopped"] = should_stop(a.deadline) or "stopped"
    out["finished"] = time.time()
    write(HERE / "out" / f"b-{a.tag}.json", out)
    seq = [r["ms"] for r in out["items"] if r["seq"] and "error" not in r]
    errs = sum(1 for r in out["items"] if "error" in r)
    print(f"[{a.tag}] n={len(out['items'])} err={errs} seq ms p50 {pct(seq, 50) or 0:.0f} p90 {pct(seq, 90) or 0:.0f}",
          flush=True)
    await client.aclose()
    await ver.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
