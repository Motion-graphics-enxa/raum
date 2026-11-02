"""Direct POST /tts probe at BACKGROUND priority (never delays a live clause; retried when refused or cut).

    uv run python /tmp/raum-gaps/probe.py OUTPREFIX LANG RATE "text" ["text2" ...]
Each text is one request, played back to back like RAUM's voice lane (next request sent when the previous
stream ended). Prints per request: ttfb, wall, audio, worst playout underrun (chunk arrived after the audio
before it had played out, playout starting at the first chunk), leading/trailing silence, inner silences;
and the silences of the concatenated audio. Saves OUTPREFIX.wav (concatenation) and OUTPREFIX-<i>.wav.
"""
import sys
import time
import wave

import httpx
import numpy as np

sys.path.insert(0, "/tmp/raum-gaps")
from gaps import silences  # noqa: E402

URL = "http://127.0.0.1:8090/tts"
SR = 24000


def one(text, lang, rate, tries=12):
    for k in range(tries):
        t0 = time.monotonic()
        arr, bufs = [], []
        try:
            with httpx.stream("POST", URL, json={"text": text, "lang": lang, "rate": rate, "sample_rate": SR,
                                                 "priority": "background"}, timeout=15) as r:
                if r.status_code != 200:
                    time.sleep(0.4)
                    continue
                q = r.headers.get("x-queue-ms")
                for b in r.iter_raw():
                    arr.append((time.monotonic() - t0, len(b)))
                    bufs.append(b)
        except httpx.HTTPError:
            time.sleep(0.4)
            continue
        raw = b"".join(bufs)
        a = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2")
        return {"ttfb": arr[0][0], "wall": arr[-1][0], "arr": arr, "pcm": a, "queue_ms": q, "tries": k + 1}
    return None


def underrun(arr):
    """Worst lateness of a chunk vs the playout clock that starts at the first chunk."""
    t_first = arr[0][0]
    played = 0.0
    worst = 0.0
    total = 0.0
    for t, n in arr:
        due = t_first + played + total  # when the audio so far has played out (incl. stalls so far)
        if t > due:
            stall = t - due
            total += stall
            worst = max(worst, stall)
        played += n / 2 / SR
    return worst, total


def edges(a):
    r = silences(a.astype(np.float32) / 32768.0, SR, 60)
    if not r:
        return None
    return r["speech_start_ms"], len(a) / SR * 1000 - r["speech_end_ms"], r["silences"]


def save(path, a):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(a.astype("<i2").tobytes())


def main():
    out, lang, rate, texts = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4:]
    pcs = []
    for i, t in enumerate(texts):
        r = one(t, lang, rate)
        if r is None:
            print(f"  [{i}] refused every time")
            return
        w, tot = underrun(r["arr"])
        e = edges(r["pcm"])
        print(f"  [{i}] rate={rate} ttfb={r['ttfb'] * 1000:.0f}ms wall={r['wall'] * 1000:.0f}ms "
              f"audio={len(r['pcm']) / SR * 1000:.0f}ms tries={r['tries']} queue={r['queue_ms']} "
              f"underrun worst={w * 1000:.0f}ms total={tot * 1000:.0f}ms "
              f"lead={e[0]:.0f}ms trail={e[1]:.0f}ms inner={e[2]}  {t!r}")
        save(f"{out}-{i}.wav", r["pcm"])
        pcs.append(r["pcm"])
    if len(pcs) > 1:
        cat = np.concatenate(pcs)
        s = silences(cat.astype(np.float32) / 32768.0, SR, 80)
        print(f"  concat audio={len(cat) / SR * 1000:.0f}ms speech={s['speech_ms']}ms silences(>=80ms)="
              f"{[(p - s['speech_start_ms'], l) for p, l in s['silences']]}")
        save(f"{out}.wav", cat)


if __name__ == "__main__":
    main()
