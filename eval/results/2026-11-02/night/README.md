# 2 Nov 2026, evening and night: speed work on the GX10

Measured on the shared GX10 (GB10) box while friends phone-tested. Ops session plus background agents; the build session merged the code. Timings are ms after the speaker stops unless noted otherwise. Bot runs use `scripts/bot.py` with synthetic clips (macOS `say`).

## Model decisions (user)

| Stage | Before | Now | Why |
|---|---|---|---|
| Translator | gemma4:26b | **gemma4:e4b** | chrF++ tie, 78.6 vs 78.4 over 180 sentences in 6 directions (paired diff +0.4, 95 % CI [-1.6, +2.3]). 0 fact errors. 1 meaning slip on a legal ru>de sentence. About 1.5x faster (contended). Frees about 14 GB. |
| Checker | gpt-oss:120b | **gpt-oss:20b** | check 6–8.5 s → 1.6–1.9 s. Catch rate NOT measured yet. Frees about 50 GB. |
| ASR | whisper large-v3-turbo | same, plus `-nlp` and a second server for finals (:8082) | Parakeet-ultra: FLEURS-50 WER de/en/ru 4.7/4.3/5.6 vs turbo 7.1/5.2/6.5, but worse on drug names (37/47 vs 42/47), no prompt biasing, no tr/ar. `-nlp` skips a second encoder pass per request. |
| Voice | Chatterbox ML V3 | same (v1.4 + sync-free) | Dia2: English only, first audio ≥1.5 s, RTF >1. Web research found no faster commercial-licence de/en/ru model (Qwen3-TTS 0.28–0.40 s first audio on GB10 vs Chatterbox 0.18 s). |
| Quality metric as checker | – | rejected | MetricX-24 large caught 9/16 negation/allergy vs 15/16 for gpt-oss:120b. |

Raw translator and checker runs: `translator-checker/`.

## Box memory

Swap and compaction stalls with 26b + 120b loaded made every stage 2–4x slower. Free memory went from about 9 GB to about 60 GB after the switch.

## Voice pipeline fixes, measured on the received audio (`eval/tools/voice/rec.py` + `gaps.py`)

| Fix | Before | After |
|---|---|---|
| Edge-silence trim (service v1.4) | 0.49–0.58 s silence at every clause join; de1→en 6.37 s with 630 + 610 ms gaps | joins about 110 ms (comma) / 230 ms (sentence end); de1→en 4.39 s, 1 silence of 120 ms |
| Pace clamp 1.0–1.15 | pace 0.80–0.94 (+30 % length) | normal speed; pre-render matches the live pace |
| First phrase only at a clause boundary (≤12 words) | cut after 5 words ("twice \| a day") | cut at a comma or sentence end |
| Voice-in-flight GPU gate (bench, `gpu-sched/`) | everything loaded: first audio 1192 ms, worst stall 1035 ms, RTF 2.17 | 319 ms, stall 0, RTF 0.58 |
| Sync-free engine (`cb_patches.py`) | 42 host↔GPU syncs per chunk decode; under load RTF 1.06, stall about 300 ms | 1 sync; RTF 0.81, stall 150–200 ms; bit-identical audio |
| Finals on whisper :8082 + `background_slots: 2` | finals waited behind partials 0.3–1.9 s on long turns | no queue for finals |

The first round of TTS research and diffs is in the session scratchpad. The merged code is in services/tts and core.

## Live mode (simultaneous for speaker listeners too)

`long-de1` (German, 3 sentences, about 18 s), gs room, listener on phone speaker. Times are from the speaker's START:

| Build | First text | First voice | Segment finals |
|---|---|---|---|
| Before (speaker listeners consecutive) | 16–22 s | 16–22 s | all at the end |
| 5b2851c, live for everyone | 6.5–7.1 s | 7.2–8.5 s | 6.9, 10.0, 13.3, 16.4, 19.1 s |
| 7bdec35, one-partial commit + voice ahead of commit | 6.3 s | 6.5 s (en) / 7.6 s (ru) | 6.3, 9.5, 13.6, 16.6, 19.0 s |

Silences in the received voice were 110–280 ms (natural), plus one 750 ms catch-up wait after segment 1 (the voice waiting for the next commit).

Short turn (de1, consecutive): voice 0.65–1.0 s after the stop.

## Code-switching

ru speech with an English phrase lost the phrase: the all-Cyrillic glossary prompt made whisper drop or transliterate it.

Fixed by:
- a code-switch seed sentence before the glossary terms;
- re-inserting a Latin run from the partials that the final dropped (`pipeline/codeswitch.py`).

Test clips: `mix-ru` (accented) and `mix2-ru` (native English voice).

## Not done or not measured

- gpt-oss:20b catch rate.
- gemma4:e2b and TranslateGemma.
- Canary, Qwen3-ASR and Voxtral full runs.
- CUDA graphs for S3Gen/HiFT.
- A batch-2 T3 step across rooms.
- Clean (quiet-GPU) latencies for the benchmarks: the timing slots were cancelled.

Draft text (963c5ac), the glass English UI (7a029d3) and push-to-talk (5bbcf13) were merged while the box was off. They are not yet measured on the box.
