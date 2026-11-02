# Measurements on the GX10, 2 Nov 2026

Raw results behind the numbers in STATUS.md and the pitch. Audio is not committed (licences, size).
The box was shared with other teams' jobs during most runs, so latencies are upper bounds.

## End of turn (`eou/`)
86 synthetic clips (de/ru/en/tr/ar; 38 complete, 48 with a 480-960 ms mid-sentence pause), plus
spliced versions. `scores.json` / `scores_splice.json` per policy.

| policy | mid-sentence pauses cut | transcript ready p50 |
|---|---|---|
| VAD 0.6 s | 27/48 | 934 ms |
| semantic (smart-turn + LiveKit text model, shipped 47ed350) | 4/48 | 635 ms |

Live after the rules (0457b5b, 6ca5b43, e88c2c9): the German, English and Russian pause traps
("Sie dürfen das Metformin ab morgen [800 ms] nicht mehr nehmen.", "You should not [800 ms] take ...",
"Вам нельзя [800 ms] принимать ...") stay one utterance; voice starts 0.85-1.2 s after the speaker stops.

## Checker (`checker/`)
`g3-analysis.txt` (90 records, ar/ru/tr) and `g3b-analysis.txt` (120 records, en/ru/tr/ar, incl. 40 new
subtle-meaning perturbations). Fact guard 100 % caught, 0 false alarms. Second-AI checker at red 0.83:
15/16 negation and omitted allergy, 22 % of subtle changes, 1-2 % false alarms.

## Voices (`tts/`)
- `chatterbox/`: Chatterbox Multilingual V3 (MIT) bake-off and the streaming service (`service.json`), Qwen3-TTS for comparison.
- `fish/`: Fish OpenAudio S1-mini and S2-Pro (non-commercial / research licences; listening test only),
  plus Chatterbox and S2-Pro cloned from ElevenLabs library reference clips (`*-el.json`).

| model | first audio (stream) | real-time factor | licence |
|---|---|---|---|
| Chatterbox ML V3 (shipped) | 0.18-0.37 s | 0.30-0.60 | MIT |
| Fish S1-mini | 0.26 s | 0.69 | CC-BY-NC-SA |
| Fish S2-Pro | 0.88 s | 2.4 (not real-time on GB10) | Fish Audio Research |

`asr*.json` = Whisper CER of the generated audio, `sim*.json` = WavLM speaker similarity.
