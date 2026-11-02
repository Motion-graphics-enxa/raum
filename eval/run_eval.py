#!/usr/bin/env python3
"""RAUM evaluation (plan §12): fact guard, second-model check, translator, on eval/sets/*.jsonl.

    # on any laptop (guard only, mock profile)
    uv run --project core python eval/run_eval.py
    # on the box (translator + back-translation + bge-m3 similarity as well)
    RAUM_PROFILE=spark uv run --project core python eval/run_eval.py --profile spark --lang ru

What it measures, per language and per perturbation type:
  * guard on the clean reference          -> expect green; red = false alarm
  * guard on the perturbed translation    -> expect red;   red = caught
  * guard extraction recall on the source (informational: expected `facts` vs guard.extract)
  * with engines: the second-model check (back-translation into the source language, bge-m3
    cosine, §6.9 thresholds) on ref and perturbed; and the live translator on the source
    (first-token and full latency, guard pass rate, back-translation similarity)
  * a threshold suggestion for §6.9 calibration from the clean vs semantic-perturbed similarities

Writes eval/out/report.json and eval/out/report.md. Exit code 0 unless --strict and a target fails.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    GUARD_TYPES,
    OUT_DIR,
    PERTURBATIONS,
    SEMANTIC_TYPES,
    DirectEmbedder,
    DirectTranslator,
    DirectVerifier,
    Guard,
    Ollama,
    badge,
    cosine,
    fact_recall,
    fmt_ms,
    fmt_pct,
    lang_name,
    load_sets,
    md_table,
    now_iso,
    percentile,
    rate,
    split_csv,
    timed_stream,
    translate_fields,
    write_json,
)

TARGETS = {"guard_numbers": 1.0, "check_semantic": 0.8, "false_alarms": 0.10}


# ------------------------------------------------------------------ engines


async def build(cfg: Any, direct_ollama: str | None, log) -> tuple[Any, Any, Any, str, list[Any]]:
    """(translator, verifier, embedder, source, closers). Prefers the pipeline's own engines."""
    if not direct_ollama:
        try:
            from raum.pipeline.engines import build_engines  # written by the pipeline workstream

            eng = build_engines(cfg)
            if inspect.isawaitable(eng):
                eng = await eng
            return eng.translate, eng.verify, eng.embed, "raum.pipeline.engines", [eng]
        except (ImportError, NotImplementedError) as e:
            if cfg.models.translate.engine != "ollama":
                raise SystemExit(f"build_engines unavailable ({e!r}) and profile '{cfg.profile}' has no "
                                 "ollama translator to fall back to") from e
            log(f"build_engines unavailable ({e!r}); falling back to direct Ollama engines")
    m = cfg.models
    url = direct_ollama or m.translate.url or "http://127.0.0.1:11434"
    ol = Ollama(url)
    tr = DirectTranslator(ol, m.translate.model, m.translate.options)
    ve = DirectVerifier(Ollama(m.verify.url or url) if not direct_ollama else ol, m.verify.model,
                        m.verify.options) if m.verify.enabled else None
    em = DirectEmbedder(ol, m.embed.model) if m.embed.enabled else None
    return tr, ve, em, f"direct ollama {url}", [ol]


async def second_check(verifier: Any, embedder: Any, rec: dict, tgt_text: str, guard_red: bool | None,
                       thr: Any) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        back = await verifier.back_translate(tgt_text, rec["tgt_lang"], rec["src_lang"])
        t1 = time.perf_counter()
        a, b = await embedder.embed([rec["src"], back])
        s = cosine(a, b)
    except Exception as e:  # noqa: BLE001
        return {"similarity": None, "badge": "grey", "back": None, "error": repr(e),
                "combined": "red" if guard_red else "grey", "ms": round((time.perf_counter() - t0) * 1000, 1)}
    t2 = time.perf_counter()
    return {"similarity": round(s, 4), "badge": badge(s, thr.green, thr.amber),
            "combined": badge(s, thr.green, thr.amber, guard_red), "back": back,
            "ms": round((t2 - t0) * 1000, 1), "bt_ms": round((t1 - t0) * 1000, 1),
            "embed_ms": round((t2 - t1) * 1000, 1)}


# ------------------------------------------------------------------ one record


async def eval_record(rec: dict, guard: Guard, engines: tuple | None, thr: Any, mode: Any,
                      glossary: Any) -> dict[str, Any]:
    row: dict[str, Any] = {k: rec.get(k) for k in ("id", "sid", "lang", "room", "direction", "perturbation",
                                                   "approved")}
    row["src"] = rec["src"]
    found = guard.extract(rec["room"], rec["src"], rec["src_lang"])
    if found is not None:
        hit, missing = fact_recall(rec["facts"], found)
        row["extract"] = {"expected": len(rec["facts"]), "hit": hit, "missing": missing, "found": found}
    if rec.get("ref"):
        g = guard.check(rec["room"], rec["src"], rec["src_lang"], rec["ref"], rec["tgt_lang"])
        row["guard_ref"] = {"red": g.red, "mismatches": g.mismatches, "ms": round(g.ms, 3), "error": g.error}
    if rec.get("perturbed"):
        p = rec["perturbed"]["tgt_text"]
        g = guard.check(rec["room"], rec["src"], rec["src_lang"], p, rec["tgt_lang"])
        row["guard_pert"] = {"red": g.red, "mismatches": g.mismatches, "ms": round(g.ms, 3), "error": g.error}
    if engines is None:
        return row
    translator, verifier, embedder = engines
    checking = verifier is not None and embedder is not None
    if checking and rec.get("ref"):
        row["check_ref"] = await second_check(verifier, embedder, rec, rec["ref"],
                                              (row.get("guard_ref") or {}).get("red"), thr)
    if checking and rec.get("perturbed"):
        row["check_pert"] = await second_check(verifier, embedder, rec, rec["perturbed"]["tgt_text"],
                                               (row.get("guard_pert") or {}).get("red"), thr)
    # the live translator on the source sentence
    from raum.pipeline.base import TranslateRequest, TurnContext

    f = translate_fields(rec, mode, glossary)
    req = TranslateRequest(text=rec["src"], src_lang=rec["src_lang"], tgt_lang=rec["tgt_lang"],
                           room_name=f["room_name"], role_a=f["role_a"], role_b=f["role_b"],
                           lang_a="de", lang_b=rec["lang"], register=mode.register, glossary=f["glossary"],
                           context="(none)",
                           turn=TurnContext(room=f"{mode.prefix}-eval", mode=mode.id, speaker="eval",
                                            role="a" if rec["src_lang"] == "de" else "b", utt=rec["id"]))
    try:
        text, first, full = await timed_stream(translator.stream(req))
        g = guard.check(rec["room"], rec["src"], rec["src_lang"], text, rec["tgt_lang"])
        mt: dict[str, Any] = {"text": text, "first_ms": first and round(first, 1), "full_ms": round(full, 1),
                              "guard_red": g.red, "mismatches": g.mismatches}
        if checking:
            mt |= await second_check(verifier, embedder, rec, text, g.red, thr)
    except Exception as e:  # noqa: BLE001
        mt = {"error": repr(e)}
    row["mt"] = mt
    return row


# ------------------------------------------------------------------ metrics


def summarize(rows: list[dict]) -> dict[str, Any]:
    s: dict[str, Any] = {"n": len(rows)}

    def by_type(key: str, field: str, hit: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for t in PERTURBATIONS:
            vals = [r[key][field] for r in rows if r.get("perturbation") == t and key in r
                    and r[key].get(field) is not None]
            n = sum(1 for r in rows if r.get("perturbation") == t)
            caught = sum(1 for v in vals if hit(v))
            out[t] = {"n": n, "evaluated": len(vals), "caught": caught, "rate": rate(caught, len(vals))}
        return out

    def group_rate(types: tuple[str, ...], d: dict[str, Any]) -> float | None:
        ev = sum(d[t]["evaluated"] for t in types)
        return rate(sum(d[t]["caught"] for t in types), ev)

    # guard
    refs = [r["guard_ref"]["red"] for r in rows if r.get("guard_ref", {}).get("red") is not None]
    gt = by_type("guard_pert", "red", lambda v: v is True)
    ext = [r["extract"] for r in rows if "extract" in r]
    by_kind: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        if "extract" not in r:
            continue
        missing = defaultdict(int)
        for m in r["extract"]["missing"]:
            missing[m["kind"]] += 1
        exp = defaultdict(int)
        for f in (r.get("_facts") or []):
            exp[f["kind"]] += 1
        for k, n in exp.items():
            by_kind[k][0] += n
            by_kind[k][1] += n - missing.get(k, 0)
    ms = [r[k]["ms"] for r in rows for k in ("guard_ref", "guard_pert") if r.get(k, {}).get("red") is not None]
    s["guard"] = {
        "by_type": gt,
        "numbers_rate": group_rate(GUARD_TYPES, gt),
        "semantic_rate": group_rate(SEMANTIC_TYPES, gt),
        "false_alarms": {"n": len(refs), "red": sum(refs), "rate": rate(sum(refs), len(refs))},
        "extraction": {"expected": sum(e["expected"] for e in ext), "hit": sum(e["hit"] for e in ext),
                       "recall": rate(sum(e["hit"] for e in ext), sum(e["expected"] for e in ext)),
                       "by_kind": {k: {"expected": v[0], "hit": v[1], "recall": rate(v[1], v[0])}
                                   for k, v in sorted(by_kind.items())}},
        "ms": {"p50": percentile(ms, 50), "p95": percentile(ms, 95), "max": max(ms) if ms else None},
    }
    # second-model check
    if any("check_ref" in r or "check_pert" in r for r in rows):
        ct = by_type("check_pert", "similarity", lambda v: v is not None)  # placeholder for counts
        for t in PERTURBATIONS:
            sims = [r["check_pert"] for r in rows if r.get("perturbation") == t and "check_pert" in r
                    and r["check_pert"].get("similarity") is not None]
            ct[t]["caught"] = sum(1 for c in sims if c["badge"] == "red")
            ct[t]["flagged"] = sum(1 for c in sims if c["badge"] in ("red", "amber"))
            ct[t]["rate"] = rate(ct[t]["caught"], len(sims))
            ct[t]["flag_rate"] = rate(ct[t]["flagged"], len(sims))
        cref = [r["check_ref"] for r in rows if r.get("check_ref", {}).get("similarity") is not None]
        comb = by_type("check_pert", "combined", lambda v: v == "red")
        comb_ref = [r["check_ref"]["combined"] for r in rows if "check_ref" in r]
        s["check"] = {
            "by_type": ct,
            "semantic_rate": group_rate(SEMANTIC_TYPES, ct),
            "numbers_rate": group_rate(GUARD_TYPES, ct),
            "false_alarms": {"n": len(cref), "red": sum(1 for c in cref if c["badge"] == "red"),
                             "amber": sum(1 for c in cref if c["badge"] == "amber"),
                             "rate": rate(sum(1 for c in cref if c["badge"] == "red"), len(cref))},
            "similarity": {"clean_p50": percentile([c["similarity"] for c in cref], 50),
                           "clean_min": min((c["similarity"] for c in cref), default=None),
                           "perturbed_p50": percentile([r["check_pert"]["similarity"] for r in rows
                                                        if r.get("check_pert", {}).get("similarity")
                                                        is not None], 50)},
            "ms": {q: percentile([c.get("ms") for r in rows for k in ("check_ref", "check_pert")
                                  for c in [r.get(k) or {}]] + [r["mt"].get("ms") for r in rows if "mt" in r],
                                 int(q[1:])) for q in ("p50", "p95")},
            "calibration": calibrate(rows),
        }
        s["combined"] = {
            "by_type": comb,
            "all_rate": group_rate(PERTURBATIONS, comb),
            "false_alarms": {"n": len(comb_ref), "red": sum(1 for c in comb_ref if c == "red"),
                             "rate": rate(sum(1 for c in comb_ref if c == "red"), len(comb_ref))},
        }
    # live translator
    mts = [r["mt"] for r in rows if "mt" in r]
    if mts:
        ok = [m for m in mts if "error" not in m]
        greens = [m for m in ok if m.get("guard_red") is False]
        judged = [m for m in ok if m.get("guard_red") is not None]
        s["mt"] = {
            "n": len(mts), "errors": len(mts) - len(ok),
            "first_ms": {"p50": percentile([m["first_ms"] for m in ok], 50),
                         "p95": percentile([m["first_ms"] for m in ok], 95)},
            "full_ms": {"p50": percentile([m["full_ms"] for m in ok], 50),
                        "p95": percentile([m["full_ms"] for m in ok], 95)},
            "guard_pass": rate(len(greens), len(judged)),
            "similarity_p50": percentile([m.get("similarity") for m in ok], 50),
            "badges": {b: sum(1 for m in ok if m.get("combined", m.get("badge")) == b)
                       for b in ("green", "amber", "red", "grey")},
        }
    return s


def calibrate(rows: list[dict]) -> dict[str, Any] | None:
    """Suggest the §6.9 red threshold: best Youden J between clean refs and semantic perturbations."""
    clean = [r["check_ref"]["similarity"] for r in rows if r.get("check_ref", {}).get("similarity") is not None]
    bad = [r["check_pert"]["similarity"] for r in rows if r.get("perturbation") in SEMANTIC_TYPES
           and r.get("check_pert", {}).get("similarity") is not None]
    if len(clean) < 3 or len(bad) < 2:
        return None
    best = None
    for t in sorted(set(clean + bad)):
        thr = t + 1e-6  # "red if s < thr"
        tpr = sum(1 for s in bad if s < thr) / len(bad)
        fpr = sum(1 for s in clean if s < thr) / len(clean)
        j = tpr - fpr
        if best is None or j > best["j"]:
            best = {"red_below": round(thr, 4), "catch": tpr, "false_alarm": fpr, "j": round(j, 4)}
    return best


# ------------------------------------------------------------------ report


def targets(s: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    g = s["guard"]
    out.append({"metric": "Fact guard on perturbed numbers/units/dates", "target": "100% caught",
                "value": g["numbers_rate"], "pass": None if g["numbers_rate"] is None
                else g["numbers_rate"] >= TARGETS["guard_numbers"]})
    sem = s.get("check", {}).get("semantic_rate")
    out.append({"metric": "Checker on semantic perturbations (negation, allergy omitted)", "target": ">= 80%",
                "value": sem, "pass": None if sem is None else sem >= TARGETS["check_semantic"]})
    fa_src = s.get("combined", s["guard"])["false_alarms"]
    out.append({"metric": "False alarms on clean set" + (" (guard + checker)" if "combined" in s else " (guard)"),
                "target": "< 10%", "value": fa_src["rate"],
                "pass": None if fa_src["rate"] is None else fa_src["rate"] < TARGETS["false_alarms"]})
    return out


def render_md(rep: dict[str, Any]) -> str:
    s = rep["summary"]
    L = ["# RAUM eval report", "",
         f"Generated {rep['generated_at']} · profile `{rep['profile']}` · mode **{rep['mode']}** · "
         f"languages {', '.join(rep['langs'])} · {rep['n_records']} records "
         f"({rep['n_approved']} native-speaker approved)", ""]
    if not rep["guard_available"]:
        L += [f"> **Fact guard unavailable:** {rep['guard_reason']}. Guard rows below are n/a.", ""]
    if rep.get("engines"):
        L += [f"Engines: {rep['engines']}", ""]
    L += ["## Targets (§12.2)", "",
          md_table(["Metric", "Target", "Value", "Pass"],
                   [[t["metric"], t["target"], fmt_pct(t["value"]),
                     {True: "yes", False: "**no**", None: "n/a"}[t["pass"]]] for t in rep["targets"]]), ""]

    def type_rows(sec: dict[str, Any], extra: bool = False) -> list[list[Any]]:
        rows = []
        for t in PERTURBATIONS:
            d = sec["by_type"][t]
            row = [t, d["n"], d["evaluated"], d["caught"], fmt_pct(d["rate"])]
            if extra:
                row.append(fmt_pct(d.get("flag_rate")))
            rows.append(row)
        return rows

    g = s["guard"]
    L += ["## Fact guard (V-01)", "",
          md_table(["Perturbation", "n", "evaluated", "caught (red)", "catch rate"], type_rows(g)), "",
          f"False alarms on clean references: {g['false_alarms']['red']}/{g['false_alarms']['n']} "
          f"= {fmt_pct(g['false_alarms']['rate'])}. "
          f"Guard time p50 {fmt_ms(g['ms']['p50'])}, p95 {fmt_ms(g['ms']['p95'])} (target < 5 ms).", "",
          f"Extraction recall on the source sentences: {g['extraction']['hit']}/{g['extraction']['expected']} "
          f"= {fmt_pct(g['extraction']['recall'])} (informational: depends on the guard's value normalisation).",
          ""]
    if g["extraction"]["by_kind"]:
        L += [md_table(["Kind", "expected", "extracted", "recall"],
                       [[k, v["expected"], v["hit"], fmt_pct(v["recall"])]
                        for k, v in g["extraction"]["by_kind"].items()]), ""]
    if "check" in s:
        c = s["check"]
        L += ["## Second-model check (V-02: back-translation + bge-m3)", "",
              md_table(["Perturbation", "n", "evaluated", "red", "red rate", "red or amber"], type_rows(c, True)),
              "", f"False alarms (red) on clean references: {c['false_alarms']['red']}/{c['false_alarms']['n']} "
              f"= {fmt_pct(c['false_alarms']['rate'])}; amber: {c['false_alarms']['amber']}.",
              f"Similarity: clean p50 {c['similarity']['clean_p50']}, clean min {c['similarity']['clean_min']}, "
              f"perturbed p50 {round(c['similarity']['perturbed_p50'] or 0, 4)}. "
              f"Thresholds green ≥ {rep['thresholds']['green']}, amber ≥ {rep['thresholds']['amber']}. "
              f"Check time (back-translation + embedding) p50 {fmt_ms(c['ms']['p50'])}, p95 {fmt_ms(c['ms']['p95'])}.",
              ""]
        if c.get("calibration"):
            cal = c["calibration"]
            L += [f"Calibration suggestion (§6.9): red below **{cal['red_below']}** catches "
                  f"{fmt_pct(cal['catch'])} of semantic perturbations at {fmt_pct(cal['false_alarm'])} false alarms. "
                  "Set `thresholds.amber` in raum.yaml after checking the rows below.", ""]
        cm = s["combined"]
        L += ["### Combined (guard red OR checker red)", "",
              md_table(["Perturbation", "n", "evaluated", "caught", "catch rate"], type_rows(cm)), "",
              f"Combined false alarms: {cm['false_alarms']['red']}/{cm['false_alarms']['n']} "
              f"= {fmt_pct(cm['false_alarms']['rate'])}.", ""]
    if "mt" in s:
        m = s["mt"]
        L += ["## Live translator on the source sentences", "",
              md_table(["n", "errors", "first token p50", "p95", "full p50", "p95", "guard pass", "similarity p50",
                        "badges g/a/r/grey"],
                       [[m["n"], m["errors"], fmt_ms(m["first_ms"]["p50"]), fmt_ms(m["first_ms"]["p95"]),
                         fmt_ms(m["full_ms"]["p50"]), fmt_ms(m["full_ms"]["p95"]), fmt_pct(m["guard_pass"]),
                         m["similarity_p50"], "/".join(str(m["badges"][b]) for b in ("green", "amber", "red",
                                                                                      "grey"))]]), ""]
    if len(rep["by_lang"]) > 1:
        L += ["## Per language", "",
              md_table(["Lang", "n", "guard numbers", "guard semantic", "guard false alarms", "checker semantic",
                        "checker false alarms"],
                       [[f"{lg} ({lang_name(lg)})", v["n"], fmt_pct(v["guard"]["numbers_rate"]),
                         fmt_pct(v["guard"]["semantic_rate"]), fmt_pct(v["guard"]["false_alarms"]["rate"]),
                         fmt_pct(v.get("check", {}).get("semantic_rate")),
                         fmt_pct(v.get("check", {}).get("false_alarms", {}).get("rate"))]
                        for lg, v in rep["by_lang"].items()]), ""]
    # problem rows
    probs = []
    for r in rep["records"]:
        gr, gp = r.get("guard_ref", {}), r.get("guard_pert", {})
        if gr.get("red"):
            probs.append([r["id"], "false alarm (guard on ref)", _mism(gr["mismatches"])])
        if gp and gp.get("red") is False:
            probs.append([r["id"], f"missed {r['perturbation']} (guard)", ""])
        cr, cp = r.get("check_ref", {}), r.get("check_pert", {})
        if cr.get("badge") == "red":
            probs.append([r["id"], f"false alarm (checker, s={cr['similarity']})", (cr.get("back") or "")[:80]])
        if cp and cp.get("combined") not in ("red", None):
            probs.append([r["id"], f"missed {r['perturbation']} (combined, s={cp.get('similarity')})",
                          (cp.get("back") or "")[:80]])
        for k in ("guard_ref", "guard_pert"):
            if r.get(k, {}).get("error") and rep["guard_available"]:
                probs.append([r["id"], f"guard error ({k})", r[k]["error"][:80]])
        if r.get("mt", {}).get("error"):
            probs.append([r["id"], "translator error", r["mt"]["error"][:80]])
    if probs:
        L += ["## Rows to look at", "", md_table(["id", "what", "detail"], probs), ""]
    L += ["Native-speaker approval: records with `\"approved\": false` are drafts; "
          "rerun with `--approved-only` for the proof slide (see eval/sets/README.md).", ""]
    return "\n".join(L)


def _mism(m: list[dict]) -> str:
    return "; ".join(f"{x.get('kind')}: {x.get('src')!s} → {x.get('tgt')!s}" for x in m)[:120]


# ------------------------------------------------------------------ main


async def main_async(args: argparse.Namespace) -> int:
    if args.profile:
        os.environ["RAUM_PROFILE"] = args.profile
    from raum.config import load_config

    cfg = load_config()
    log = (lambda *a: print(*a, file=sys.stderr)) if not args.quiet else (lambda *a: None)
    recs = load_sets(split_csv(args.lang), split_csv(args.room), args.approved_only)
    if args.limit:
        recs = recs[: args.limit]
    if not recs:
        print("no records match the filters", file=sys.stderr)
        return 2
    guard = Guard()
    if not guard.available:
        log(f"warning: {guard.reason}; guard metrics will be n/a")

    use_engines = (cfg.profile != "mock" or args.engines) and not args.guard_only
    engines = None
    closers: list[Any] = []
    engine_desc = None
    if use_engines:
        tr, ve, em, engine_desc, closers = await build(cfg, args.direct_ollama, log)
        engines = (tr, ve, em)
        log(f"engines: {engine_desc}; translate={getattr(tr, 'name', '?')} "
            f"verify={getattr(ve, 'name', None)} embed={getattr(em, 'name', None)}")

    rows: list[dict] = []
    try:
        for i, rec in enumerate(recs, 1):
            row = await eval_record(rec, guard, engines, cfg.thresholds, guard.modes[rec["room"]],
                                    guard.glossaries[rec["room"]])
            row["_facts"] = rec["facts"]
            rows.append(row)
            if engines:
                log(f"[{i}/{len(recs)}] {rec['id']}")
    finally:
        for c in closers:
            close = getattr(c, "aclose", None)
            if close:
                await close()

    summary = summarize(rows)
    by_lang = {lg: summarize([r for r in rows if r["lang"] == lg]) for lg in sorted({r["lang"] for r in rows})}
    for r in rows:
        r.pop("_facts", None)
    rep = {
        "generated_at": now_iso(),
        "profile": cfg.profile,
        "mode": "full" if engines else "guard-only",
        "engines": engine_desc,
        "models": cfg.engine_names(),
        "thresholds": {"green": cfg.thresholds.green, "amber": cfg.thresholds.amber},
        "langs": sorted({r["lang"] for r in rows}),
        "n_records": len(rows),
        "n_approved": sum(1 for r in rows if r.get("approved")),
        "guard_available": guard.available,
        "guard_reason": guard.reason,
        "summary": summary,
        "by_lang": by_lang,
        "targets": targets(summary),
        "records": rows,
    }
    out = Path(args.out)
    write_json(out / "report.json", rep)
    md = render_md(rep)
    (out / "report.md").write_text(md, encoding="utf-8")
    if not args.quiet:
        print(md.split("## Rows to look at")[0].rstrip())
        print(f"\nwrote {out / 'report.json'} and {out / 'report.md'}")
    if args.strict and any(t["pass"] is False for t in rep["targets"]):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="raum profile (default: raum.yaml / RAUM_PROFILE). Non-mock = full run")
    ap.add_argument("--lang", help="comma list, e.g. ru or ru,tr (default: all)")
    ap.add_argument("--room", help="comma list of room ids (default: all)")
    ap.add_argument("--approved-only", action="store_true", help="only native-speaker approved records")
    ap.add_argument("--guard-only", action="store_true", help="skip translator and second-model check")
    ap.add_argument("--engines", action="store_true", help="run engines even on the mock profile (plumbing test)")
    ap.add_argument("--direct-ollama", metavar="URL", help="bypass build_engines; call this Ollama directly")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--strict", action="store_true", help="exit 1 if a §12.2 target fails")
    ap.add_argument("-q", "--quiet", action="store_true")
    return asyncio.run(main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
