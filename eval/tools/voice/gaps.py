"""Silences inside speech in a WAV (or raw s16le with --raw RATE).

    python gaps.py file.wav [--min-ms 80] [--db -40] [--json rec.json]
Prints speech span, every silence >= min-ms between the first and last voiced 10 ms window (position ms, length
ms), and with --json the frame-arrival gaps (> 30 ms between frames) of a rec.py capture.
"""
import argparse
import json
import wave

import numpy as np


def load(path, raw_rate=None):
    if raw_rate:
        return np.fromfile(path, dtype="<i2").astype(np.float32) / 32768.0, raw_rate
    with wave.open(path, "rb") as w:
        rate, n = w.getframerate(), w.getnframes()
        a = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    return a, rate


def silences(a, rate, min_ms=80, db=-40.0, win_ms=10):
    hop = rate * win_ms // 1000
    n = len(a) // hop
    if n == 0:
        return None
    rms = np.sqrt(np.mean(a[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    lv = 20 * np.log10(rms)
    peak = np.percentile(lv, 95)
    voiced = lv > max(peak + db, -60)
    idx = np.flatnonzero(voiced)
    if not len(idx):
        return None
    s, e = idx[0], idx[-1]
    out = []
    i = s
    while i <= e:
        if not voiced[i]:
            j = i
            while j <= e and not voiced[j]:
                j += 1
            if (j - i) * win_ms >= min_ms:
                out.append((i * win_ms, (j - i) * win_ms))
            i = j
        else:
            i += 1
    return {"speech_start_ms": s * win_ms, "speech_end_ms": (e + 1) * win_ms,
            "speech_ms": (e + 1 - s) * win_ms, "silences": out,
            "silence_total_ms": sum(x[1] for x in out)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--raw", type=int)
    ap.add_argument("--min-ms", type=int, default=80)
    ap.add_argument("--db", type=float, default=-40.0)
    ap.add_argument("--json")
    a = ap.parse_args()
    x, rate = load(a.path, a.raw)
    r = silences(x, rate, a.min_ms, a.db)
    print(f"{a.path}: {len(x) / rate:.2f} s")
    if r:
        print(f"  speech {r['speech_start_ms']}..{r['speech_end_ms']} ms ({r['speech_ms']} ms), "
              f"{len(r['silences'])} silences >= {a.min_ms} ms, total {r['silence_total_ms']} ms")
        for pos, ln in r["silences"]:
            print(f"    at {pos - r['speech_start_ms']:6d} ms (abs {pos:6d}) : {ln:4d} ms")
    if a.json:
        d = json.load(open(a.json))
        arr = d["arrivals"]
        gaps = [(arr[i][0], (arr[i][0] - arr[i - 1][0]) * 1000) for i in range(1, len(arr))
                if arr[i][0] - arr[i - 1][0] > 0.03]
        print(f"  frames {len(arr)}, arrival gaps > 30 ms: {len(gaps)}", [(round(t, 2), round(g)) for t, g in gaps[:20]])


if __name__ == "__main__":
    main()
