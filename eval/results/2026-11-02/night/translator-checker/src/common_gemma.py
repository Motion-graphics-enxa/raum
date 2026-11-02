"""eval-gemma shared bits: repo import paths, abort/deadline checks, item builders, percentiles."""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

REPO = Path.home() / "Desktop/HackathonRaum/raum"
HERE = Path(__file__).resolve().parent.parent  # runtime/eval-gemma
sys.path.insert(0, str(REPO / "eval"))
sys.path.insert(0, str(REPO / "core"))
os.environ.setdefault("RAUM_HOME", str(REPO))

import common  # noqa: E402  (eval/common.py: load_sets, Guard, timed_stream, cosine)

OLLAMA = "http://127.0.0.1:11434"
ABORT = HERE / "out" / "ABORT"


def should_stop(deadline: float | None) -> str | None:
    if ABORT.exists():
        return "abort file"
    if deadline and time.time() > deadline:
        return "deadline"
    return None


def pct(v, q):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * q / 100
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


DIRS = [("de", "en"), ("en", "de"), ("de", "ru"), ("ru", "de"), ("en", "ru"), ("ru", "en")]


def a_items() -> list[dict]:
    """30 sentence ids x 6 directions. Every sentence id has a German, an English and a Russian text
    (source or reference in the eval sets; the German side is shared across languages)."""
    by: dict[str, dict] = {}
    for r in common.load_sets(langs=["en", "ru"]):
        d = by.setdefault(r["sid"], {"room": r["room"], "sid": r["sid"]})
        de_text = r["src"] if r["src_lang"] == "de" else r["ref"]
        l_text = r["ref"] if r["src_lang"] == "de" else r["src"]
        if "de" in d and d["de"] != de_text:
            print(f"warning: German text differs across languages for {r['sid']}", file=sys.stderr)
        d.setdefault("de", de_text)
        d[r["lang"]] = l_text
        d["orig"] = "de" if r["src_lang"] == "de" else "xx"  # which side the sentence was written in
    out = []
    for s, t in DIRS:
        for sid in sorted(by, key=lambda x: (by[x]["room"], x)):
            d = by[sid]
            out.append({"sid": sid, "room": d["room"], "src_lang": s, "tgt_lang": t, "src": d[s], "ref": d[t],
                        "orig": d["orig"]})
    return out


def b_items() -> list[dict]:
    """The g3b checker items: clean ref, perturbed and the baseline run's live gemma4:26b MT, 120 records."""
    base = {}
    for p in sorted((HERE / "baseline").glob("g3b-*.json")):
        for r in json.loads(p.read_text())["records"]:
            base[r["id"]] = r
    out = []
    for r in common.load_sets(langs=["en", "ru", "tr", "ar"]):
        b = base.get(r["id"], {})
        common_f = {"id": r["id"], "lang": r["lang"], "room": r["room"], "perturbation": r.get("perturbation"),
                    "src": r["src"], "src_lang": r["src_lang"], "tgt_lang": r["tgt_lang"]}
        out.append({**common_f, "kind": "ref", "tgt": r["ref"], "base": (b.get("check_ref") or {})})
        if r.get("perturbed"):
            out.append({**common_f, "kind": "pert", "tgt": r["perturbed"]["tgt_text"], "base": (b.get("check_pert") or {}),
                        "note": r["perturbed"].get("note")})
        mt = (b.get("mt") or {})
        if mt.get("text"):
            out.append({**common_f, "kind": "mt", "tgt": mt["text"], "base": {k: mt.get(k) for k in
                        ("similarity", "back", "ms", "bt_ms", "embed_ms", "guard_red")}})
    for i, it in enumerate(out):
        it["key"] = f"{it['id']}:{it['kind']}"
        it["base"] = {k: it["base"].get(k) for k in ("similarity", "back", "ms", "bt_ms", "embed_ms", "guard_red")
                      if k in it["base"]}
    return out
