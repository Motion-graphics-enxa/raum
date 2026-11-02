#!/usr/bin/env python3
"""Translator bake-off (plan §6.3): pick the translation model by measurement, not opinion.

    # on the box (Ollama on localhost); takes ~10-20 min for 4 candidates x 30 sentences
    uv run --project core python eval/bakeoff.py --ollama http://127.0.0.1:11434 --lang ru
    # anywhere, no network: mock engines and fake timings
    uv run --project core python eval/bakeoff.py --dry-run

Per candidate it translates every sentence of eval/sets (streamed through Ollama /api/chat with the
room's translate prompt), recording first-token and full-sentence latency, then runs the fact guard
on the output (pass = not red). A second pass back-translates every output with the checker model
(default gpt-oss:120b, a different family) and scores the meaning with bge-m3 against the source;
it also scores the output against the reference translation. Translation and checking run in
separate passes so model swaps never pollute the latency numbers.

Outputs: eval/out/bakeoff.csv (one row per candidate x sentence; the `native_rating` column is left
blank for the native speaker, 1-5), eval/out/bakeoff.md and eval/out/bakeoff.json.
The winner line for raum.yaml is printed at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import random
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    OUT_DIR,
    Guard,
    Ollama,
    cosine,
    fmt_ms,
    fmt_pct,
    lang_name,
    load_sets,
    md_table,
    now_iso,
    percentile,
    rate,
    render_prompt,
    split_csv,
    timed_stream,
    translate_fields,
    write_json,
)

DEFAULT_CANDIDATES = ["gemma4:26b", "gemma4:e4b", "translategemma", "qwen3:30b-a3b"]
OPTIONS = {"temperature": 0.1, "num_predict": 256}
CSV_FIELDS = ["candidate", "model", "id", "lang", "room", "direction", "perturbation", "first_ms", "full_ms",
              "guard_pass", "guard_mismatches", "back_sim", "ref_sim", "output", "back_translation", "error",
              "native_rating"]


def resolve(candidates: list[str], available: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """(label, ollama tag) for each candidate present; missing labels. A bare family name such as
    'translategemma' matches the first installed tag that starts with it."""
    found, missing = [], []
    avail = set(available)
    for c in candidates:
        if c in avail or f"{c}:latest" in avail:
            found.append((c, c if c in avail else f"{c}:latest"))
            continue
        prefix = [a for a in available if a.split("/")[-1].startswith(c.split(":")[0])
                  and (":" not in c or a.endswith(c.split(":", 1)[1]))]
        if prefix and ":" not in c:
            found.append((c, sorted(prefix)[0]))
        else:
            missing.append(c)
    return found, missing


# ------------------------------------------------------------------ dry run fakes


class FakeOllama:
    """Deterministic stand-in: the reference translation (sometimes the perturbed one) with
    latencies shaped like the candidate's size."""

    SPEED = {"gemma4:26b": (380, 26), "gemma4:e4b": (210, 14), "translategemma": (300, 22),
             "qwen3:30b-a3b": (420, 24)}

    def __init__(self, recs: list[dict]) -> None:
        self.by_src = {r["src"]: r for r in recs}
        self.by_tgt = {r.get("ref"): r for r in recs} | {r.get("perturbed", {}).get("tgt_text"): r for r in recs}

    async def tags(self) -> list[str]:
        return ["gemma4:26b", "gemma4:e4b", "qwen3:30b-a3b", "gpt-oss:120b", "bge-m3:latest"]

    async def chat_stream(self, model: str, system: str, user: str, options: Any = None, think: Any = "auto",
                          keep_alive: Any = None):
        first, per_tok = self.SPEED.get(model, (350, 25))
        rng = random.Random(f"{model}|{user}")
        rec = self.by_src.get(user)
        if rec is None:  # back-translation or warm-up
            r2 = self.by_tgt.get(user)
            out = r2["src"] if r2 else "ok"
            first, per_tok = 600, 3
        else:
            bad = rec.get("perturbed") and rng.random() < {"gemma4:e4b": 0.5, "qwen3:30b-a3b": 0.35}.get(model, 0.1)
            out = rec["perturbed"]["tgt_text"] if bad else rec["ref"]
        await asyncio.sleep(rng.uniform(0.6, 1.4) * first / 1000 / 50)  # 50x faster than real
        words = out.split(" ")
        for i, w in enumerate(words):
            if i:
                await asyncio.sleep(per_tok / 1000 / 50)
            yield w + (" " if i < len(words) - 1 else "")

    async def chat(self, model: str, system: str, user: str, options: Any = None, think: Any = "auto") -> str:
        return "".join([d async for d in self.chat_stream(model, system, user, options, think)])

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            rng = random.Random(t)
            out.append([1.0] + [rng.uniform(-0.25, 0.25) for _ in range(7)])
        return out

    async def aclose(self) -> None:
        return None


# ------------------------------------------------------------------ passes


async def translate_pass(ol: Any, label: str, model: str, recs: list[dict], guard: Guard, speed: float,
                         log) -> list[dict]:
    rows = []
    try:  # warm-up: load the model before timing (cold load is not translation latency)
        t0 = time.perf_counter()
        await ol.chat(model, "Reply with: ok", "ok", {"num_predict": 4})
        log(f"  {label}: warm in {time.perf_counter() - t0:.1f}s")
    except Exception as e:  # noqa: BLE001
        log(f"  {label}: warm-up failed: {e!r}")
    for rec in recs:
        mode, glossary = guard.modes[rec["room"]], guard.glossaries[rec["room"]]
        system = render_prompt("translate.md", **translate_fields(rec, mode, glossary))
        row: dict[str, Any] = {"candidate": label, "model": model, "id": rec["id"], "lang": rec["lang"],
                               "room": rec["room"], "direction": rec["direction"],
                               "perturbation": rec.get("perturbation") or "", "native_rating": ""}
        try:
            text, first, full = await timed_stream(ol.chat_stream(model, system, rec["src"], OPTIONS))
            row |= {"output": text, "first_ms": first and round(first * speed, 1), "full_ms": round(full * speed, 1)}
            g = guard.check(rec["room"], rec["src"], rec["src_lang"], text, rec["tgt_lang"])
            row["guard_pass"] = None if g.red is None else (not g.red)
            row["guard_mismatches"] = "; ".join(f"{m['kind']}:{m['src']}->{m['tgt']}" for m in g.mismatches)
        except Exception as e:  # noqa: BLE001
            row["error"] = repr(e)[:300]
        rows.append(row)
    return rows


async def check_pass(ol: Any, checker: str, embed_model: str, recs: dict[str, dict], rows: list[dict],
                     log) -> None:
    for row in rows:
        if not row.get("output"):
            continue
        rec = recs[row["id"]]
        try:
            system = render_prompt("backtranslate.md", tgt_lang=lang_name(rec["tgt_lang"]),
                                   src_lang=lang_name(rec["src_lang"]))
            back = await ol.chat(checker, system, row["output"], {"num_predict": 256})
            vec = await ol.embed(embed_model, [rec["src"], back, row["output"], rec.get("ref") or row["output"]])
            row["back_translation"] = back
            row["back_sim"] = round(cosine(vec[0], vec[1]), 4)
            if rec.get("ref"):
                row["ref_sim"] = round(cosine(vec[2], vec[3]), 4)
        except Exception as e:  # noqa: BLE001
            row["error"] = ((row.get("error") or "") + f" check: {e!r}")[:300]


def summarize(rows: list[dict], labels: list[str]) -> list[dict]:
    out = []
    for c in labels:
        rs = [r for r in rows if r["candidate"] == c]
        ok = [r for r in rs if r.get("output")]
        judged = [r for r in ok if r.get("guard_pass") is not None]
        rated = [float(r["native_rating"]) for r in rs if str(r.get("native_rating") or "").strip()]
        bs = [r["back_sim"] for r in ok if r.get("back_sim") is not None]
        rsim = [r["ref_sim"] for r in ok if r.get("ref_sim") is not None]
        out.append({
            "candidate": c, "model": rs[0]["model"] if rs else c, "n": len(rs), "errors": len(rs) - len(ok),
            "first_p50": percentile([r.get("first_ms") for r in ok], 50),
            "first_p95": percentile([r.get("first_ms") for r in ok], 95),
            "full_p50": percentile([r.get("full_ms") for r in ok], 50),
            "full_p95": percentile([r.get("full_ms") for r in ok], 95),
            "guard_pass": rate(sum(1 for r in judged if r["guard_pass"]), len(judged)),
            "back_sim": sum(bs) / len(bs) if bs else None,
            "ref_sim": sum(rsim) / len(rsim) if rsim else None,
            "native_rating": sum(rated) / len(rated) if rated else None,
        })
    return out


def pick_winner(summary: list[dict]) -> dict | None:
    """Highest guard pass rate, then back-translation similarity, then fastest first token.
    Candidates with a first-token p50 above 1 s are only chosen if nothing else qualifies (§3)."""
    ok = [s for s in summary if s["n"] and s["errors"] < s["n"]]
    if not ok:
        return None
    fast = [s for s in ok if (s["first_p50"] or 1e9) <= 1000] or ok

    def key(s: dict) -> tuple:  # similarities bucketed to 0.01 so noise does not beat latency
        return (-round(s["guard_pass"] or 0, 2), -round(s["native_rating"] or 0, 1), -round(s["back_sim"] or 0, 2),
                -round(s["ref_sim"] or 0, 2), s["first_p50"] or 1e9)

    return sorted(fast, key=key)[0]


def render_md(meta: dict, summary: list[dict], winner: dict | None, missing: list[str]) -> str:
    L = ["# Translator bake-off (§6.3)", "",
         f"Generated {meta['generated_at']} · {'**DRY RUN (fake engines and timings)**' if meta['dry_run'] else meta['ollama']}"
         f" · languages {', '.join(meta['langs'])} · {meta['n_sentences']} sentences · checker `{meta['checker']}`"
         f" · embeddings `{meta['embed']}`", ""]
    if not meta["guard_available"]:
        L += [f"> Fact guard unavailable ({meta['guard_reason']}); guard pass is n/a.", ""]
    L += [md_table(["Candidate", "Ollama tag", "First token p50", "p95", "Full sentence p50", "p95",
                    "Fact-guard pass", "Back-translation sim", "Sim to reference", "Native rating (1-5)"],
                   [[s["candidate"], f"`{s['model']}`", fmt_ms(s["first_p50"]), fmt_ms(s["first_p95"]),
                     fmt_ms(s["full_p50"]), fmt_ms(s["full_p95"]), fmt_pct(s["guard_pass"]),
                     "n/a" if s["back_sim"] is None else f"{s['back_sim']:.3f}",
                     "n/a" if s["ref_sim"] is None else f"{s['ref_sim']:.3f}",
                     "fill in" if s["native_rating"] is None else f"{s['native_rating']:.1f}"]
                    for s in summary]), ""]
    if missing:
        L += [f"Not installed (skipped): {', '.join(f'`{m}`' for m in missing)}. "
              "Pull with `ollama pull <tag>` and rerun. TranslateGemma is under the Gemma Terms of Use, not "
              "Apache 2.0: fine for the demo, flag it for the business.", ""]
    if winner:
        L += [f"**Winner: `{winner['model']}`.** In raum.yaml: `models: {{ translate: {{ model: {winner['model']} }} }}`",
              ""]
    L += ["Native rating: fill the `native_rating` column of eval/out/bakeoff.csv (1-5), then "
          "`uv run --project core python eval/bakeoff.py --rescore` to fold it into this table.", ""]
    return "\n".join(L)


def write_outputs(out: Path, rows: list[dict], meta: dict, labels: list[str], missing: list[str]) -> dict | None:
    out.mkdir(parents=True, exist_ok=True)
    with (out / "bakeoff.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    summary = summarize(rows, labels)
    winner = pick_winner(summary)
    write_json(out / "bakeoff.json", {**meta, "summary": summary, "winner": winner, "missing": missing})
    (out / "bakeoff.md").write_text(render_md(meta, summary, winner, missing), encoding="utf-8")
    return winner


def rescore(out: Path) -> int:
    """Recompute the summary from an edited bakeoff.csv (native ratings filled in)."""
    import json

    meta = json.loads((out / "bakeoff.json").read_text(encoding="utf-8"))
    with (out / "bakeoff.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("first_ms", "full_ms", "back_sim", "ref_sim"):
            r[k] = float(r[k]) if r.get(k) not in (None, "") else None
        r["guard_pass"] = {"True": True, "False": False}.get(r.get("guard_pass") or "")
    labels = list(dict.fromkeys(r["candidate"] for r in rows))
    summary = summarize(rows, labels)
    winner = pick_winner(summary)
    meta.update(summary=summary, winner=winner)
    write_json(out / "bakeoff.json", meta)
    (out / "bakeoff.md").write_text(render_md(meta, summary, winner, meta.get("missing", [])), encoding="utf-8")
    print((out / "bakeoff.md").read_text(encoding="utf-8"))
    return 0


async def main_async(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if args.rescore:
        return rescore(out)
    log = lambda *a: print(*a, file=sys.stderr)
    recs = load_sets(split_csv(args.lang), split_csv(args.room))
    if args.limit:
        recs = recs[: args.limit]
    if not recs:
        log("no records match the filters")
        return 2
    guard = Guard()
    if not guard.available:
        log(f"warning: {guard.reason}; guard pass will be n/a")
    ol: Any = FakeOllama(recs) if args.dry_run else Ollama(args.ollama)
    speed = 50.0 if args.dry_run else 1.0  # fake engine runs 50x faster; report realistic numbers
    try:
        available = await ol.tags()
        cands, missing = resolve(split_csv(args.candidates) or DEFAULT_CANDIDATES, available)
        if not cands:
            log(f"none of the candidates are installed; available: {available}")
            return 2
        for m in missing:
            log(f"skip {m}: not installed (ollama pull {m})")
        rows: list[dict] = []
        for label, tag in cands:
            log(f"translate: {label} ({tag}), {len(recs)} sentences")
            rows += await translate_pass(ol, label, tag, recs, guard, speed, log)
        if not args.no_check:
            if args.checker not in available and f"{args.checker}:latest" not in available:
                log(f"warning: checker {args.checker} not installed; back-translation will error")
            log(f"check: back-translation with {args.checker}, similarity with {args.embed}")
            await check_pass(ol, args.checker, args.embed, {r["id"]: r for r in recs}, rows, log)
    finally:
        await ol.aclose()
    meta = {"generated_at": now_iso(), "dry_run": args.dry_run, "ollama": args.ollama,
            "langs": sorted({r["lang"] for r in recs}), "n_sentences": len(recs), "checker": args.checker,
            "embed": args.embed, "guard_available": guard.available, "guard_reason": guard.reason,
            "options": OPTIONS}
    winner = write_outputs(out, rows, meta, [c for c, _ in cands], missing)
    print((out / "bakeoff.md").read_text(encoding="utf-8"))
    print(f"wrote {out / 'bakeoff.csv'}, {out / 'bakeoff.md'}, {out / 'bakeoff.json'}")
    if winner:
        print(f"\nraum.yaml:\nmodels:\n  translate: {{ model: {winner['model']} }}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ollama", default="http://127.0.0.1:11434", help="Ollama base URL")
    ap.add_argument("--candidates", help=f"comma list (default: {','.join(DEFAULT_CANDIDATES)})")
    ap.add_argument("--checker", default="gpt-oss:120b", help="back-translation model (different family)")
    ap.add_argument("--embed", default="bge-m3", help="embedding model")
    ap.add_argument("--lang", help="comma list, e.g. ru (default: all languages in the sets)")
    ap.add_argument("--room", help="comma list of room ids")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-check", action="store_true", help="skip back-translation and similarity")
    ap.add_argument("--dry-run", action="store_true", help="mock engines, fake timings, no network")
    ap.add_argument("--rescore", action="store_true", help="recompute the table from an edited bakeoff.csv")
    ap.add_argument("--out", default=str(OUT_DIR))
    return asyncio.run(main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
