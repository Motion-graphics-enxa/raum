#!/usr/bin/env python3
"""eval-gemma analysis: Part A (translators) and Part B (checkers) from the window's out/ folder.

    uv run --python 3.12 --with sacrebleu python analyze.py <out-dir>
"""
from __future__ import annotations

import json
import math
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import sacrebleu

OUT = Path(sys.argv[1])
CHRF = sacrebleu.metrics.CHRF(word_order=2)  # chrF++
RED, GREEN = 0.83, 0.86


def pct(v, q):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * q / 100
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def f0(x):
    return "n/a" if x is None else f"{x:.0f}"


def boot_ci(d, n=4000, seed=1):
    """95% paired-bootstrap CI of the mean of per-sentence differences d."""
    if not d:
        return (None, None, None)
    rng = random.Random(seed)
    m = sum(d) / len(d)
    bs = sorted(sum(rng.choice(d) for _ in d) / len(d) for _ in range(n))
    return m, bs[int(0.025 * n)], bs[int(0.975 * n)]


# ------------------------------------------------------------------ Part A
def part_a():
    allf = {json.loads(p.read_text())["tag"]: json.loads(p.read_text()) for p in sorted(OUT.glob("a-*.json"))
            if p.name != "a-items.json"}
    files = {t: v for t, v in allf.items() if not t.endswith("-lat")}
    latf = {t[:-4]: v for t, v in allf.items() if t.endswith("-lat")}
    if not files:
        print("no Part A results")
        return
    comet = json.loads((OUT / "comet-a.json").read_text())["scores"] if (OUT / "comet-a.json").exists() else {}
    order = [t for t in ("g26b", "e4b", "e2b", "tg4b", "tg4b-native", "tg12b") if t in files] + \
            [t for t in files if t not in ("g26b", "e4b", "e2b", "tg4b", "tg4b-native", "tg12b")]
    rows = {}
    for tag in order:
        j = files[tag]
        its = j["items"]
        for i, it in enumerate(its):
            it["comet"] = (comet.get(tag) or {}).get(str(i))
            it["chrf"] = CHRF.sentence_score(it.get("mt") or "", [it["ref"]]).score if "mt" in it else None
        rows[tag] = its
    print("\n## Part A: translators (30 sentences per direction; RAUM prompt + spark options unless noted)\n")
    print(f"models: " + "; ".join(f"{t} = {files[t]['model']} ({files[t]['prompt']} prompt, load {files[t].get('load_s')} s"
                                  f"{', stopped: ' + files[t]['stopped'] if files[t].get('stopped') else ''})" for t in order))
    dirs = ["de>en", "en>de", "de>ru", "ru>de", "en>ru", "ru>en"]

    def sel(its, d):
        return [it for it in its if "mt" in it and (d == "ALL" or f"{it['src_lang']}>{it['tgt_lang']}" == d or
                                                    (d == "DEMO" and "de" in (it["src_lang"], it["tgt_lang"])))]

    print("\n### quality per direction: chrF++ (corpus) / COMET-22 (mean x100) / guard fact mismatches\n")
    print("| model | " + " | ".join(dirs + ["demo 4", "all 6"]) + " |")
    print("|---" * (len(dirs) + 3) + "|")
    for tag in order:
        cells = []
        for d in dirs + ["DEMO", "ALL"]:
            s = sel(rows[tag], d)
            if not s:
                cells.append("-")
                continue
            chrf = CHRF.corpus_score([it["mt"] for it in s], [[it["ref"] for it in s]]).score
            cm = [it["comet"] for it in s if it["comet"] is not None]
            g = sum(1 for it in s if it.get("guard_red"))
            cells.append(f"{chrf:.1f} / {100 * sum(cm) / len(cm):.1f} / {g}" if cm else f"{chrf:.1f} / - / {g}")
        print(f"| {tag} | " + " | ".join(cells) + " |")
    print("\n(reference translations through the guard: "
          f"{sum(1 for it in rows[order[0]] if it.get('ref_guard_red'))} red of {len(rows[order[0]])})")

    base = {(it["sid"], it["src_lang"], it["tgt_lang"]): it for it in rows.get("g26b", [])}
    if base:
        print("\n### difference vs gemma4:26b, paired over the same sentences (mean, 95% bootstrap CI)\n")
        print("| model | n | dCOMET x100 demo 4 | dCOMET x100 all | dchrF++ (sent.) all | sentences COMET -5 or worse | guard red (26b) |")
        print("|---|---|---|---|---|---|---|")
        for tag in order:
            if tag == "g26b":
                continue
            pairs = [(it, base.get((it["sid"], it["src_lang"], it["tgt_lang"]))) for it in rows[tag] if "mt" in it]
            pairs = [(a, b) for a, b in pairs if b and "mt" in b]
            dc = [100 * (a["comet"] - b["comet"]) for a, b in pairs if a["comet"] is not None and b["comet"] is not None]
            dcd = [100 * (a["comet"] - b["comet"]) for a, b in pairs if a["comet"] is not None and b["comet"] is not None
                   and "de" in (a["src_lang"], a["tgt_lang"])]
            dch = [a["chrf"] - b["chrf"] for a, b in pairs]
            worse = sum(1 for x in dc if x <= -5)
            g = sum(1 for a, _ in pairs if a.get("guard_red"))
            gb = sum(1 for _, b in pairs if b.get("guard_red"))

            def ci(x):
                m, lo, hi = boot_ci(x)
                return "n/a" if m is None else f"{m:+.1f} [{lo:+.1f}, {hi:+.1f}]"
            print(f"| {tag} | {len(pairs)} | {ci(dcd)} | {ci(dc)} | {ci(dch)} | {worse} | {g} ({gb}) |")

    print("\n### latency on the GB10 (warm, sequential, under the shared latency lock; 10 sentences x 6 directions; ms)\n")
    print("| model | n | first p50 | first p90 | full p50 | full p90 | errors |")
    print("|---|---|---|---|---|---|---|")
    for tag in order:
        if tag not in latf:
            print(f"| {tag} | no locked latency run | | | | | |")
            continue
        ok = [it for it in latf[tag]["items"] if "mt" in it]
        print(f"| {tag} | {len(ok)} ", end="")
        print(f"| {f0(pct([it['first_ms'] for it in ok], 50))} | {f0(pct([it['first_ms'] for it in ok], 90))} | "
              f"{f0(pct([it['full_ms'] for it in ok], 50))} | {f0(pct([it['full_ms'] for it in ok], 90))} | "
              f"{sum(1 for it in latf[tag]['items'] if 'error' in it)} |")

    print("\n### guard mismatches on model outputs (kind: count) and the worst COMET drops vs 26b\n")
    for tag in order:
        c = Counter(m["kind"] for it in rows[tag] for m in (it.get("mismatches") or []))
        print(f"- {tag}: {dict(c) or 'none'}")
        for it in rows[tag]:
            if it.get("guard_red"):
                print(f"    {it['sid']} {it['src_lang']}>{it['tgt_lang']}: {it['mismatches']}\n      src: {it['src']}\n      mt : {it['mt']}")
    for tag in order:
        if tag == "g26b" or not base:
            continue
        drops = []
        for it in rows[tag]:
            b = base.get((it["sid"], it["src_lang"], it["tgt_lang"]))
            if b and it.get("comet") is not None and b.get("comet") is not None:
                drops.append((100 * (it["comet"] - b["comet"]), it, b))
        drops.sort(key=lambda x: x[0])
        print(f"\n- {tag} worst 4 vs 26b:")
        for d, it, b in drops[:4]:
            print(f"    {d:+.1f} {it['sid']} {it['src_lang']}>{it['tgt_lang']}\n      src : {it['src']}\n      26b : {b['mt']}\n      {tag:4}: {it['mt']}")


# ------------------------------------------------------------------ Part B
NUM = ("wrong_number", "wrong_unit", "date_shifted")
NEGALL = ("negation", "allergy_omitted")


def part_b():
    items = {it["key"]: it for it in json.loads((OUT / "b-items.json").read_text())}
    cfgs = {}
    # baseline gpt-oss:120b (g3b run, same items)
    cfgs["oss120-bt (g3b baseline)"] = {"kind": "sim", "model": "gpt-oss:120b", "rows": {
        k: {"similarity": it["base"].get("similarity"), "ms": it["base"].get("ms"), "seq": True, "back": it["base"].get("back")}
        for k, it in items.items()}}
    latb = {}
    for p in sorted(OUT.glob("b-*-lat.json")):
        j = json.loads(p.read_text())
        latb[j["tag"][:-4]] = [r for r in j["items"] if r.get("seq")]
    for p in sorted(OUT.glob("b-*.json")):
        if p.name == "b-items.json" or p.name.endswith("-lat.json"):
            continue
        j = json.loads(p.read_text())
        kind = "judge" if j["mode"] == "judge" else "sim"
        cfgs[j["tag"]] = {"kind": kind, "model": j["model"], "think": j.get("think"), "load_s": j.get("load_s"),
                          "stopped": j.get("stopped"), "rows": {r["key"]: r for r in j["items"]},
                          "latrows": latb.get(j["tag"])}
    if "oss120-bt" in latb:
        cfgs["oss120-bt (g3b baseline)"]["latrows"] = latb["oss120-bt"]
    for name in ("large", "xl"):
        p = OUT / f"qe-{name}.json"
        if p.exists():
            j = json.loads(p.read_text())
            lj = json.loads((OUT / f"qe-{name}-lat.json").read_text()) if (OUT / f"qe-{name}-lat.json").exists() else {}
            cfgs[f"metricx24-{name} QE"] = {"kind": "qe", "model": j["model"], "lat": {**(j.get("latency") or {}), **(lj.get("latency") or {})},
                                           "rows": {k: {"qe": v} for k, v in j["scores"].items()}}

    def flag(cfg, r, thr=None):
        if r is None:
            return None
        if cfg["kind"] == "sim":
            s = r.get("similarity")
            return None if s is None else s < (thr if thr is not None else RED)
        if cfg["kind"] == "judge":
            return None if r.get("same") is None else (r["same"] is False)
        if cfg["kind"] == "qe":
            return None if r.get("qe") is None else r["qe"] > thr

    def group(keys_filter):
        return [k for k, it in items.items() if keys_filter(it)]

    G = {
        "neg+allergy (16)": group(lambda it: it["kind"] == "pert" and it["perturbation"] in NEGALL),
        "subtle semantic (40)": group(lambda it: it["kind"] == "pert" and it["perturbation"] == "semantic"),
        "numbers/unit/date (24)": group(lambda it: it["kind"] == "pert" and it["perturbation"] in NUM),
        "FA clean ref (120)": group(lambda it: it["kind"] == "ref"),
        "flag live MT (120)": group(lambda it: it["kind"] == "mt"),
    }

    def stats(cfg, thr=None):
        out = {}
        for g, keys in G.items():
            fl = [flag(cfg, cfg["rows"].get(k), thr) for k in keys]
            ev = [x for x in fl if x is not None]
            out[g] = (sum(ev), len(ev))
        return out

    def qe_thr(cfg, max_fa):
        """lowest threshold (most catches) with <= max_fa clean-ref flags"""
        refs = sorted((cfg["rows"].get(k) or {}).get("qe") for k in G["FA clean ref (120)"] if (cfg["rows"].get(k) or {}).get("qe") is not None)
        if not refs:
            return None
        return refs[-(max_fa + 1)] if max_fa < len(refs) else 0.0

    def sim_thr(cfg, max_fa):
        refs = sorted((cfg["rows"].get(k) or {}).get("similarity") for k in G["FA clean ref (120)"]
                      if (cfg["rows"].get(k) or {}).get("similarity") is not None)
        if not refs:
            return None
        return refs[max_fa] if max_fa < len(refs) else 1.01  # red if s < thr -> exactly max_fa refs below

    def lat(cfg):
        if cfg["kind"] == "qe":
            l = cfg.get("lat") or {}
            g = l.get("cuda") or {}
            return g.get("p50"), g.get("p90")
        src = cfg.get("latrows") if cfg.get("latrows") is not None else [r for r in cfg["rows"].values() if r.get("seq")]
        ms = [r.get("ms") for r in src if "error" not in r]
        return pct(ms, 50), pct(ms, 90)

    def frac(t):
        a, b = t
        return f"{a}/{b}" if b else "-"

    print("\n## Part B: second-AI checker (g3b items: 120 records en/ru/tr/ar; clean ref, perturbed, live gemma4:26b MT)\n")
    print("Back-translation configs: red if bge-m3 cosine < 0.83 (RAUM's red); judge: red if same_meaning=false; "
          "MetricX QE: red if score > t, t set so clean refs give at most 1 and 2 false alarms (higher score = worse).\n")
    print("| checker | " + " | ".join(G) + " | p50 ms | p90 ms | errors |")
    print("|---" * (len(G) + 4) + "|")
    for name, cfg in cfgs.items():
        errs = sum(1 for r in cfg["rows"].values() if "error" in r or (cfg["kind"] == "judge" and r.get("same") is None))
        p50, p90 = lat(cfg)
        if cfg["kind"] == "qe":
            for fa in (1, 2):
                t = qe_thr(cfg, fa)
                st = stats(cfg, t)
                print(f"| {name} t={t:.2f} | " + " | ".join(frac(st[g]) for g in G) + f" | {f0(p50)} | {f0(p90)} | {errs} |")
        else:
            st = stats(cfg)
            print(f"| {name} | " + " | ".join(frac(st[g]) for g in G) + f" | {f0(p50)} | {f0(p90)} | {errs} |")
            if cfg["kind"] == "sim":
                for fa in (1, 2):
                    t = sim_thr(cfg, fa)
                    st = stats(cfg, t)
                    print(f"| {name} @t={t:.3f} (FA<={fa}) | " + " | ".join(frac(st[g]) for g in G) + " | | | |")
    # latency split for bt configs
    print("\n### latency detail (sequential subset; ms)\n")
    for name, cfg in cfgs.items():
        rs = cfg.get("latrows") if cfg.get("latrows") is not None else [r for r in cfg["rows"].values() if r.get("seq")]
        rs = [r for r in rs if "error" not in r]
        if cfg["kind"] == "sim" and rs and "bt_ms" in rs[0]:
            print(f"- {name}: n={len(rs)} total p50 {f0(pct([r['ms'] for r in rs], 50))} p90 {f0(pct([r['ms'] for r in rs], 90))}"
                  f" | back-translation p50 {f0(pct([r['bt_ms'] for r in rs], 50))} | embed p50 {f0(pct([r['embed_ms'] for r in rs], 50))}"
                  f"{' | load ' + str(cfg.get('load_s')) + ' s' if cfg.get('load_s') else ''}")
        elif cfg["kind"] == "judge" and rs:
            print(f"- {name}: n={len(rs)} p50 {f0(pct([r['ms'] for r in rs], 50))} p90 {f0(pct([r['ms'] for r in rs], 90))}"
                  f" | output tokens p50 {f0(pct([r.get('eval_count') for r in rs], 50))} | load {cfg.get('load_s')} s")
        elif cfg["kind"] == "qe":
            print(f"- {name}: {json.dumps(cfg.get('lat'))}")
        elif rs and name.startswith("oss120"):
            print(f"- {name}: g3b run under other teams' CPU load: p50 {f0(pct([r['ms'] for r in rs], 50))} p90 {f0(pct([r['ms'] for r in rs], 90))}")

    # combinations
    print("\n### combinations (red if any signal is red)\n")
    names = list(cfgs)
    judges = [n for n in names if cfgs[n]["kind"] == "judge"]
    sims = [n for n in names if cfgs[n]["kind"] == "sim"]
    qes = [n for n in names if cfgs[n]["kind"] == "qe"]
    combos = [(j, s) for j in judges for s in sims] + [(j, q) for j in judges for q in qes] + [(s, q) for s in sims for q in qes]
    print("| combination | " + " | ".join(G) + " |")
    print("|---" * (len(G) + 1) + "|")
    for a, b in combos:
        ta = qe_thr(cfgs[a], 1) if cfgs[a]["kind"] == "qe" else None
        tb = qe_thr(cfgs[b], 1) if cfgs[b]["kind"] == "qe" else None
        cells = []
        for g, keys in G.items():
            n = c = 0
            for k in keys:
                fa_, fb_ = flag(cfgs[a], cfgs[a]["rows"].get(k), ta), flag(cfgs[b], cfgs[b]["rows"].get(k), tb)
                if fa_ is None and fb_ is None:
                    continue
                n += 1
                c += bool(fa_) or bool(fb_)
            cells.append(f"{c}/{n}")
        print(f"| {a} + {b}{' (QE t at FA<=1)' if (ta or tb) else ''} | " + " | ".join(cells) + " |")

    # per-item detail for the judge configs: misses and false alarms
    for name in judges + [n for n in sims if not n.startswith("oss120-bt (")]:
        cfg = cfgs[name]
        print(f"\n### {name}: misses on subtle/neg/allergy, flags on clean refs and live MT\n")
        for k, it in items.items():
            r = cfg["rows"].get(k)
            if r is None:
                continue
            f = flag(cfg, r)
            bad = it["kind"] == "pert" and it["perturbation"] not in NUM
            if (bad and not f) or (it["kind"] in ("ref", "mt") and f):
                what = "MISS" if bad else ("FA-ref" if it["kind"] == "ref" else "flag-MT")
                extra = r.get("diff") if cfg["kind"] == "judge" else f"sim {r.get('similarity')} back: {r.get('back')}"
                print(f"- {what} {k} {it.get('perturbation') or ''}: {it['tgt'][:110]} | {extra}")


if __name__ == "__main__":
    part_a()
    part_b()
