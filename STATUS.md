# RAUM build status

_Read by the ops session. Updated at every milestone. Last update: Mon 2 Nov, ~16:45 (main d823427+)._

## What works (on `main`)
- **Live on the GX10 (ops):** real engines (whisper.cpp large-v3-turbo, gemma4:26b, gpt-oss:120b, bge-m3, Piper binary), fact guard, encrypted sessions + audit chain. Measured warm: vad 320 / asr 249 / first words 677 / sentence 888 / voice 1399 / check 1425 ms.
- **Mac, mock profile:** full loop with real mics (two Chrome phones with fake-mic WAVs via `scripts/e2e/`): partial captions, streamed translation, fact-guard dimensions, confirm prompts, glass box, summary, burn.
- **raum-core:** join (LiveKit token, metadata role/lang/device/path/audio_out), rooms, control, presence, **participant update** (`POST /api/rooms/{room}/participant {identity, lang?, audio_out?}`), end -> summary, approve, burn, compliance, events WS + backlog, privacy meter/seal, admin, agent link.
- **raum-agent:** Silero VAD, RoomPipeline (§6.4 events), clause-by-clause voice, cold-load-safe warm-ups (checker answers amber "warming up" until resident), room-based capacity, questions/`present` facts don't interrupt.
- **Echo control per device (C-14):** only mics that hear the voice pause (playback + 300 ms, `voice.state`); live lang/audio_out changes from participant metadata apply from the next utterance.
- **Voice:** resident Piper process per voice (restart on hang/death); TTS interface `synthesize(text, lang, voice_ref, rate)`; `http` engine for a voice-cloning service (:8090, fallback piper), voice sample in memory only (`pipeline/voice.py`, privacy test).
- **F-07 document camera:** `POST /api/rooms/{room}/document` -> `document.ready` (photo never stored); camera in the Sprechzimmer/Kanzlei menu, cards in the transcript (frame in the viewer's language, reading in its own).
- **N-02 deal glossary:** `POST|GET /api/rooms/{room}/glossary` (YAML/CSV); compact row in the Verhandlungsraum menu. **K-02** explain simply hardened (background, falls back to the plain translation).
- **Guard:** spaced thousands, count words, unit synonyms; translate prompt with target-language conventions + Sie/du mirroring; back-translation num_predict capped.
- **Fact guard + security:** guard ~0.2 ms, eval 100% caught / 0% false alarms (ar, ru, tr, 90 records); AES-256-GCM store, burn by key deletion, hash-chained audit, `raum audit verify|show`.
- **App (port 5180):** Join (one-tap language chips, the whole field taps, headphones/speaker), Talk (language + voice output in the menu, same-language warning, voice-playing cue), Summary, Table (to-scale timeline + 1.0 s budget), Admin, Listen, setup `/`.
- **Design:** impeccable only. PRODUCT.md, direction contract `.impeccable/surfaces/`, finish review (fix -> verdict -> final batch), **DESIGN.md** + `.impeccable/design.json`.
- **No third-party bytes:** LiveKit gets LAN-only STUN/TURN (box's own :3478; compose, livekit.yaml, dev.sh), onnxruntime telemetry off (ops 739e8a1); privacy meter reads 0 B on the GX10.
- **Deploy:** `deploy/push.sh` -> `gx10:~/Desktop/HackathonRaum/raum`; `deploy/box.sh` (sources `runtime/raum.env`; process groups); `RAUM_UV_EXTRAS="--extra denoise"` for DeepFilterNet.
- **Tests:** `cd core && uv run pytest -q` -> 651 passed.

## In flight (parallel agents, worktree branches; the integrator merges)
1. Simultaneous mode (segments committed while speaking; `seg`/`segs`; headphones listeners only) + pluggable EOU endpointing + utterance merging + complete clauses + speculative MT.
2. A-03 speakerphone participant (`raum speakerphone`), C-12 reconnect, F-05 admin completeness, F-03 install.sh dry-run proof + `--user-space`.

## Integrator TODO
- `GlossaryUpload` panel in Admin (after agent 2 above merges; it reworks Admin).
- App store: concatenate `seg` segments per language; merged-utterance re-final (after simultaneous mode merges).
- DeepFilterNet redesign landed (raum.pipeline.clean); ops to re-enable on spark with `denoise: {engine: deepfilternet}` + `RAUM_UV_EXTRAS="--extra denoise"` and check the "noise cleaning ready" log line.
- Consent copy for AI voice cloning ("AI voice" disclosure) when the cloning service lands.
- Known: two people speaking at once into the same language interleave on that language's track.
- Then P2: F-11 three people / three languages, F-08 Tauri, F-09 Android TWA, F-10 voice lock.

## How to run on a laptop
```bash
scripts/dev.sh            # livekit-server --dev (reused), core :8000, agent, app :5180
scripts/dev.sh --https    # phones on the LAN need https for the mic
cd core && uv run python ../scripts/replay.py --room sz-1234     # scripted conversation (UI test, no models)
cd scripts/e2e && npm i && npm run audio && node two-phones.mjs  # two Chrome phones, fake mics
```

## Blockers
- None. Real-engine paths are verified by ops on the box; new features land on `main` behind the mock profile first.
