"""Voice under contention: TTS (raum-tts-svc2 :8091, priority background) while load loops hit whisper/Ollama.
usage: python bench.py COND [COND ...]   (each COND = one <=14 s burst, 12 s idle between)
Per clause: ttfa (request -> first audio), stall (playback underrun if playback starts at first audio),
max gap between arrivals, rtf = synth wall / audio s."""
import asyncio, json, random, sys, time
import httpx

TTS = "http://127.0.0.1:8091/tts"
OTHER_TTS = "http://127.0.0.1:8090/tts"   # prod: background only (refused/cut by live)
W1, W2 = "http://127.0.0.1:8081/inference", "http://127.0.0.1:8082/inference"
OLL = "http://127.0.0.1:11434/api/chat"
REC = "/home/deeptranslate/Desktop/HackathonRaum/raum/recordings/"
PART = open(REC + "long-de1-16k-4s.wav", "rb").read()
FINAL = open(REC + "long-de1-16k-8s.wav", "rb").read()
CLAUSES = [("de", "Bitte nehmen Sie morgens eine Tablette mit viel Wasser, und am Abend noch eine."),
           ("en", "Please take one tablet in the morning with plenty of water, and another one at night."),
           ("de", "Wir sehen uns nächste Woche am Dienstag um zehn Uhr in der Praxis wieder."),
           ("en", "We will see each other again next Tuesday at ten o'clock in the practice.")]
SYS = ("You are a professional medical interpreter. Translate the user's German text into English. "
       "Keep numbers, doses and names exactly. Output only the translation.\n" + "Context: " +
       " ".join(["Der Patient hat seit drei Tagen Fieber und Husten, nimmt Ibuprofen 400 mg."] * 12))
SENTS = ["Ich habe seit gestern starke Kopfschmerzen und mir ist schwindelig.",
         "Nehmen Sie die Tabletten bitte zweimal täglich nach dem Essen.",
         "Haben Sie Allergien gegen Medikamente, zum Beispiel Penicillin?",
         "Der Termin ist am Donnerstag um halb neun, bitte kommen Sie nüchtern."]
CHECK = ("Compare source and translation. Reply JSON {\"ok\": bool, \"issues\": [..]}.\nSource (de): {s}\n"
         "Translation (en): Please take the tablets twice a day after meals. {r}")

def whisper(url, wav, period):
    async def loop(c, stop, st):
        while not stop.is_set():
            t = time.monotonic()
            try:
                r = await c.post(url, data={"response_format": "verbose_json", "temperature": "0.0", "language": "de",
                                            "no_language_probabilities": "true"},
                                 files={"file": ("a.wav", wav, "audio/wav")})
                st.append((time.monotonic() - t) * 1000)
            except Exception as e:
                st.append(-1)
            await asyncio.sleep(max(0.0, period - (time.monotonic() - t)))
    return loop

def gemma(npred=96):
    async def loop(c, stop, st):
        while not stop.is_set():
            t = time.monotonic()
            body = {"model": "gemma4:e4b", "stream": False, "keep_alive": -1, "think": False,
                    "options": {"temperature": 0.1, "num_predict": npred, "num_ctx": 8192},
                    "messages": [{"role": "system", "content": SYS},
                                 {"role": "user", "content": random.choice(SENTS) + f" ({random.randint(1, 10**6)})"}]}
            try:
                await c.post(OLL, json=body); st.append((time.monotonic() - t) * 1000)
            except Exception:
                st.append(-1)
    return loop

def gptoss():
    async def loop(c, stop, st):
        while not stop.is_set():
            t = time.monotonic()
            body = {"model": "gpt-oss:20b", "stream": False, "keep_alive": -1, "think": "low",
                    "options": {"temperature": 0.0, "num_predict": 256},
                    "messages": [{"role": "user", "content": CHECK.replace("{s}", random.choice(SENTS)).replace("{r}", str(random.randint(1, 10**6)))}]}
            try:
                await c.post(OLL, json=body); st.append((time.monotonic() - t) * 1000)
            except Exception:
                st.append(-1)
    return loop

def other_voice():
    async def loop(c, stop, st):
        while not stop.is_set():
            t = time.monotonic()
            try:
                async with c.stream("POST", OTHER_TTS, json={"text": CLAUSES[1][1], "lang": "en", "sample_rate": 24000,
                                                             "priority": "background"}) as r:
                    async for _ in r.aiter_bytes():
                        pass
                    st.append(r.status_code)
            except Exception:
                st.append(-1)
            if st and st[-1] != 200:
                await asyncio.sleep(0.3)
    return loop

P = lambda per: whisper(W1, PART, per)
CONDS = {
    "alone": [],
    "whisper500": [P(0.5)],
    "whisper2x500": [P(0.5), P(0.5)],                    # 2 rooms' partials
    "gemma1": [gemma()],
    "gemma2": [gemma(), gemma()],                         # live + speculative MT
    "gptoss": [gptoss()],
    "othervoice": [other_voice()],
    "all": [P(0.5), P(0.5), whisper(W2, FINAL, 1.0), gemma(), gemma(), gptoss(), other_voice()],
    # mitigations, each = "all" with one load class removed / thinned while the voice is synthesised
    "all-partials1000": [P(1.0), P(1.0), whisper(W2, FINAL, 1.0), gemma(), gemma(), gptoss(), other_voice()],
    "all-nopartials": [whisper(W2, FINAL, 1.0), gemma(), gemma(), gptoss(), other_voice()],
    "all-nocheck": [P(0.5), P(0.5), gemma(), gemma(), other_voice()],          # gpt-oss + echo ASR deferred
    "all-1mt": [P(0.5), P(0.5), whisper(W2, FINAL, 1.0), gemma(), gptoss(), other_voice()],  # spec MT held
    "all-noother": [P(0.5), P(0.5), whisper(W2, FINAL, 1.0), gemma(), gemma(), gptoss()],     # one voice at a time
    "policy": [gemma()],                                  # all room policies: only the live MT keeps running
    "policy+other": [gemma(), other_voice()],
}

async def tts_one(c, lang, text):
    for _ in range(15):   # 8091 refuses background while it serves someone else's request: wait for it
        r = await tts_try(c, lang, text)
        if r.get("refused") != 409:
            return r
        await asyncio.sleep(0.2)
    return r

async def tts_try(c, lang, text):
    t0 = time.monotonic()
    arr = []
    async with c.stream("POST", TTS, json={"text": text, "lang": lang, "sample_rate": 24000, "priority": "background"}) as r:
        if r.status_code >= 400:
            return {"refused": r.status_code}
        q = r.headers.get("x-queue-ms")
        async for b in r.aiter_bytes():
            arr.append((time.monotonic() - t0, len(b) / 2 / 24000))
    if not arr:
        return {"empty": 1}
    first = arr[0][0]; end = first; stall = mx = 0.0; prev = first; gap = 0.0
    for t, d in arr:
        if t > end:
            s = t - end; stall += s; mx = max(mx, s); end = t
        end += d; gap = max(gap, t - prev); prev = t
    audio = sum(d for _, d in arr)
    return {"ttfa": first * 1000, "stall": stall * 1000, "maxstall": mx * 1000, "gap": gap * 1000,
            "rtf": arr[-1][0] / audio, "audio": audio, "q": q}

async def cond(name):
    stop = asyncio.Event(); stats = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=2.0)) as c:
        tasks = []
        for i, L in enumerate(CONDS[name]):
            st = []; stats.append((L.__qualname__.split(".")[0], st)); tasks.append(asyncio.create_task(L(c, stop, st)))
        if tasks:
            await asyncio.sleep(1.5)
        rows = []
        for lang, text in CLAUSES:
            rows.append(await tts_one(c, lang, text))
        stop.set()
        for t in tasks:
            t.cancel()   # Ollama aborts a generation when its client goes
        await asyncio.gather(*tasks, return_exceptions=True)
    ok = [r for r in rows if "ttfa" in r]
    def agg(k, f=max):
        return f(r[k] for r in ok) if ok else float("nan")
    med = lambda k: sorted(r[k] for r in ok)[len(ok) // 2] if ok else float("nan")
    load = " ".join(f"{n}:{len(s)}x{(sum(x for x in s if x > 0) / max(1, len([x for x in s if x > 0]))):.0f}" for n, s in stats)
    print(f"{name:18s} ttfa med {med('ttfa'):5.0f} max {agg('ttfa'):5.0f} | stall tot max {agg('stall'):5.0f} "
          f"maxstall {agg('maxstall'):5.0f} | gap max {agg('gap'):4.0f} | rtf med {med('rtf'):.2f} max {agg('rtf'):.2f} "
          f"| n={len(ok)}/{len(rows)} refused={[r for r in rows if 'ttfa' not in r]} | load {load}", flush=True)
    json.dump({"cond": name, "rows": rows}, open(f"/home/deeptranslate/Desktop/HackathonRaum/runtime/gpu-sched/{name}.json", "w"))

async def main():
    for i, n in enumerate(sys.argv[1:]):
        if open("/home/deeptranslate/Desktop/HackathonRaum/runtime/GPU_WINDOW").read().strip() not in ("none", "gpu-sched", ""):
            print("GPU window held by someone else: stop"); return
        if i:
            await asyncio.sleep(18)
        await cond(n)

asyncio.run(main())
