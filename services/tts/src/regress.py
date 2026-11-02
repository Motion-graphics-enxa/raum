"""Regression for the 2 Nov hangs: the exact clauses RAUM sent (bot outputs /tmp/f-*.out, 17:45-17:50 local),
with the bots' own voices (recordings/*.wav -> RAUM VoiceRef, 16 kHz) and speaker paces 0.8-1.3,
through RAUM's unmodified HttpTTS. Prints first chunk / total / audio per clause and the cap per clause."""
import asyncio, os, sys, time, wave, json
import numpy as np, soxr
from raum.pipeline.tts_http import HttpTTS
from raum.pipeline.voice import VoiceRef

REC = os.path.expanduser("~/Desktop/HackathonRaum/raum/recordings")
PACE = {"ar": 1.4, "ru": 1.15, "tr": 1.15}


def voice(name, seconds):
    with wave.open(f"{REC}/{name}.wav") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        if w.getnchannels() == 2:
            pcm = pcm.reshape(-1, 2).mean(axis=1)
        sr = w.getframerate()
    v = VoiceRef(max_s=seconds)
    v.add(soxr.resample(pcm, sr, 16000))
    return v.wav()


# (target lang, clause, speaker recording, ref seconds, rate)
CASES = [
    ("en", "Take the card.", "de1", 3, 1.30),                       # simultaneous-mode partial, 14 chars @1.30
    ("en", "Take 500 mg Metformin twice a day,", "de1", 8, 0.96),
    ("en", "always after meals.", "de1", 8, 0.96),
    ("en", "I am allergic to penicillin.", "aux-de", 3, 0.80),
    ("en", "You must not take the Metformin starting from tomorrow.", "pause-de", 4, 0.85),
    ("en", "Take 500 mg Metformin", "de1", 3, 0.90),
    ("de", "Vielen Dank, Herr Doktor.", "en1", 3, 1.10),
    ("de", "Ich bin allergisch gegen Penicillin und nehme bereits jeden Abend 20 mg Simvastatin.", "en1", 8, 1.05),
    ("de", "Das sollten Sie nicht tun.", "en-pause", 3, 1.20),
    ("de", "Nehmen Sie dieses Medikament nicht zusammen mit Alkohol ein.", "en-pause", 6, 1.00),
    ("de", "Herr Doktor,", "ru1", 3, 1.30),
    ("de", "ich habe eine Allergie gegen Penicillin und nehme jeden Abend 20 mg Simvastatin ein.", "ru1", 8, 0.95),
    ("de", "Sie dürfen dieses Medikament nicht zusammen mit Alkohol einnehmen.", "ru-pause", 5, 0.90),
    ("ru", "Принимайте 500 мг метформина два раза в день,", "de1", 8, 1.00),
    ("ru", "всегда после еды.", "de1", 8, 1.25),
    ("ru", "У вас есть аллергия?", "en1", 6, 1.15),
    ("ru", "Вам нельзя принимать этот препарат вместе с алкоголем.", "pause-de", 6, 0.85),
    ("en", "Thank you, doctor.", "ru1", 4, 1.30),
    ("de", "Danke.", "en1", 8, 1.30),
    ("en", "Do you have any allergies?", "de1", 8, 0.80),
]


def cap_s(text, lang):
    return min(2.5 + 0.12 * len(text) * PACE.get(lang, 1.0), 20.0)


class FakePiper:
    sample_rate = 24000
    calls = 0

    async def synthesize(self, text, lang, **kw):
        FakePiper.calls += 1
        yield np.zeros(240, dtype=np.int16)


async def one(tts, lang, text, ref, rate, tag=""):
    t0 = time.perf_counter(); first = None; n = 0
    fb_before = FakePiper.calls
    async for c in tts.synthesize(text, lang, voice_ref=ref, rate=rate):
        if first is None:
            first = time.perf_counter() - t0
        n += len(c)
    tot = time.perf_counter() - t0
    fb = FakePiper.calls > fb_before
    return dict(tag=tag, lang=lang, chars=len(text), rate=rate, first_ms=round((first or 0) * 1000),
                total_ms=round(tot * 1000), audio_s=round(n / 24000, 2), fallback=fb)


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "seq"
    refs = {}
    tts = HttpTTS("http://127.0.0.1:8090", options={"sample_rate": 24000, "retry_s": 0.0}, fallback=FakePiper())
    await tts.warmup()
    if mode in ("seq", "all"):
        worst = 0; firsts = []; fails = 0
        for lang, text, rec, secs, rate in CASES:
            ref = refs.setdefault((rec, secs), voice(rec, secs))
            r = await one(tts, lang, text, ref, rate)
            ok = (not r["fallback"]) and r["audio_s"] <= cap_s(text, lang) + 0.3 and r["total_ms"] < 1000 * (2 + 1.5 * r["audio_s"]) + 300
            worst = max(worst, r["total_ms"]); firsts.append(r["first_ms"]); fails += not ok
            print(("ok  " if ok else "FAIL"), json.dumps(r, ensure_ascii=False), f"cap={cap_s(text, lang):.1f}s", text[:40], flush=True)
        print(f"SEQ: {len(CASES) - fails}/{len(CASES)} ok; first chunk ms median {int(np.median(firsts))}, "
              f"p90 {int(np.percentile(firsts, 90))}, max {max(firsts)}; worst total {worst} ms", flush=True)
    if mode in ("conc", "all"):
        print("-- 3 concurrent requests (two rooms + one more)")
        jobs = [one(tts, "de", "Nehmen Sie dieses Medikament nicht zusammen mit Alkohol ein.", voice("en-pause", 6), 1.0, "A"),
                one(tts, "en", "Take 500 mg Metformin twice a day, always after meals.", voice("de1", 8), 1.0, "B"),
                one(tts, "ru", "Вам нельзя принимать этот препарат вместе с алкоголем.", voice("pause-de", 6), 1.0, "C")]
        for r in await asyncio.gather(*jobs):
            print(json.dumps(r, ensure_ascii=False), flush=True)
    if mode in ("edge", "all"):
        await edge()
    await tts.aclose()


async def edge():
    import httpx
    U = "http://127.0.0.1:8090/tts"
    long_en = "Take 500 mg Metformin twice a day, always after meals, and call me if you feel dizzy or sick."
    async with httpx.AsyncClient(timeout=30) as c:
        print("-- caps (test hooks can only tighten them)")
        for tag, extra in (("audio cap 1.0 s", {"_cap_audio_s": 1.0}), ("wall cap 0.1 s (before audio)", {"_cap_wall_s": 0.1}),
                           ("wall cap 1.0 s (after first audio)", {"_cap_wall_s": 1.0})):
            t0 = time.perf_counter()
            async with c.stream("POST", U, json={"text": long_en, "lang": "en", "sample_rate": 24000, **extra}) as r:
                n = 0
                async for b in r.aiter_bytes():
                    n += len(b)
            print(f"{tag:36s} HTTP {r.status_code} audio {n / 2 / 24000:.2f}s in {1000 * (time.perf_counter() - t0):.0f} ms", flush=True)
        print("-- paused listener (the 2 Nov root cause): read 1 chunk, stop reading 8 s; another request meanwhile")
        async with c.stream("POST", U, json={"text": long_en, "lang": "en", "sample_rate": 24000}) as r1:
            it = r1.aiter_bytes(); await it.__anext__()
            await asyncio.sleep(1.5)
            t0 = time.perf_counter(); first = None; n = 0
            async with c.stream("POST", U, json={"text": "Haben Sie heute schon gegessen?", "lang": "de", "sample_rate": 24000}) as r2:
                async for b in r2.aiter_bytes():
                    first = first or time.perf_counter() - t0; n += len(b)
            print(f"second request while first listener is paused: HTTP {r2.status_code}, first audio {1000 * (first or 0):.0f} ms, "
                  f"{n / 48000:.2f}s audio", flush=True)
            await asyncio.sleep(6.5)
            rest = 0
            async for b in it:
                rest += len(b)
            print(f"paused listener resumed and got the remaining {rest / 48000:.2f}s", flush=True)
        print("-- client hangs up after the first chunk: the GPU must be free right after")
        async with c.stream("POST", U, json={"text": long_en, "lang": "en", "sample_rate": 24000}) as r1:
            it = r1.aiter_bytes(); await it.__anext__()
        t0 = time.perf_counter()
        async with c.stream("POST", U, json={"text": "Danke schön.", "lang": "de", "sample_rate": 24000}) as r2:
            first = None
            async for b in r2.aiter_bytes():
                first = first or time.perf_counter() - t0
        print(f"next request: HTTP {r2.status_code}, first audio {1000 * (first or 0):.0f} ms", flush=True)
        h = (await c.get("http://127.0.0.1:8090/health")).json()
        print("health:", {k: h[k] for k in ("busy", "busy_for_s", "waiting", "counters")}, flush=True)

asyncio.run(main())
