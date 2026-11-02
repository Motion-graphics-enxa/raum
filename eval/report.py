#!/usr/bin/env python3
"""Render eval/out/* into the §12.2 proof table (markdown) for slide 4 and the README.

    uv run --project core python eval/report.py
    uv run --project core python eval/report.py --qr-seconds 7.4,8.1,6.9 --third-party-bytes 0

Reads whatever exists (missing inputs show as "not measured"):
  eval/out/report.json    run_eval.py   guard / checker catch and false-alarm rates
  eval/out/bakeoff.json   bakeoff.py    translator table, native rating
  eval/out/load.csv       load_test.py  latency per N, capacity at p95 < 3 s
  eval/out/privacy.json   optional      {"third_party_bytes": 0}  (or --third-party-bytes, or nft on the box)
Writes eval/out/proof.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import OUT_DIR, fmt_pct, md_table, now_iso


def _json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_rows(p: Path) -> list[dict[str, Any]]:
    try:
        with p.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    for r in rows:
        for k, v in list(r.items()):
            if k == "models":
                continue
            try:
                r[k] = float(v) if v not in ("", None) else None
            except ValueError:
                pass
    return sorted(rows, key=lambda r: r["n_rooms"] or 0)


def _s(ms: float | None) -> str:
    return "not measured" if ms is None else f"{ms / 1000:.2f} s"


def nft_bytes() -> int | None:
    """Third-party bytes straight from the box's nftables counter (needs root; silent otherwise)."""
    if not shutil.which("nft"):
        return None
    try:
        out = subprocess.run(["nft", "-j", "list", "counters", "table", "inet", "raum"], capture_output=True,
                             text=True, timeout=3, check=True).stdout
        for item in json.loads(out).get("nftables", []):
            c = item.get("counter")
            if c and c.get("name") == "third_party":
                return int(c.get("bytes", 0))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


def build(args: argparse.Namespace) -> str:
    out = Path(args.out)
    rep, bake, priv = _json(out / "report.json"), _json(out / "bakeoff.json"), _json(out / "privacy.json")
    load = _load_rows(out / "load.csv")
    rows: list[list[str]] = []

    def add(metric: str, how: str, target: str, value: str, ok: bool | None) -> None:
        rows.append([metric, how, target, value, {True: "yes", False: "**no**", None: ""}[ok]])

    # latency (load test, N = smallest run with timings)
    base = next((r for r in load if (r.get("timing_events") or 0) > 0), None)
    if base:
        first = base.get("e2e_first_ms_p50") or base.get("mt_first_ms_p50")
        first95 = base.get("e2e_first_ms_p95") or base.get("mt_first_ms_p95")
        full = base.get("e2e_full_ms_p50") or base.get("mt_full_ms_p50")
        full95 = base.get("e2e_full_ms_p95") or base.get("mt_full_ms_p95")
        n = int(base["n_rooms"])
        add("First words on screen after speaker stops", f"load test, {n} room(s), client-observed", "p50 < 1.0 s",
            f"p50 {_s(first)} · p95 {_s(first95)}", None if first is None else first < 1000)
        add("Full sentence", "same", "p50 < 1.5 s", f"p50 {_s(full)} · p95 {_s(full95)}",
            None if full is None else full < 1500)
    else:
        add("First words on screen after speaker stops", "load test", "p50 < 1.0 s", "not measured", None)
        add("Full sentence", "load test", "p50 < 1.5 s", "not measured", None)
    if args.qr_seconds:
        qs = [float(x) for x in args.qr_seconds.split(",") if x.strip()]
        add("QR scan to first translated sentence", f"stopwatch, {len(qs)} runs", "< 10 s",
            f"mean {sum(qs) / len(qs):.1f} s · max {max(qs):.1f} s", max(qs) < 10)
    else:
        add("QR scan to first translated sentence", "stopwatch, 3 runs (--qr-seconds)", "< 10 s", "not measured",
            None)
    if args.wer_raw is not None and args.wer_clean is not None:
        add("Transcription error rate in the hall", "hall recordings, without / with DeepFilterNet",
            "lower with cleaning", f"{args.wer_raw:.1%} → {args.wer_clean:.1%}", args.wer_clean < args.wer_raw)
    else:
        add("Transcription error rate in the hall", "hall recordings (--wer-raw/--wer-clean)", "lower with cleaning",
            "not measured", None)

    # checks
    if rep:
        s = rep["summary"]
        draft = "" if rep["n_approved"] == rep["n_records"] else \
            f" (draft set: {rep['n_approved']}/{rep['n_records']} approved)"
        g = s["guard"]
        add("Fact guard on perturbed numbers", f"eval, {rep['mode']}{draft}", "100% caught",
            fmt_pct(g["numbers_rate"]) if rep["guard_available"] else "guard not available",
            None if g["numbers_rate"] is None else g["numbers_rate"] >= 1.0)
        sem = s.get("check", {}).get("semantic_rate")
        add("Checker on semantic perturbations", "eval, back-translation + bge-m3" if sem is not None
            else "eval (needs --profile spark)", "≥ 80% caught", fmt_pct(sem), None if sem is None else sem >= 0.8)
        fa = s.get("combined", g)["false_alarms"]
        add("False alarms on clean set", "eval" + (", guard + checker" if "combined" in s else ", guard only"),
            "< 10%", f"{fmt_pct(fa['rate'])} ({fa['red']}/{fa['n']})" if fa["n"] else "n/a",
            None if fa["rate"] is None else fa["rate"] < 0.10)
    else:
        for m, t in (("Fact guard on perturbed numbers", "100% caught"),
                     ("Checker on semantic perturbations", "≥ 80% caught"), ("False alarms on clean set", "< 10%")):
            add(m, "eval/run_eval.py", t, "not measured", None)

    win = (bake or {}).get("winner") or {}
    nr = win.get("native_rating")
    dry = " (DRY RUN)" if (bake or {}).get("dry_run") else ""
    add("Native-speaker rating", f"1–5 on the bake-off winner `{win.get('model', '?')}`{dry}" if win else "bake-off",
        "≥ 4", "not rated" if nr is None else f"{nr:.1f}", None if nr is None or dry else nr >= 4)

    measured = [r for r in load if (r.get("timing_events") or 0) > 0]
    if measured:
        def p95(r: dict) -> float | None:
            return r.get("e2e_full_ms_p95") or r.get("mt_full_ms_p95")

        okn = [int(r["n_rooms"]) for r in measured if p95(r) is not None and p95(r) < 3000]
        tried = ", ".join(str(int(r["n_rooms"])) for r in measured)
        add("Conversations at once with p95 < 3 s", f"load test, N in {{{tried}}}", "measured value",
            str(max(okn)) if okn else f"< {int(measured[0]['n_rooms'])}", None)
    else:
        add("Conversations at once with p95 < 3 s", "eval/load_test.py", "measured value", "not measured", None)

    tp = args.third_party_bytes
    src = "--third-party-bytes"
    if tp is None and priv and "third_party_bytes" in priv:
        tp, src = int(priv["third_party_bytes"]), "eval/out/privacy.json"
    if tp is None:
        tp, src = nft_bytes(), "nft counter third_party"
    add("Bytes to third parties", f"privacy meter ({src})" if tp is not None else "privacy meter", "0",
        "not measured" if tp is None else f"{tp:,} B", None if tp is None else tp == 0)

    L = ["# RAUM proof table (§12.2)", "", f"Generated {now_iso()} from {out}.", "",
         md_table(["Metric", "How", "Target", "Value", "Met"], rows), ""]
    if bake and bake.get("summary"):
        L += ["## Translator bake-off" + (" (DRY RUN)" if bake.get("dry_run") else ""), "",
              md_table(["Candidate", "First token p50", "Full p50", "Guard pass", "Back-transl. sim", "Native"],
                       [[f"`{c['model']}`", _s(c["first_p50"]), _s(c["full_p50"]), fmt_pct(c["guard_pass"]),
                         "n/a" if c["back_sim"] is None else f"{c['back_sim']:.3f}",
                         "–" if c["native_rating"] is None else f"{c['native_rating']:.1f}"]
                        for c in bake["summary"]]), ""]
    if load:
        L += ["## Load test", "",
              md_table(["Rooms", "Utterances", "Timings", "First words p50", "p95", "Full p50", "p95",
                        "MT first p50 (agent)", "Agent in rooms"],
                       [[int(r["n_rooms"]), int(r["utterances_sent"] or 0), int(r["timing_events"] or 0),
                         _s(r.get("e2e_first_ms_p50")), _s(r.get("e2e_first_ms_p95")), _s(r.get("e2e_full_ms_p50")),
                         _s(r.get("e2e_full_ms_p95")), _s(r.get("mt_first_ms_p50")),
                         f"{int(r.get('rooms_with_agent') or 0)}/{int(r['n_rooms'])}"] for r in load]), ""]
        engines = sorted({str(r["models"]) for r in load if r.get("models")})
        if engines:
            L += [f"Engines reported by the agent: {' | '.join(f'`{e}`' for e in engines)}"
                  + (" (**mock pipeline: not a real-model number**)" if any("mock" in e for e in engines) else ""), ""]
        if (out / "load.png").exists():
            L += ["![latency per conversations at once](load.png)", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--qr-seconds", help="stopwatch runs, comma list, e.g. 7.4,8.1,6.9")
    ap.add_argument("--wer-raw", type=float, help="hall WER without DeepFilterNet, 0..1")
    ap.add_argument("--wer-clean", type=float, help="hall WER with DeepFilterNet, 0..1")
    ap.add_argument("--third-party-bytes", type=int)
    args = ap.parse_args(argv)
    md = build(args)
    p = Path(args.out) / "proof.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(md, encoding="utf-8")
    print(md)
    print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
