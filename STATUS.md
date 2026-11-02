# RAUM build status

_Read by the ops session. Updated at every milestone. Last update: Mon 2 Nov, ~18:05 (main c652e2c)._

## What works (on `main`)
- **Live on the GX10 (ops):** real engines (whisper.cpp large-v3-turbo, gemma4:26b, gpt-oss:120b, bge-m3, Piper binary), fact guard, encrypted sessions + audit chain. Measured warm: vad 320 / asr 249 / first words 677 / sentence 888 / voice 1399 / check 1425 ms.
- **Mac, mock profile:** full loop with real mics (two Chrome phones with fake-mic WAVs via `scripts/e2e/`): partial captions, streamed translation, fact-guard dimensions, confirm prompts, glass box, summary, burn.
- **raum-core:** join (LiveKit token, metadata role/lang/device/path/audio_out), rooms, control, presence, **participant update** (`POST /api/rooms/{room}/participant {identity, lang?, audio_out?}`), end -> summary, approve, burn, compliance, events WS + backlog, privacy meter/seal, admin, agent link.
- **raum-agent:** Silero VAD, RoomPipeline (§6.4 events), clause-by-clause voice, cold-load-safe warm-ups (checker answers amber "warming up" until resident), room-based capacity, questions/`present` facts don't interrupt.
- **Echo control per device (C-14):** only mics that hear the voice pause (playback + 300 ms, `voice.state`); live lang/audio_out changes from participant metadata apply from the next utterance.
- **Voice lanes (P0 fix, e0f806b):** one writer per `tts-<lang>` source; utterances into one language speak in turn; a failed clause is dropped (3 in a row: that language pauses 20 s), never off for the session; a failed source is replaced.
- **Semantic end of turn (ops, 47ed350):** VAD 0.25 s pause -> early final ASR + smart-turn + LiveKit text turn model -> end now / 1.0 s / 1.6 s; speech in the wait continues the utterance. Extra `turn`; off in mock.
- **Simultaneous mode + latency (d1bd3b7):** on ops' end of turn; speculation at the pause (never published before the turn ends), voice on the first phrase, stable partial as final; `tts_synth_first_ms` / `tts_publish_first_ms` / `asr_from` in pipeline.timing. Box: speaker stops -> voice 747-1246 ms (sz), 850-956 ms (gs).
- **Gespraech room (gs):** `checker: off` (fact guard only, no badge unless red, no prompts), numbers/dates/times, simultaneous from the first word; rooms list in `order`. **3+ language rooms:** `pipeline.check_targets: one` (one gpt-oss check per utterance).
- **Guard English (guard-en):** 10.30 am, EUR 1.2m, 950k, at nine, half past two / halb drei, the third of November, number words, at 2 / 14 Uhr, F -> C (prompt converts, original in brackets). Eval 100 % caught, 0 false alarms.
- **Cloned voice (ops' Chatterbox http service on spark):** DELETE /tts/voices/{id} on burn, end, close and a device gone for good; no clause under 5 words to the clone.
- **F-10 voice lock (4a28867):** ECAPA per device (off by default; `models.voicelock`, extra `voicelock`), `voice.lock` event, `voice_lock_reset`; the speaker's phone notes an ignored turn and can re-learn.
- **A-03 speakerphone, C-12 reconnect, F-05 admin, F-03 install (fefd0bf):** `raum speakerphone`; phones rejoin and catch up, a departed phone stays a target 120 s, ids continue across agents; admin room sheets (devices, latency, QR, glossary, burn one room); `install.sh --user-space`.
- **F-08 desktop (Tauri v2) + F-09 Android (WebView):** `desktop/`, `android/`; box address form (checks /api/health), mic for the box only, `raum://` links, local-CA trust documented in their READMEs. CI only on dispatch / `v*` tags: `gh workflow run desktop.yml`, `gh workflow run android.yml -f link_host=<box ip>`. Join shows "Open in the RAUM app" in Android browsers.
- **App:** simultaneous-mode segments joined per (utt, lang, seg) in the store; merged utterances; voice lock note.
- **Voice:** resident Piper process per voice (restart on hang/death); TTS interface `synthesize(text, lang, voice_ref, rate)`; `http` engine for a voice-cloning service (:8090, fallback piper), voice sample in memory only (`pipeline/voice.py`, privacy test). Per device "My own voice / Standard voice" (`ParticipantInfo.voice`, Join + room menu, disclosure in the consent note; shown when `/api/health` `features.voice_clone`): standard = no sample taken or sent, a switch drops it at once (also in the service); speakerphone = standard.
- **F-07 document camera:** `POST /api/rooms/{room}/document` -> `document.ready` (photo never stored); camera in the Sprechzimmer/Kanzlei menu, cards in the transcript (frame in the viewer's language, reading in its own).
- **N-02 deal glossary:** `POST|GET /api/rooms/{room}/glossary` (YAML/CSV); compact row in the Verhandlungsraum menu. **K-02** explain simply hardened (background, falls back to the plain translation).
- **Guard:** spaced thousands, count words, unit synonyms; translate prompt with target-language conventions + Sie/du mirroring; back-translation num_predict capped.
- **Fact guard + security:** guard ~0.2 ms, eval 100% caught / 0% false alarms (ar, ru, tr, 90 records); AES-256-GCM store, burn by key deletion, hash-chained audit, `raum audit verify|show`.
- **App (port 5180):** Join (one-tap language chips, the whole field taps, headphones/speaker), Talk (language + voice output in the menu, same-language warning, voice-playing cue), Summary, Table (to-scale timeline + 1.0 s budget), Admin, Listen, setup `/`.
- **Design:** impeccable only. PRODUCT.md, direction contract `.impeccable/surfaces/`, finish review (fix -> verdict -> final batch), **DESIGN.md** + `.impeccable/design.json`.
- **No third-party bytes:** LiveKit gets LAN-only STUN/TURN (box's own :3478; compose, livekit.yaml, dev.sh), onnxruntime telemetry off (ops 739e8a1); privacy meter reads 0 B on the GX10.
- **Deploy:** `deploy/push.sh` -> `gx10:~/Desktop/HackathonRaum/raum`; `deploy/box.sh` (sources `runtime/raum.env`; process groups); `RAUM_UV_EXTRAS="--extra denoise"` for DeepFilterNet.
- **Tests:** `cd core && uv run pytest -q` -> 885 passed.

## In flight (parallel agents, worktree branches; the integrator merges)
1. AI-voice disclosure + per-device "own voice / standard voice" (ParticipantInfo.voice; standard: no sample, switching deletes it in the service; shown only when features.voice_clone).

## Integrator TODO
- Spoken AI disclosure for the speakerphone participant (no consent screen).
- After the freeze: checker meaning judge on the gpt-oss call (subtle changes score 0.88-0.99 similarity).
- P2: F-11 three people / three languages (pipeline already fans out to every listener language; needs the app/consent side and a 3-person room layout).

## How to run on a laptop
```bash
scripts/dev.sh            # livekit-server --dev (reused), core :8000, agent, app :5180
scripts/dev.sh --https    # phones on the LAN need https for the mic
cd core && uv run python ../scripts/replay.py --room sz-1234     # scripted conversation (UI test, no models)
cd scripts/e2e && npm i && npm run audio && node two-phones.mjs  # two Chrome phones, fake mics
```

## Blockers
- None. Real-engine paths are verified by ops on the box; new features land on `main` behind the mock profile first.
