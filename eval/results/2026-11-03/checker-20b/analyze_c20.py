#!/usr/bin/env python3
"""checker-20b analysis: gpt-oss:20b (this run) vs gpt-oss:120b (g3b baseline, same 320 items, same production
back-translation check). Usage: python3 analyze_c20.py <run-dir> <out-dir>

run-dir has results.jsonl, pauses.jsonl, meta-*.json, b-items.json. Writes <out-dir>/results.json and summary.json and
prints markdown tables.
"""
from __future__ import annotations

import json
import math
import sys
from collections import Counter
from pathlib import Path

RUN, OUT = Path(sys.argv[1]), Path(sys.argv[2])
RED, GREEN = 0.83, 0.86
PERTS = ("negation", "allergy_omitted", "semantic", "wrong_number", "wrong_unit", "date_shifted")


def pct(v, q):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * q / 100
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


items = {it["key"]: it for it in json.loads((RUN / "b-items.json").read_text())}
rows: dict[str, dict] = {}
for line in (RUN / "results.jsonl").read_text().splitlines():
    if line.strip():
        r = json.loads(line)
        if "error" not in r or r["key"] not in rows:
            rows[r["key"]] = r  # last good row per key
pauses = [json.loads(x) for x in (RUN / "pauses.jsonl").read_text().splitlines() if x.strip()] \
    if (RUN / "pauses.jsonl").exists() else []
metas = [json.loads(p.read_text()) for p in sorted(RUN.glob("meta-*.json"))]
ok = {k: r for k, r in rows.items() if "error" not in r}
errors = {k: r for k, r in rows.items() if "error" in r}


def sim20(k):
    r = ok.get(k)
    return None if r is None else r["similarity"]


def sim120(k):
    return items[k]["base"].get("similarity")


def guard(k):
    r = ok.get(k)
    return None if r is None else r.get("guard_red")


def group_keys():
    g = {f"pert:{p}": [k for k, it in items.items() if it["kind"] == "pert" and it["perturbation"] == p] for p in PERTS}
    g["pert:ALL"] = [k for k, it in items.items() if it["kind"] == "pert"]
    g["pert:neg+allergy"] = g["pert:negation"] + g["pert:allergy_omitted"]
    g["pert:numbers(num/unit/date)"] = g["pert:wrong_number"] + g["pert:wrong_unit"] + g["pert:date_shifted"]
    g["ref"] = [k for k, it in items.items() if it["kind"] == "ref"]
    g["mt"] = [k for k, it in items.items() if it["kind"] == "mt"]
    return g


G = group_keys()


def count(keys, simf, thr, with_guard=False):
    """(flagged, evaluated) over keys that have a 20b result (so both models are counted on the same items)."""
    n = c = 0
    for k in keys:
        if k not in ok:
            continue
        s = simf(k)
        if s is None:
            continue
        n += 1
        c += (s < thr) or (with_guard and bool(guard(k)))
    return c, n


summary = {"groups": {}, "n_items": len(items), "n_done": len(ok), "n_errors": len(errors)}
for g, keys in G.items():
    summary["groups"][g] = {
        "n_total": len(keys),
        "oss20_red": count(keys, sim20, RED), "oss20_amber_or_red": count(keys, sim20, GREEN),
        "oss20_badge_red_with_guard": count(keys, sim20, RED, True),
        "oss120_red": count(keys, sim120, RED), "oss120_amber_or_red": count(keys, sim120, GREEN),
        "oss120_badge_red_with_guard": count(keys, sim120, RED, True),
        "guard_red": (sum(1 for k in keys if k in ok and guard(k)), sum(1 for k in keys if k in ok)),
    }

# latency
okr = list(ok.values())
lat = {}
for name, f in (("check_ms (bt+embed)", lambda r: r["ms"]), ("bt_ms", lambda r: r["bt_ms"]),
                ("embed_ms", lambda r: r["embed_ms"]),
                ("first_call_wall_ms", lambda r: r["calls"][0]["wall_ms"]),
                ("first_call_server_ms", lambda r: r["calls"][0]["total_ms"])):
    v = [f(r) for r in okr]
    lat[name] = {"n": len(v), "p50": pct(v, 50), "p95": pct(v, 95), "max": max(v) if v else None,
                 "mean": sum(v) / len(v) if v else None}
allcalls = [c for r in okr for c in r["calls"]]
for name, f in (("per_ollama_call_wall_ms", lambda c: c["wall_ms"]),):
    v = [f(c) for c in allcalls]
    lat[name] = {"n": len(v), "p50": pct(v, 50), "p95": pct(v, 95), "max": max(v) if v else None,
                 "mean": sum(v) / len(v) if v else None}
nr = [r["ms"] for r in okr if not r.get("retried")]
wr = [r["ms"] for r in okr if r.get("retried")]
lat["check_ms_no_retry"] = {"n": len(nr), "p50": pct(nr, 50), "p95": pct(nr, 95), "max": max(nr) if nr else None}
lat["check_ms_with_retry"] = {"n": len(wr), "p50": pct(wr, 50), "p95": pct(wr, 95), "max": max(wr) if wr else None}
retries = sum(1 for r in okr if r.get("retried"))
over20 = sum(1 for r in okr if r["ms"] > 20000)
loads = sum(1 for r in okr for c in r["calls"] if (c.get("load_ms") or 0) > 500)
b120 = [items[k]["base"].get("ms") for k in ok]
lat["oss120_g3b_check_ms"] = {"n": len([x for x in b120 if x]), "p50": pct(b120, 50), "p95": pct(b120, 95),
                              "max": max(x for x in b120 if x)}
summary["latency"] = lat
summary["retries_done_reason_length"] = retries
summary["checks_over_20s_production_timeout"] = over20
summary["calls_with_load_over_500ms"] = loads
summary["eval_count_first_call"] = {"p50": pct([r["calls"][0].get("eval_count") for r in okr], 50),
                                    "p95": pct([r["calls"][0].get("eval_count") for r in okr], 95)}
summary["empty_back_translations"] = sum(1 for r in okr if not (r.get("back") or "").strip())
summary["pauses"] = {"n": len(pauses), "total_s": round(sum(p["dur_s"] for p in pauses), 1),
                     "max_s": max((p["dur_s"] for p in pauses), default=0),
                     "health_unreachable_polls": sum(p.get("health_unreachable_polls", 0) for p in pauses),
                     "list": pauses}
if okr:
    t0 = min(r["t"] for r in okr)
    t1 = max(r["t"] for r in okr)
    summary["wall_span_s"] = round(t1 - t0, 1)
summary["by_lang_pert_red"] = {}
for lang in ("en", "ru", "tr", "ar"):
    ks = [k for k in G["pert:ALL"] if items[k]["lang"] == lang]
    rk = [k for k in G["ref"] if items[k]["lang"] == lang]
    mk = [k for k in G["mt"] if items[k]["lang"] == lang]
    summary["by_lang_pert_red"][lang] = {"pert_oss20": count(ks, sim20, RED), "pert_oss120": count(ks, sim120, RED),
                                         "ref_oss20": count(rk, sim20, RED), "ref_oss120": count(rk, sim120, RED),
                                         "mt_oss20": count(mk, sim20, RED), "mt_oss120": count(mk, sim120, RED)}

# discordant items
disc = []
for k in ok:
    a, b = sim20(k), sim120(k)
    if b is None:
        continue
    if (a < RED) != (b < RED):
        it = items[k]
        disc.append({"key": k, "kind": it["kind"], "perturbation": it.get("perturbation"), "src": it["src"], "tgt": it["tgt"],
                     "oss20_sim": a, "oss20_back": ok[k]["back"], "oss120_sim": b, "oss120_back": it["base"].get("back")})
summary["discordant_red"] = disc
flags = []
for k in ok:
    it = items[k]
    if it["kind"] in ("ref", "mt") and sim20(k) < GREEN:
        flags.append({"key": k, "kind": it["kind"], "badge": ok[k]["badge"], "src": it["src"], "tgt": it["tgt"],
                      "oss20_sim": sim20(k), "oss20_back": ok[k]["back"], "oss120_sim": sim120(k),
                      "oss120_back": it["base"].get("back"), "guard_red": guard(k)})
summary["ref_mt_flags_oss20"] = flags
misses = []
for k in G["pert:ALL"]:
    if k in ok and sim20(k) >= RED:
        it = items[k]
        misses.append({"key": k, "perturbation": it["perturbation"], "src": it["src"], "tgt": it["tgt"],
                       "note": it.get("note"), "oss20_sim": sim20(k), "oss20_back": ok[k]["back"],
                       "oss120_sim": sim120(k), "guard_red": guard(k)})
summary["pert_misses_oss20"] = misses

# threshold-matched comparison: the 20b's similarities run lower overall, so compare at equal clean-ref false alarms
meaning = [k for k in G["pert:semantic"] + G["pert:neg+allergy"] if k in ok]
semk = [k for k in G["pert:semantic"] if k in ok]
refk = [k for k in G["ref"] if k in ok]
mtk = [k for k in G["mt"] if k in ok]
tm = {}
for name, f in (("oss20", sim20), ("oss120", sim120)):
    rv = sorted(f(k) for k in refk)
    tm[name] = {"auc_meaning_perts_vs_refs": round(sum((f(a) < f(b)) + 0.5 * (f(a) == f(b)) for a in meaning for b in refk)
                                                   / (len(meaning) * len(refk)), 4), "at_ref_fa": {}}
    for fa in (1, 2, 4, 8):
        t = rv[fa]  # red if s < t: exactly fa clean refs below t (barring ties)
        tm[name]["at_ref_fa"][fa] = {"thr": t, "meaning_perts_56": sum(1 for k in meaning if f(k) < t),
                                     "semantic_40": sum(1 for k in semk if f(k) < t), "mt_flags_120": sum(1 for k in mtk if f(k) < t)}
summary["threshold_matched"] = tm
# paired discordance at the production threshold
summary["paired_red_discordance"] = {
    g: {"oss20_only": sum(1 for k in G[g] if k in ok and sim20(k) < RED <= sim120(k)),
        "oss120_only": sum(1 for k in G[g] if k in ok and sim120(k) < RED <= sim20(k))}
    for g in ("pert:ALL", "ref", "mt")}
# retry budget: how many first calls would still hit the cap with a higher floor (eval_count of the call that finished)
summary["retry_floor_sim"] = {fl: sum(1 for r in okr if (r["calls"][-1].get("eval_count") or 0) >= max(fl, r["calls"][0]["num_predict"]))
                              for fl in (64, 96, 128, 160)}
summary["retry_by_back_lang"] = {l: [sum(1 for r in okr if r.get("retried") and r["src_lang"] == l),
                                     sum(1 for r in okr if r["src_lang"] == l)] for l in ("de", "en", "ru", "tr", "ar")}
summary["identical_back_translations_vs_120b"] = sum(1 for k, r in ok.items()
                                                     if r["back"].strip() == (items[k]["base"].get("back") or "").strip())

OUT.mkdir(parents=True, exist_ok=True)
res = {"meta": metas, "note": "one row per item (last good row); calls = each Ollama /api/chat call of the "
       "back-translation (2 when the short num_predict hit done_reason=length and production retried at 256)",
       "items": [rows[k] for k in sorted(rows)]}
(OUT / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n")
(OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n")


def fr(t):
    return f"{t[0]}/{t[1]}"


print(f"done {len(ok)}/{len(items)}, errors {len(errors)}")
print("\n| group | n | 20b red | 120b red | 20b amber+red | 120b amber+red | 20b badge red (+guard) | 120b badge red (+guard) | guard alone |")
print("|---|---|---|---|---|---|---|---|---|")
for g, v in summary["groups"].items():
    print(f"| {g} | {v['n_total']} | {fr(v['oss20_red'])} | {fr(v['oss120_red'])} | {fr(v['oss20_amber_or_red'])} | "
          f"{fr(v['oss120_amber_or_red'])} | {fr(v['oss20_badge_red_with_guard'])} | {fr(v['oss120_badge_red_with_guard'])} | "
          f"{fr(v['guard_red'])} |")
print("\nlatency:")
for k, v in lat.items():
    print(f"  {k}: n={v['n']} p50 {v['p50'] and round(v['p50'])} p95 {v['p95'] and round(v['p95'])} max {v['max'] and round(v['max'])}")
print(f"retries {retries}, >20 s {over20}, loads>500ms {loads}, empty backs {summary['empty_back_translations']}, "
      f"eval_count first call {summary['eval_count_first_call']}")
print(f"pauses: {summary['pauses']['n']} total {summary['pauses']['total_s']} s max {summary['pauses']['max_s']} s; "
      f"wall span {summary.get('wall_span_s')} s")
print("\nby lang:", json.dumps(summary["by_lang_pert_red"]))
print("\nthreshold-matched:", json.dumps(tm))
print("paired discordance:", json.dumps(summary["paired_red_discordance"]))
print("retry floor sim:", summary["retry_floor_sim"], "retry by back-translation language:", summary["retry_by_back_lang"],
      "identical backs:", summary["identical_back_translations_vs_120b"])
print(f"\ndiscordant red ({len(disc)}):")
for d in disc:
    print(f"- {d['key']} {d['perturbation'] or ''} 20b {d['oss20_sim']} / 120b {d['oss120_sim']}\n    src {d['src']}\n    tgt {d['tgt']}\n"
          f"    20b back {d['oss20_back']}\n    120b back {d['oss120_back']}")
print(f"\n20b flags (amber/red) on ref/mt ({len(flags)}):")
for d in flags:
    print(f"- {d['key']} {d['badge']} 20b {d['oss20_sim']} / 120b {d['oss120_sim']} guard {d['guard_red']}\n    src {d['src']}\n"
          f"    tgt {d['tgt']}\n    20b back {d['oss20_back']}\n    120b back {d['oss120_back']}")
print(f"\n20b misses on perturbations ({len(misses)}):")
for d in misses:
    print(f"- {d['key']} {d['perturbation']} 20b {d['oss20_sim']} 120b {d['oss120_sim']} guard {d['guard_red']} | {d['note']}")
