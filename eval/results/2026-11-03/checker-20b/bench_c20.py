#!/usr/bin/env python3
"""checker-20b: RAUM's production meaning check on the 320 g3b checker items (eval-gemma out/b-items.json).

Exactly the live check (raum.verify.backtranslate.second_check's two calls):
  1. raum.pipeline.verifier.OllamaVerifier.back_translate (prompts/backtranslate.md, spark verify options:
     reasoning low -> think "low", num_predict cap 64..256 + one retry at 256 on done_reason=length)
  2. raum.pipeline.embed.OllamaEmbedder (bge-m3) cosine(src, back); badge via raum.verify.similarity.badge_for
     with the box's raum.yaml thresholds (green 0.86 / amber 0.83: red < 0.83).
  Plus the current fact guard (CPU, eval/common.Guard) so the production badge (guard red wins) can be shown.

Only change vs production: options.num_ctx 8192 is sent explicitly (Ollama's default on this box is 8192, so the
same runner is used; required so a different context never reloads the model).

Safety for the shared box / live phone test:
  - concurrency 1 (one Ollama call at a time)
  - before EVERY Ollama call: core /api/rooms; any room with participants or subscribers > 0 (or the endpoint
    unreachable) -> pause, poll every 5 s until no live room for 10 s; every pause is logged to pauses.jsonl
    (open but empty rooms are ignored: ops correction 3 Nov 10:40; the first pause used /api/health `rooms`)
  - before every call: /api/ps must list gpt-oss:20b and bge-m3 at context 8192 (never trigger a load)
  - MemAvailable < 8 GiB -> stop
  - file ABORT in this folder -> stop
Resumable: one JSON line per item in results.jsonl; items already there without an error are skipped.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path

import httpx

REPO = Path.home() / "Desktop/HackathonRaum/raum"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "core"))
sys.path.insert(0, str(REPO / "eval"))
os.environ["RAUM_HOME"] = str(REPO)
os.environ["RAUM_PROFILE"] = "spark"

from raum.config import load_config  # noqa: E402
from raum.pipeline.embed import OllamaEmbedder  # noqa: E402
from raum.pipeline.verifier import OllamaVerifier  # noqa: E402
from raum.verify.similarity import badge_for, cosine  # noqa: E402

import common as evalcommon  # noqa: E402  (eval/common.py: Guard)

OLLAMA = "http://127.0.0.1:11434"
HEALTH = "http://127.0.0.1:8000/api/health"
ROOMS = "http://127.0.0.1:8000/api/rooms"
MEM_FLOOR_KB = 8 * 1024 * 1024
RESULTS = HERE / "results.jsonl"
PAUSES = HERE / "pauses.jsonl"
ABORT = HERE / "ABORT"
STOPFILE = HERE / "STOPPED"


def ts() -> str:
    return time.strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"{ts()} {msg}", flush=True)


def append(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


class Stop(Exception):
    pass


def mem_available_kb() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    return 0


class Gate:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.c = client
        self.paused_s = 0.0
        self.n_pauses = 0
        self.n_checks = 0
        self.last_live: list[str] = []

    async def rooms(self) -> int | None:
        """Number of LIVE rooms: /api/rooms entries with participants or subscribers > 0 and idle_s <= 600 (or null)
        (ops, 3 Nov 10:40 and 11:34: /api/health `rooms` also counts open but empty rooms; rooms idle > 600 s are
        ignored). None = unreachable (pause, retry every 5 s)."""
        try:
            r = await self.c.get(ROOMS, timeout=3)
            live = [x for x in r.json() if (x.get("participants") or (x.get("subscribers") or 0) > 0)
                    and (x.get("idle_s") is None or x["idle_s"] <= 600)]
            self.last_live = [x.get("room") for x in live]
            return len(live)
        except Exception:  # noqa: BLE001
            self.last_live = []
            return None

    async def resident(self) -> None:
        r = await self.c.get(f"{OLLAMA}/api/ps", timeout=5)
        ms = {m["name"]: m for m in r.json().get("models", [])}
        for name in ("gpt-oss:20b", "bge-m3:latest"):
            m = ms.get(name)
            if m is None:
                raise Stop(f"{name} not resident in /api/ps (would trigger a load): {sorted(ms)}")
            if m.get("context_length") not in (None, 8192):
                raise Stop(f"{name} context_length {m.get('context_length')} != 8192")

    def safety(self) -> None:
        if ABORT.exists():
            raise Stop("ABORT file")
        kb = mem_available_kb()
        if kb < MEM_FLOOR_KB:
            raise Stop(f"MemAvailable {kb / 1048576:.1f} GiB < 8 GiB")

    async def wait(self, what: str) -> float:
        """Before every Ollama call. Returns seconds paused."""
        self.n_checks += 1
        self.safety()
        n = await self.rooms()
        if n == 0:
            await self.resident()
            return 0.0
        t0 = time.time()
        self.n_pauses += 1
        log(f"PAUSE before {what}: live rooms={n} {self.last_live}")
        seen = [n]
        names = set(self.last_live)
        zero_since = None
        while True:
            await asyncio.sleep(5)
            self.safety()
            n = await self.rooms()
            seen.append(n)
            names |= set(self.last_live)
            now = time.time()
            if n == 0:
                zero_since = zero_since or now
                if now - zero_since >= 10:
                    break
            else:
                zero_since = None
        dur = time.time() - t0
        self.paused_s += dur
        append(PAUSES, {"start": t0, "end": time.time(), "start_hms": time.strftime("%H:%M:%S", time.localtime(t0)),
                        "dur_s": round(dur, 1), "before": what, "max_rooms": max((x or 0) for x in seen),
                        "rooms_unreachable_polls": sum(1 for x in seen if x is None), "polls": len(seen),
                        "live_rooms": sorted(n for n in names if n), "rule": "api/rooms (participants|subscribers) and idle_s<=600|null"})
        log(f"RESUME after {dur:.0f} s")
        await self.resident()
        return dur


class GatedVerifier(OllamaVerifier):
    """The production verifier; only adds the live gate before each call and records each call's metadata."""

    def __init__(self, *a, livegate: Gate, **k) -> None:
        super().__init__(*a, **k)
        self._livegate = livegate
        self.calls: list[dict] = []

    async def _post(self, messages, *, fmt=None, num_predict=None, purpose=None):
        self.calls.append({"paused_s": await self._livegate.wait("check")})
        body = self._body(messages, fmt=fmt, num_predict=num_predict)
        t0 = time.perf_counter()
        j = await super()._post(messages, fmt=fmt, num_predict=num_predict, purpose=purpose)
        wall = (time.perf_counter() - t0) * 1e3
        msg = j.get("message") or {}
        self.calls[-1] |= {
            "wall_ms": round(wall, 1), "num_predict": body["options"].get("num_predict"),
            "think": body.get("think"), "done_reason": j.get("done_reason"), "eval_count": j.get("eval_count"),
            "prompt_eval_count": j.get("prompt_eval_count"),
            "total_ms": round((j.get("total_duration") or 0) / 1e6, 1),
            "load_ms": round((j.get("load_duration") or 0) / 1e6, 1),
            "thinking_chars": len(msg.get("thinking") or ""), "content_chars": len(msg.get("content") or "")}
        return j


async def main() -> int:
    items = json.loads((HERE / "b-items.json").read_text())
    random.Random(7).shuffle(items)  # mixed kinds / languages, so a partial run is still balanced
    done = set()
    if RESULTS.exists():
        for line in RESULTS.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if "error" not in r:
                    done.add(r["key"])
    todo = [it for it in items if it["key"] not in done]
    if len(sys.argv) > 1:  # optional limit for a smoke run
        todo = todo[: int(sys.argv[1])]

    cfg = load_config()
    thr = cfg.thresholds
    vopts = dict(cfg.models.verify.options or {})
    vopts["num_ctx"] = 8192
    client = httpx.AsyncClient()
    gate = Gate(client)
    ver = GatedVerifier(cfg.models.verify.url or OLLAMA, model=cfg.models.verify.model, options=vopts, livegate=gate)
    emb = OllamaEmbedder(cfg.models.embed.url or OLLAMA, model=cfg.models.embed.model, options=cfg.models.embed.options)
    guard = evalcommon.Guard()
    meta = {"started": time.time(), "verify_model": cfg.models.verify.model, "verify_options_profile":
            cfg.models.verify.options, "verify_options_sent": vopts, "embed_model": cfg.models.embed.model,
            "thresholds": {"green": thr.green, "amber": thr.amber}, "guard_available": guard.available,
            "example_body": {k: v for k, v in ver._body([{"role": "user", "content": "x"}], num_predict=64).items()
                             if k != "messages"},
            "n_items": len(items), "already_done": len(done), "todo": len(todo)}
    (HERE / f"meta-{int(time.time())}.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    log(f"start: {len(items)} items, {len(done)} done, {len(todo)} to do; model {cfg.models.verify.model} "
        f"options {vopts}; thresholds {thr.green}/{thr.amber}; body {meta['example_body']}")

    errs_in_row = 0
    stop_reason = None
    try:
        for i, it in enumerate(todo):
            row = {k: it.get(k) for k in ("key", "id", "lang", "room", "kind", "perturbation", "src", "src_lang",
                                          "tgt", "tgt_lang")}
            ver.calls = []
            try:
                back = await ver.back_translate(it["tgt"], it["tgt_lang"], it["src_lang"])
                bt_ms = sum(c.get("wall_ms", 0) for c in ver.calls)
                p_emb = await gate.wait("embed")
                t1 = time.perf_counter()
                va, vb = await emb.embed([it["src"], back])
                embed_ms = (time.perf_counter() - t1) * 1e3
                sim = round(cosine(va, vb), 4)
                g = guard.check(it["room"], it["src"], it["src_lang"], it["tgt"], it["tgt_lang"])
                badge = badge_for(sim, thr)
                row |= {"back": back, "similarity": sim, "badge": badge, "guard_red": g.red,
                        "guard_mismatches": g.mismatches, "combined": "red" if g.red else badge,
                        "bt_ms": round(bt_ms, 1), "embed_ms": round(embed_ms, 1), "ms": round(bt_ms + embed_ms, 1),
                        "n_calls": len(ver.calls), "retried": len(ver.calls) > 1, "calls": ver.calls,
                        "paused_s": round(sum(c.get("paused_s", 0) for c in ver.calls) + p_emb, 1),
                        "t": time.time()}
                errs_in_row = 0
            except Stop:
                raise
            except Exception as e:  # noqa: BLE001
                row |= {"error": repr(e), "calls": ver.calls, "t": time.time()}
                errs_in_row += 1
            append(RESULTS, row)
            if "error" in row:
                log(f"[{i + 1}/{len(todo)}] {row['key']} ERROR {row['error'][:200]}")
                if errs_in_row >= 5:
                    raise Stop("5 errors in a row")
            else:
                log(f"[{i + 1}/{len(todo)}] {row['key']} sim {row['similarity']} {row['badge']} "
                    f"guard {'red' if row['guard_red'] else '-'} bt {row['bt_ms']:.0f} ms"
                    f"{' RETRY' if row['retried'] else ''} embed {row['embed_ms']:.0f} ms")
            await asyncio.sleep(0.3)
    except Stop as e:
        stop_reason = str(e)
        log(f"STOP: {stop_reason}")
        STOPFILE.write_text(f"{ts()} {stop_reason}\n")
    finally:
        log(f"end: paused {gate.paused_s:.0f} s over {gate.n_pauses} pauses; {gate.n_checks} gate checks")
        await ver.aclose()
        await emb.aclose()
        await client.aclose()
    return 2 if stop_reason else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
