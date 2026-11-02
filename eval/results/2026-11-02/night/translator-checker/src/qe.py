#!/usr/bin/env python3
"""eval-gemma metrics container (NGC PyTorch + venvs/qe): COMET-22 for Part A, MetricX-24 QE for Part B.

  comet     Unbabel/wmt22-comet-da (Apache-2.0) on every out/a-*.json translation (src, mt, ref)
  metricx   google/metricx-24-hybrid-{large,xl}-v2p6-bfloat16 (Apache-2.0), reference-free (QE) mode,
            on out/b-items.json (the checker items); plus batch-1 latency on the device and on 4 CPU threads

Env: STEPS (comma list, default comet,metricx-large,metricx-xl,lat), DEVICE (cuda|cpu), SMOKE=1 (tiny models).
Output: out/comet-a.json, out/qe-<name>.json
"""
from __future__ import annotations

import glob
import json
import os
import statistics
import sys
import time
from pathlib import Path

import torch

W = Path("/work")
OUT = W / "out"
HF = Path(os.environ.get("HF_HOME", "/work/hf")) / "hub"
DEVICE = os.environ.get("DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
STEPS = os.environ.get("STEPS", "comet,metricx-large,metricx-xl,lat").split(",")
SMOKE = os.environ.get("SMOKE") == "1"


def snap(repo: str) -> Path:
    d = HF / ("models--" + repo.replace("/", "--")) / "snapshots"
    return sorted(d.iterdir())[-1]


def log(*a):
    print(time.strftime("%T"), *a, flush=True)


def stop() -> bool:
    return (OUT / "ABORT").exists()


def comet_step():
    from comet import load_from_checkpoint

    repo = "Unbabel/eamt22-cometinho-da" if SMOKE else "Unbabel/wmt22-comet-da"
    model = load_from_checkpoint(str(snap(repo) / "checkpoints" / "model.ckpt"))
    files = [f for f in sorted(glob.glob(str(OUT / "a-*.json"))) if not f.endswith("a-items.json")]
    files = [f for f in files if SMOKE == f.endswith("a-smoke.json") and not f.endswith("-lat.json")]
    data, keys = [], []
    for f in files:
        j = json.load(open(f))
        for i, it in enumerate(j["items"]):
            if it.get("mt") is not None:
                data.append({"src": it["src"], "mt": it["mt"], "ref": it["ref"]})
                keys.append((j["tag"], i))
    if SMOKE:
        data, keys = data[:8], keys[:8]
    log(f"comet {repo}: {len(data)} segments from {len(files)} files on {DEVICE}")
    t0 = time.perf_counter()
    pred = model.predict(data, batch_size=64, gpus=1 if DEVICE == "cuda" else 0, progress_bar=False)
    log(f"comet done in {time.perf_counter() - t0:.1f} s, system mean {pred.system_score:.4f}")
    res: dict[str, dict[str, float]] = {}
    for (tag, i), s in zip(keys, pred.scores):
        res.setdefault(tag, {})[str(i)] = round(float(s), 5)
    (OUT / ("comet-a-smoke.json" if SMOKE else "comet-a.json")).write_text(json.dumps({"model": repo, "scores": res}))
    del model
    torch.cuda.empty_cache() if DEVICE == "cuda" else None


class MetricX:
    def __init__(self, repo: str, device: str):
        import sentencepiece as spm
        from transformers import MT5ForConditionalGeneration

        # mT5 ids are the SentencePiece ids (pad 0, eos 1); MetricX drops the EOS, so plain sp.encode is the input
        self.sp = spm.SentencePieceProcessor(model_file=str(snap("google/mt5-small" if SMOKE else "google/mt5-xl") / "spiece.model"))
        self.model = MT5ForConditionalGeneration.from_pretrained(str(snap(repo)), dtype=torch.bfloat16).to(device).eval()
        self.device = device

    @torch.inference_mode()
    def score(self, pairs: list[tuple[str, str]], bs: int = 16) -> list[float]:
        out = []
        for k in range(0, len(pairs), bs):
            texts = [f"source: {s} candidate: {h}" for s, h in pairs[k:k + bs]]
            enc = [self.sp.encode(t)[:1535] for t in texts]  # = tokenizer(t, max_length=1536)["input_ids"][:-1]
            n = max(len(e) for e in enc)
            ids = torch.zeros((len(enc), n), dtype=torch.long)
            mask = torch.zeros((len(enc), n), dtype=torch.long)
            for r, e in enumerate(enc):
                ids[r, :len(e)] = torch.tensor(e)
                mask[r, :len(e)] = 1
            ids, mask = ids.to(self.device), mask.to(self.device)
            dec = torch.zeros((len(enc), 1), dtype=torch.long, device=self.device)
            logits = self.model(input_ids=ids, attention_mask=mask, decoder_input_ids=dec).logits
            out += torch.clamp(logits[:, 0, 250089].float(), 0, 25).tolist()  # 250089 = <extra_id_10>
        return out


def metricx_step(name: str):
    repo = {"large": "google/metricx-24-hybrid-large-v2p6-bfloat16",
            "xl": "google/metricx-24-hybrid-xl-v2p6-bfloat16"}[name]
    if SMOKE:
        repo = "google/mt5-small"
    items = json.load(open(OUT / "b-items.json"))
    if SMOKE:
        items = items[:8]
    t0 = time.perf_counter()
    mx = MetricX(repo, DEVICE)
    load_s = time.perf_counter() - t0
    pairs = [(it["src"], it["tgt"]) for it in items]
    score_on = os.environ.get("SCORE", "1") == "1"
    scores, batch_s = [], None
    if score_on:
        t0 = time.perf_counter()
        scores = mx.score(pairs)
        batch_s = time.perf_counter() - t0
        log(f"metricx {name}: {len(items)} items in {batch_s:.1f} s (batch 16) on {DEVICE}, load {load_s:.1f} s")
    lat = {}
    if "lat" in STEPS and not stop():
        sample = pairs[:40] if not SMOKE else pairs[:3]
        mx.score(sample[:3], bs=1)  # warm
        ms = []
        for p in sample:
            t = time.perf_counter()
            mx.score([p], bs=1)
            if DEVICE == "cuda":
                torch.cuda.synchronize()
            ms.append((time.perf_counter() - t) * 1e3)
        lat[DEVICE] = {"n": len(ms), "p50": statistics.median(ms), "p90": sorted(ms)[int(0.9 * (len(ms) - 1))],
                       "max": max(ms)}
        log(f"metricx {name} batch-1 latency on {DEVICE}: p50 {lat[DEVICE]['p50']:.0f} ms p90 {lat[DEVICE]['p90']:.0f} ms")
        if DEVICE == "cuda":
            lat["gpu_mem_gb"] = round(torch.cuda.max_memory_allocated() / 2**30, 2)
    if "cpu" in STEPS and name == "large" and not stop():
        torch.set_num_threads(4)
        cpu = MetricX(repo, "cpu")
        sample = pairs[:12] if not SMOKE else pairs[:2]
        cpu.score(sample[:1], bs=1)
        ms = []
        for p in sample:
            t = time.perf_counter()
            cpu.score([p], bs=1)
            ms.append((time.perf_counter() - t) * 1e3)
        lat["cpu4"] = {"n": len(ms), "p50": statistics.median(ms), "max": max(ms)}
        log(f"metricx {name} batch-1 latency on 4 CPU threads: p50 {lat['cpu4']['p50']:.0f} ms")
        del cpu
    res = {"model": repo, "device": DEVICE, "load_s": load_s, "batch_s": batch_s, "latency": lat,
           "scores": {it["key"]: round(s, 4) for it, s in zip(items, scores)}}
    fname = f"qe-{name}" + ("" if score_on else "-lat") + ("-smoke" if SMOKE else "") + ".json"
    (OUT / fname).write_text(json.dumps(res))
    del mx
    torch.cuda.empty_cache() if DEVICE == "cuda" else None


if __name__ == "__main__":
    log(f"torch {torch.__version__} device {DEVICE} cuda {torch.cuda.is_available()} steps {STEPS} smoke {SMOKE}")
    if DEVICE == "cuda":
        log(torch.cuda.get_device_name(0))
    for step in STEPS:
        if stop():
            log("ABORT file: stopping")
            sys.exit(2)
        if step == "comet":
            comet_step()
        elif step.startswith("metricx-"):
            metricx_step(step.split("-", 1)[1])
    log("qe done")
