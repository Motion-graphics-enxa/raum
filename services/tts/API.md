# raum-tts: local streaming TTS with in-session voice cloning

The service runs on the GX10, box-local only: `http://127.0.0.1:8090`. It is not reachable from the LAN or the tailnet.

- **Model:** Chatterbox Multilingual V3 (Resemble AI, MIT licence). It uses a 0.5B Llama T3 in bf16 with a CUDA-graph decode, plus the distilled 2-step "meanflow" S3Gen decoder from Chatterbox-Turbo (also MIT).
- **Languages:** 23 — ar, da, de, el, en, es, fi, fr, he, hi, it, ja, ko, ms, nl, no, pl, pt, ru, sv, sw, tr, zh. RAUM uses de, en, ru, tr, ar.
- **Output:** raw PCM, s16le, mono, **24000 Hz**, sent as a chunked HTTP body.
- **Privacy:** voices are held in process memory only. Nothing is written to disk, and logs record lengths and timings but no text.
- **Process:** container `raum-tts-svc` (NGC pytorch 26.07 image, host network, bound to 127.0.0.1 only). Code is in `runtime/raum-tts/src/` (`server.py`, `cb_engine.py`, `textnorm.py`), and weights are in `runtime/raum-tts/hf-cache/`. The service runs offline.
  - Start: `~/Desktop/HackathonRaum/runtime/raum-tts/start.sh`. It refuses to start unless 14 GB is available, and it also starts the memory guard.
  - Stop: `~/Desktop/HackathonRaum/runtime/raum-tts/stop.sh`. This stops the guard too and frees the memory.
  - Status and logs: `~/Desktop/HackathonRaum/runtime/raum-tts/status.sh [lines]`, or `docker logs -f raum-tts-svc`.
- **Memory:** about 6 GB of unified memory: roughly 2.6–3.4 GB of GPU allocations plus about 2.5 GB of host-side CUDA, kernel and runtime memory. `guard.sh` stops the container if host MemAvailable stays below 8 GB for 2 s, so the OOM killer never hits Ollama or another team. Its log is `runtime/raum-tts/guard.log`.
  - **The RAUM client must fall back to Piper** when the service is down: connection refused, or no `/health` answer.
- **Concurrency (v1.2, after the 2 Nov hangs):**
  - One synthesis runs on the GPU at a time.
  - Each job synthesizes into an in-memory buffer and frees the GPU as soon as it finishes, however slowly the client plays or reads. Before v1.2, a paused or vanished listener held the GPU, and every later request queued for minutes.
  - **Admission:** if the predicted wait for the GPU is over about 1 s, or someone is already waiting, the answer is **`503` with `Retry-After: 1`**, sent at once (after at most 0.25–1 s) and before any audio.
  - Headers are sent together with the first audio chunk. Every failure before audio is therefore an HTTP error, never an empty `200`, and RAUM speaks that clause with Piper.
  - A client that hangs up cancels its job within one token step (about 20 ms).
- **Caps per request:**
  - **Audio:** at most `min(2.5 s + 0.12 s × chars × script factor, 20 s)` for `/tts`. The script factor is ar 1.4 and ru/tr 1.15; `/v1/speak` is capped at 60 s.
  - **Wall clock:** at most 2 s + 1.5 × the audio produced so far, and at most 2 s + 1.5 × the audio cap.
  - **No audio within 3 s:** `503`.
  - On a cap the stream ends cleanly and the log says `cap hit`.
- **`/health`** is async and answers even while the GPU is busy. It reports `busy`, `busy_for_s`, `busy_limit_s` (the current job's wall cap), `waiting` and `counters` (done, cap_hit, client_gone, rejected_*).
- **Watchdog** (`runtime/raum-tts/watchdog.sh`, every 15 s) restarts the service in two cases:
  - `/health` fails 3 times in a row;
  - `busy_for_s` is over 30 s and over `busy_limit_s` + 5, **and** a probe `POST /tts` ("Guten Tag.") brings no audio within 10 s.
- **RAUM client note:** `HttpTTS` puts the service aside for `retry_s` after any failure, including a 503. With admission control a 503 is transient, so `retry_s: 2` is better than the default 15.
- **Latency:**
  - First audio for a 12-word sentence is about 180 ms when the GPU is quiet, and 280–530 ms while another job keeps the GPU about 95% busy.
  - Synthesis runs at 0.4–0.65× real time.
  - No mid-sentence stalls were measured.
- **No watermark:** the Perth watermark that stock Chatterbox applies is not applied on this streaming path.

## Endpoints

### `GET /health`
Returns, for example:
```json
{"status":"ok","model":"chatterbox-multilingual-v3 ...","sample_rate":24000,"format":"s16le mono",
 "languages":["ar","da","de",...],"primary_languages":["de","en","ru","tr","ar"],
 "voices":2,"max_voices":64,"busy":false,"uptime_s":812,
 "gpu_allocated_mb":2450,"gpu_reserved_mb":2700,"host_mem_available_mb":12100}
```
The model is loaded and warmed (de and en) before the port opens, so a `200` here means it is ready.

### `POST /v1/voices` (create a cloned voice)
Send the speaker's reference audio. 5–10 s of clean speech is best. The minimum is 2 s, and anything over 30 s is cut. Three body formats are accepted:

- **WAV/FLAC/OGG file as the raw body**, with any sample rate; stereo is mixed down.
  `curl -s --data-binary @ref.wav -H 'Content-Type: audio/wav' 'http://127.0.0.1:8090/v1/voices?lang=de'`
- **Raw PCM s16le mono**, with its rate in the query (this suits LiveKit frames, e.g. 48 kHz).
  `curl -s --data-binary @ref.pcm -H 'Content-Type: application/octet-stream' 'http://127.0.0.1:8090/v1/voices?sample_rate=48000&lang=de'`
- **multipart/form-data** with a `file` field, plus optional `lang` and `sample_rate` fields.

Query parameters (all optional):
- `lang`: the language the reference speaker speaks. It switches on the cross-lingual accent fix described below.
- `sample_rate`: required for raw PCM.
- `exaggeration`: emotion intensity, 0–2, default 0.5.

Response `200`:
```json
{"voice_id":"v_3f2a9c0d1e7b4a55","ref_seconds":8.4,"embed_ms":90,"lang":"de"}
```
Errors:
- `400`: the audio is undecodable, or raw PCM was sent without `sample_rate`.
- `422`: the reference is shorter than 2 s.

At most 64 voices are kept (`RAUM_TTS_MAX_VOICES`). Past that, the least recently used voice is dropped and `speak` returns 404 for it. Voices do not survive a service restart.

### `DELETE /v1/voices/{voice_id}`
Returns `204`, or `404` if the id is unknown. Call it when the session ends ("Forgets").

### `POST /v1/speak` (stream speech)
Request body (JSON):
```json
{"text":"Nehmen Sie zweimal täglich 500 Milligramm Metformin.","lang":"de",
 "voice_id":"v_3f2a9c0d1e7b4a55","speed":1.0}
```
| field | type | default | notes |
|---|---|---|---|
| `text` | string, 1–4000 chars | — | The text is split into sentences and streamed back to back, with 120 ms of silence between them. |
| `lang` | string | — | ISO 639-1 (`de`, `en`, `ru`, `tr`, `ar`, …). `de-DE` is accepted. |
| `voice_id` | string or null | null | If null, the default voice is used: the built-in Chatterbox voice (female, MIT), unless an operator file `runtime/raum-tts/voices/<lang>.wav` exists. |
| `speed` | 0.5–2.0 | 1.0 | Pitch-preserving time stretch. |
| `exaggeration` | 0–2 | 0.5 | Expressiveness. |
| `cfg_weight` | 0–1 or null | 0.5 | Text guidance. Resemble suggests 0 for cross-lingual voices to reduce accent carry-over, but on short clauses 0 and 0.3 made the model babble or repeat words, so 0.5 is the default everywhere. Lower it per request if you prefer less accent. The server-wide value for cross-lingual voices is `RAUM_TTS_XLING_CFG`. |
| `temperature` | 0.05–2 | 0.8 | |
| `normalize` | bool | true | Numbers are spelled out in the target language ("500" → "fünfhundert", "пятьсот", "beş yüz", "خمسمائة"). The model misreads digits in ru and tr without this. |

Response `200`, a chunked body of raw PCM s16le mono, with these headers:
```
Content-Type: audio/L16;rate=24000;channels=1
X-Sample-Rate: 24000
X-Audio-Format: s16le
X-Channels: 1
X-Cfg-Weight: 0.5
X-Sentences: 1
```
The first chunk arrives after 0.18–0.5 s, depending on GPU load. Later chunks arrive faster than real time, so a player that starts on the first chunk does not run dry. Errors:
- `404`: unknown `voice_id`.
- `422`: unsupported `lang` or empty text.

To cancel, close the connection. Generation stops at the next chunk.

## RAUM compatibility API (what `core/raum/pipeline/tts_http.py` speaks)

### `POST /tts`
Request: JSON `{text, lang, sample_rate, rate?, model?, voice_ref?, voice_id?, voice_lang?, cfg_weight?}`, or the same fields as multipart with `voice_ref` as a file part (`options.voice_ref_as: multipart`).
- `voice_ref`: base64 WAV of the speaker's first seconds, as RAUM's `VoiceRef` builds it (16 kHz mono, 3–8 s, growing).
  - It is registered in memory under `voice_id`, or under `sha256(ref)[:16]` if no `voice_id` is sent. This is the same id the client computes.
  - Each new ref (the client sends a longer one as it grows) costs about 70 ms of embedding, once.
  - An unreadable or too-short ref (< 2 s) never fails the request: the default voice speaks instead.
- `voice_lang` (optional): the speaker's own language. If it is missing, the voice counts as cross-lingual. Today both cases use cfg 0.5 (see `cfg_weight` above).
- `rate`: the speaker's pace (RAUM sends 0.7–1.5), applied as a pitch-preserving `speed`, clamped to 0.8–1.3.
- `model` and `sample_rate` are accepted and ignored. Output is always 24 kHz (in `X-Sample-Rate`), and the client resamples.
- If no `voice_ref` or `voice_id` is known, the default voice speaks. Numbers are spelled out as for `/v1/speak`.
- Each sentence gets a token cap (about 1 s + 0.1 s per character, longer for ar/ru/tr), so a runaway tail cannot keep talking.

Response: `200` with s16le mono chunks, flushed as each one is vocoded. Headers and the first audio arrive together, after about 0.18–0.27 s.

Errors:
- `503`: busy, slow, or a cap was hit before any audio. RAUM falls back to Piper.
- `422`: unsupported `lang` or empty text.

### `DELETE /tts/voices/{voice_id}`
An alias of `DELETE /v1/voices/{id}`: `204`, or `404` if unknown. Call it at session end with the same id (`sha256(voice_ref)[:16]`) for every ref sent during the session.

**Short clauses with a cross-lingual voice are the weak spot.** On 24 two-to-four-word clauses (de/en):
| voice | clean clauses |
|---|---|
| built-in default voice | 24/24 |
| same-language reference | 24/24 |
| cross-lingual reference | 19–20/24 (a repeated "Danke. Danke.", a dropped first word, one wrong word) |

Clauses of 5+ words were clean. If the RAUM pipeline can merge clauses of under about 4 words with the next one before TTS, do that.

## Python client sketch (for the RAUM TTS engine)
```python
import httpx, numpy as np

TTS = "http://127.0.0.1:8090"

async def clone(pcm16: bytes, sr: int, lang: str) -> str:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{TTS}/v1/voices", params={"sample_rate": sr, "lang": lang}, content=pcm16,
                         headers={"Content-Type": "application/octet-stream"})
        r.raise_for_status(); return r.json()["voice_id"]

async def speak(text: str, lang: str, voice_id: str | None):
    """Yields int16 numpy frames at 24 kHz as they arrive."""
    async with httpx.AsyncClient(timeout=None) as c:
        async with c.stream("POST", f"{TTS}/v1/speak", json={"text": text, "lang": lang, "voice_id": voice_id}) as r:
            r.raise_for_status()
            sr = int(r.headers["X-Sample-Rate"])           # 24000
            buf = b""
            async for chunk in r.aiter_bytes():
                buf += chunk
                n = len(buf) // 2 * 2
                if n:
                    yield np.frombuffer(buf[:n], dtype="<i2"); buf = buf[n:]

async def forget(voice_id: str):
    async with httpx.AsyncClient() as c:
        await c.delete(f"{TTS}/v1/voices/{voice_id}")
```
For LiveKit, resample 24 kHz to 48 kHz, or create the `rtc.AudioSource` at 24000 Hz, and push frames as they arrive.

Suggested RAUM flow:
1. When a participant has spoken about 6–10 s (VAD-gated, ideally the first clean utterance), post that PCM to `/v1/voices?lang=<their lang>` and keep the `voice_id` per participant.
2. Until the voice exists, use `voice_id=null`.
3. On session end, `DELETE` the voice.
