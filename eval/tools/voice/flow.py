"""Voice flow probe: request granularity x edge trim x prebuffer, on the real voice service, played out like the agent.

    cd ~/Desktop/HackathonRaum/raum/core && uv run python /tmp/raum-flow/flow.py OUTDIR LANG "translation" [...]
For each strategy the translation is streamed word by word like Gemma (MT_FIRST_MS, then MS_PER_WORD per word), cut
into TTS requests the way the pipeline would, and each request is POSTed (background priority, TTS_URL) at
max(text available, previous request done): the voice lane's order (speculate.prepare ``after=cur``). Received
chunks are played out like the agent's AudioSource (a chunk plays at max(arrival, end of the audio before it)) and
rendered to a timeline WAV. Variants are applied to the same received audio: edge trim (as 03-tts-trim-edges:
-42 dBFS, keep 30 ms lead / 80 ms tail; tail held back only while silent, so no added latency) and a prebuffer of
P ms before a request's first frame when the queue is empty.
Prints per strategy/variant: first audio (ms after the speaker's stop, MT model included), speech span, gaps
(silences >= 200 ms inside speech), all silences >= 80 ms, stalls (queue ran dry mid-request).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import wave

import httpx
import numpy as np

sys.path.insert(0, "/tmp/raum-gaps")
from gaps import silences  # noqa: E402

import raum.pipeline.room as R  # noqa: E402

SR = 24000
URL = os.environ.get("TTS_URL", "http://127.0.0.1:8091/tts")
MT_FIRST_MS = float(os.environ.get("MT_FIRST_MS", "120"))
MS_PER_WORD = float(os.environ.get("MS_PER_WORD", "30"))  # e4b: a 10-word sentence in ~300 ms
RATE = float(os.environ.get("RATE", "1.0"))
THR = 10 ** (-42 / 20)
KEEP_LEAD, KEEP_TAIL = int(0.03 * SR), int(0.08 * SR)
_SENT = re.compile(r"[.!?…。！？؟](?=\s)")


def take_sentence(buf: str) -> tuple[str, str]:
    cut = -1
    for m in _SENT.finditer(buf):
        if m.group(0) == "." and R._ABBREV.search(buf[:m.end()]):
            continue
        cut = m.end()
    return (buf[:cut].strip(), buf[cut:]) if cut > 0 else ("", buf)


def take_boundary_phrase(buf: str) -> tuple[str, str]:
    old = R.PHRASE_WORDS
    R.PHRASE_WORDS = 12  # 02-first-phrase-no-word-cut
    try:
        return R.take_phrase(buf)
    finally:
        R.PHRASE_WORDS = old


def segments(text: str, strategy: str) -> list[tuple[float, str]]:
    words = text.split()
    out: list[tuple[float, str]] = []
    pending, spoke = "", False
    for i, w in enumerate(words):
        pending += (" " if i else "") + w
        t = MT_FIRST_MS + i * MS_PER_WORD
        if strategy == "clause5":  # cf92159
            R.PHRASE_WORDS = 5
            c, pending = R.take_clause(pending) if spoke else R.take_phrase(pending)
        elif strategy == "clause12":  # cf92159 + 02 diff
            c, pending = R.take_clause(pending) if spoke else take_boundary_phrase(pending)
        elif strategy == "hybrid":  # first phrase at the first boundary, then whole sentences
            c, pending = take_sentence(pending) if spoke else take_boundary_phrase(pending)
        elif strategy == "sentence":
            c, pending = take_sentence(pending)
        else:
            raise SystemExit(strategy)
        if c:
            spoke = True
            out.append((t, c))
    if pending.strip():
        out.append((MT_FIRST_MS + len(words) * MS_PER_WORD, pending.strip()))
    return out


def post(text: str, lang: str, t0: float) -> dict:
    """Refusals (409: the GPU is busy with someone's live speech) are retried; the time lost to them is taken out
    of the clock (``lost``), so the playout is what an unrefused request would have given."""
    first = time.monotonic()
    for k in range(240):
        sent = time.monotonic()
        arr = []
        carry = b""
        try:
            with httpx.stream("POST", URL, json={"text": text, "lang": lang, "rate": RATE, "sample_rate": SR,
                                                 "priority": "background"}, timeout=20) as r:
                if r.status_code != 200:
                    time.sleep(0.25)
                    continue
                lost = sent - first
                for b in r.iter_raw():
                    b = carry + b
                    n = len(b) // 2 * 2
                    carry = b[n:]
                    if n:
                        arr.append((time.monotonic() - t0 - lost, np.frombuffer(b[:n], "<i2").copy()))
        except httpx.HTTPError:
            time.sleep(0.3)
            continue
        if arr:
            return {"text": text, "sent": sent - t0 - lost, "chunks": arr, "tries": k + 1, "lost": lost}
    raise RuntimeError("refused")


def run(lang: str, segs: list[tuple[float, str]]) -> list[dict]:
    t0 = time.monotonic()
    reqs, done = [], 0.0
    for avail, text in segs:
        at = max(avail / 1000.0, done)
        d = at - (time.monotonic() - t0)
        if d > 0:
            time.sleep(d)
        r = post(text, lang, t0)
        t0 += r["lost"]  # the virtual clock: as if the request had gone through at once
        r["avail"] = avail / 1000.0
        done = r["chunks"][-1][0]
        reqs.append(r)
    return reqs


def trim(chunks: list[tuple[float, np.ndarray]]) -> list[tuple[float, np.ndarray]]:
    """Client-side edge trim: drop the lead before the first loud sample (keep 30 ms) and the tail after the last
    (keep 80 ms). Silent samples are held only until louder audio follows: speech is never delayed."""
    out, held, lead = [], [], True
    for t, c in chunks:
        if lead:
            loud = np.flatnonzero(np.abs(c) > THR * 32768)
            if not loud.size:
                continue
            c = c[max(0, int(loud[0]) - KEEP_LEAD):]
            lead = False
        loud = np.flatnonzero(np.abs(c) > THR * 32768)
        if loud.size:
            for _, h in held:
                out.append((t, h))
            held = []
            head, tail = c[:int(loud[-1]) + 1], c[int(loud[-1]) + 1:]
            out.append((t, head))
            if tail.size:
                held.append((t, tail))
        else:
            held.append((t, c))
    rest = np.concatenate([h for _, h in held]) if held else np.zeros(0, np.int16)
    if rest.size:
        out.append((chunks[-1][0], rest[:KEEP_TAIL]))
    return out


def playout(reqs: list[dict], *, do_trim: bool, prebuf_ms: float) -> tuple[np.ndarray, float, list]:
    """Play the requests' chunks like the AudioSource: back to back, a chunk at max(arrival, end of the one
    before). With a prebuffer, a request that starts on an empty queue waits until prebuf_ms of it arrived."""
    placed, stalls = [], []
    head = None  # when the queue runs dry (absolute s)
    for r in reqs:
        ch = trim(r["chunks"]) if do_trim else r["chunks"]
        ch = [(t, c) for t, c in ch if c.size]
        if not ch:
            continue
        release = ch[0][0]
        if prebuf_ms and (head is None or head <= ch[0][0]):
            got, release = 0.0, ch[-1][0]
            for t, c in ch:
                got += c.size / SR * 1000
                if got >= prebuf_ms:
                    release = t
                    break
        for i, (t, c) in enumerate(ch):
            start = max(t, release, head if head is not None else 0.0)
            if i and head is not None and t > head + 1e-3:
                stalls.append((round(head * 1000), round((t - head) * 1000)))
            placed.append((start, c))
            head = start + c.size / SR
    t_end = head or 0.0
    out = np.zeros(int(t_end * SR) + SR // 10, np.int16)
    for s, c in placed:
        i = int(s * SR)
        out[i:i + c.size] = c[: len(out) - i]
    return out, t_end, stalls


def save(path, a):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(a.astype("<i2").tobytes())


def main():
    outdir, lang, texts = sys.argv[1], sys.argv[2], sys.argv[3:]
    strategies = os.environ.get("STRATS", "clause5,clause12,hybrid,sentence").split(",")
    os.makedirs(outdir, exist_ok=True)
    res = []
    for ti, text in enumerate(texts):
        for s in strategies:
            segs = segments(text, s)
            reqs = run(lang, segs)
            for do_trim in (False, True):
                for pb in (0.0, 120.0):
                    a, t_end, stalls = playout(reqs, do_trim=do_trim, prebuf_ms=pb)
                    g = silences(a.astype(np.float32) / 32768.0, SR, 80)
                    gaps = [(p - g["speech_start_ms"], ln) for p, ln in g["silences"]]
                    row = {"text": ti, "lang": lang, "strategy": s, "trim": do_trim, "prebuf": pb,
                           "requests": [x for _, x in segs], "avail_ms": [round(t) for t, _ in segs],
                           "sent_ms": [round(r["sent"] * 1000) for r in reqs],
                           "ttfb_ms": [round((r["chunks"][0][0] - r["sent"]) * 1000) for r in reqs],
                           "tries": [r["tries"] for r in reqs],
                           "first_audio_ms": g["speech_start_ms"], "speech_ms": g["speech_ms"],
                           "gaps200": [x for x in gaps if x[1] >= 200], "sil80": gaps,
                           "stalls": stalls}
                    res.append(row)
                    print(json.dumps(row, ensure_ascii=False, default=int), flush=True)
                    save(f"{outdir}/{lang}{ti}-{s}-{'trim' if do_trim else 'raw'}-pb{int(pb)}.wav", a)
            time.sleep(1.0)  # short bursts: never a sustained GPU load
    json.dump(res, open(f"{outdir}/{lang}.json", "w"), ensure_ascii=False, default=int)


if __name__ == "__main__":
    main()
