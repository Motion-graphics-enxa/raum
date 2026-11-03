# 3 Nov 2026: gpt-oss:20b as RAUM's meaning checker (catch rate and false alarms)

gpt-oss:20b replaced gpt-oss:120b as the checker on 2 Nov (d2ec53d) for speed. Its catch rate had not been measured. This run measures it on the same 320 checker items as the 120b baseline (`eval/results/2026-11-02/night/translator-checker/out/b-items.json`: 120 clean references, 120 live gemma4:26b MT, 80 perturbations).

## Method: the production check, not the judge prompt

What ran on the GX10 for each item is exactly the live second check (`raum.verify.backtranslate.second_check`):

1. `OllamaVerifier.back_translate`, with `prompts/backtranslate.md` and the spark verify options (`reasoning: low` → `think: "low"`, temperature 0). num_predict is capped at 64..256; if the reply ends with done_reason=length, the call is retried once at 256.
2. bge-m3 cosine between the source and the back-translation.
3. The box `raum.yaml` thresholds: **red if < 0.83**, amber if 0.83–0.86.

The checker code on the box matched origin/main (md5 checked). There was one deviation from production: `num_ctx: 8192` was sent explicitly. This equals the box default, so no model was reloaded.

The current fact guard was also run on every item (CPU only), so the production badge (guard red wins) can be shown.

**"Caught" means red (similarity < 0.83).** The 120b numbers come from the g3b run (2 Nov, 15:51–16:13): same items, same verifier class and prompt, bge-m3, same thresholds (`base.similarity` in b-items.json). This is the source of the night README's "15/16 negation/allergy".

## Results (n = 320, 0 errors)

| items | gpt-oss:20b red | gpt-oss:120b red | 20b amber or red | 120b amber or red |
|---|---|---|---|---|
| negation (8) | 8/8 | 7/8 | 8/8 | 7/8 |
| allergy omitted (8) | 8/8 | 8/8 | 8/8 | 8/8 |
| subtle semantic (40) | 11/40 | 9/40 | 14/40 | 12/40 |
| wrong number (12) | 0/12 | 0/12 | 0/12 | 0/12 |
| wrong unit (8) | 0/8 | 0/8 | 0/8 | 0/8 |
| date shifted (4) | 0/4 | 0/4 | 1/4 | 1/4 |
| **all perturbations (80)** | **27/80** | **24/80** | 31/80 | 28/80 |
| **clean references: false alarms (120)** | **8/120** | **1/120** | 9/120 | 4/120 |
| **live MT flagged (120)** | **4/120** | **2/120** | 7/120 | 5/120 |

- **Paired:** every disagreement at the 0.83 cut goes the same way. 20b is red and 120b is not on 3 perturbations, 7 references and 2 MT items. There are 0 cases the other way round.
- **At equal false alarms, 120b separates better.** 20b's similarities run lower overall: it catches more at 0.83, but it also raises 8× the reference false alarms. With the threshold set so that both models have the same clean-reference false alarms:

  | ref false alarms | 20b: meaning perts (neg + allergy + semantic, 56) | 120b |
  |---|---|---|
  | ≤ 1/120 | 13/56 (semantic 2/40) | 24/56 (semantic 9/40) |
  | ≤ 2/120 | 19/56 (8/40) | 27/56 (12/40) |
  | ≤ 8/120 | 27/56 (11/40) | 33/56 (17/40) |

  AUC (meaning perturbations vs clean refs): 20b 0.882, 120b 0.898.
- **Where the false alarms come from:** 20b's 8 reference false alarms are 5 tr, 2 ar, 1 en and 0 ru. The pattern is back-translations into Turkish or Arabic that keep a German word ("Kündigung'i 3 Kasım'da aldım") or garble a term (Arabic tatweel in "Penicillin"). All 8 are kanzlei or sprechzimmer sentences: dismissal or termination wording (6) and penicillin allergy (2).
- **Live MT flags, read by hand:** all 4 red 20b flags (kz-04-ar, kz-04-tr, kz-06-tr, kz-08-tr) are correct translations, so they are false alarms. 120b's 2 red flags are false alarms too. One flagged MT has a real error: kz-04-ru, Kündigung rendered as Abmahnung. Both models only marked it amber (0.8454). The 113 unflagged MT items were not reviewed by hand.
- **With the fact guard (the production badge):** the guard alone is red on all 24 number/unit/date perturbations and all 16 negation/allergy perturbations, with 0 false alarms. The badge is red on 51/80 perturbations with 20b and 49/80 with 120b. The checker's own contribution is the subtle semantic set (11/40 vs 9/40). The guard adds no false alarms, so badge false alarms are the same as above: refs 8/120 vs 1/120, MT 4/120 vs 2/120.
- **Missed by both models in all 4 languages:** the semantic swaps "over 39 °C" → "under", "at least" → "at most", "until" → "from", gross → net, "I take" → "my husband takes" and "come back" → "call us". The back-translation keeps the change, but the bge-m3 cosine stays at 0.86–0.98 (20b). "Labour court hearing" → "meeting with your employer" was caught in 1 of 4 languages by each model. Discount → surcharge was caught in 4/4 by both.

## Latency (gpt-oss:20b, measured only while no live room was active)

| | n | p50 ms | p95 ms | max ms |
|---|---|---|---|---|
| whole check (back-translation incl. retry + embed) | 320 | 1025 | 3058 | 5129 |
| one Ollama /api/chat call | 440 | 1103 | 1742 | 4016 |
| checks without a retry | 200 | 866 | 1114 | 1215 |
| checks with a retry | 120 | 2455 | 3360 | 5129 |
| bge-m3 embed | 320 | 18 | 23 | 87 |
| 120b, g3b run (26b + 120b loaded, memory pressure; not comparable) | 320 | 4256 | 11222 | 24105 |

No check took more than 20 s (the production timeout). No call showed a model load over 500 ms, so nothing was reloaded.

**120/320 checks (37.5 %) retried.** The first call's budget was 64 tokens, and gpt-oss:20b's low reasoning plus the translation needed more (p50 80, p95 129 tokens on the call that finished). Retries were most common for back-translations into German: 82/192. All 320 final calls ended with done_reason=stop.

Suggested change, not applied and its latency not measured: raise the floor in `core/raum/pipeline/verifier.py`:

```diff
-BT_NUM_PREDICT_MIN = 64
+BT_NUM_PREDICT_MIN = 128
```

From the recorded token counts, 6/320 checks instead of 120/320 would still hit the cap. num_predict is only a ceiling, so short replies do not get slower.

## Live-room pauses: 5 pauses, 3093 s (51.5 min) in total

Concurrency was 1. Before every Ollama call, the bench checked core first, then `/api/ps`: it continued only if gpt-oss:20b and bge-m3 were resident at ctx 8192. It also stopped if MemAvailable fell under 8 GiB (sampled values were 69–74 GiB). The pause rule changed twice during the run, on ops' instruction:

1. Pause if `/api/health` rooms > 0.
2. Pause if `/api/rooms` shows any participants or subscribers.
3. Pause if `/api/rooms` shows participants or subscribers and idle_s ≤ 600; pause if the endpoint does not answer.

The pauses:

| start | s | why |
|---|---|---|
| 10:35:20 | 231 | rule 1; ops: an open but empty test room |
| 10:39:14 | 54 | ops bot test room sz-7102 (ended by the restart to rule 2) |
| 10:46:10 | 2738 | phones in gs-6585 / gs-4923. The last ~20 min was one idle participant in gs-4923 (idle_s ~1200), which rule 3 would ignore |
| 11:32:36 | 55 | ops test room sz-7103 |
| 11:34:14 | 15 | `/api/rooms` unreachable (core restart during ops' updates) |

The first result came in at 10:39:12 and the last at 11:36:49.

## Files

- `results.json`: one row per item: back-translation, similarity, badge, guard result, timings, and each Ollama call's num_predict, done_reason, eval_count and server time.
- `summary.json`: all the numbers above, the discordant items, and the lists of flags and misses.
- `pauses.jsonl`: the pause log.
- `bench_c20.py`: the bench, run on the box from `~/Desktop/HackathonRaum/runtime/eval-checker20` with `uv run --no-sync --project ~/Desktop/HackathonRaum/raum/core python bench_c20.py`. It is resumable.
- `analyze_c20.py`: rebuild with `python3 analyze_c20.py <run-dir> <out-dir>`.
