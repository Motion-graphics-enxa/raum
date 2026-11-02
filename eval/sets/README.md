# Eval sets (plan §12.1)

One JSON object per line, one file per room. Each file holds the room's 10 sentences in every
prepared language: **ru** and **tr** (the demo language is one of them, §19) and **ar**.
So each language is a complete 30-sentence set on its own:

- 10 sentences per room, both directions (German professional → other party, other party → German),
  every sentence with at least one fact
- 10 perturbed translations: wrong number 3, wrong unit 2, negation flipped 2, allergy omitted 2,
  date shifted 1
- a clean reference translation on every record (30 ≥ the 10 the plan asks for) to measure false alarms
- all seven §20.7 demo sentences (as German source or German reference)

`core/tests/test_eval_sets.py` checks every rule above. Run it after editing: `cd core && uv run pytest -q tests/test_eval_sets.py`.

## Record

```json
{"id": "sz-01-ru", "room": "sprechzimmer", "src_lang": "de", "tgt_lang": "ru",
 "src": "Nehmen Sie zweimal täglich 500 mg Metformin.",
 "facts": [{"kind": "dose", "value": "500 mg"}, {"kind": "frequency", "value": "2/day"},
           {"kind": "medication", "value": "Metformin"}],
 "ref": "Принимайте метформин по 500 мг два раза в день.",
 "perturbation": "wrong_number",
 "perturbed": {"tgt_text": "Принимайте метформин по 50 мг два раза в день.", "expect": "red",
               "note": "500 mg -> 50 mg"},
 "approved": false}
```

| Field | Meaning |
|---|---|
| `id` | `<sentence>-<lang>`, e.g. `sz-01-ru`. The sentence part (`sz-01`) is shared across languages and starts with the room prefix |
| `room` | mode id; must match the file name |
| `src_lang`, `tgt_lang` | one of them is always `de` |
| `src` | what the speaker says |
| `facts` | what the fact guard must protect, normalised (below). Kinds must be in the room's `guard.kinds` |
| `ref` | clean reference translation (expect **green**; red = false alarm) |
| `perturbation` | `wrong_number` · `wrong_unit` · `negation` · `allergy_omitted` · `date_shifted` |
| `perturbed.tgt_text` | the reference with one deliberate error; `expect` is always `red` |
| `perturbed.note` | what was changed, for the report |
| `approved` | `false` until a native speaker has approved `src` / `ref` / `perturbed` for this language |

For X→de records the German reference and German perturbation are the same in every language;
for de→X records the German source is the same.

### Fact value normalisation

Language-independent, so the same sentence has the same facts in every language. This is what
`run_eval.py` compares against `guard.extract()` (extraction recall is informational: it only
counts when the guard normalises the same way).

| Kind | Value | Examples |
|---|---|---|
| `dose` | number + canonical unit (`mg g µg ml l IE`) | `500 mg`, `5 ml` |
| `frequency` | times per day | `2/day` |
| `medication`, `allergy` | German glossary form | `Metformin`, `Penicillin` |
| `date`, `deadline` | ISO 8601 without year, `--MM-DD` (or `--MM` for a month) | `--11-24`, `--03` |
| `time` | 24 h `HH:MM` | `09:00` |
| `duration` | ISO 8601 duration | `P3W`, `P6Y` |
| `number` | plain, `.` as decimal separator, no thousands separator | `3`, `5000` |
| `amount` | number + ISO 4217 currency if stated | `1200000 EUR`, `1050000` |
| `currency` | ISO 4217 | `EUR` |
| `percent` | number + `%` | `3%` |
| `negation` | `not`, one entry per negation | `not` |

The sets deliberately include locale traps for the guard: `3 200` (ru thousands space), `950.000`
(de/tr thousands dot) vs `950,000` (ar), `1,2` vs `1.2` million, `%3` (tr percent prefix),
number words (`drei`, `три`, `üç`, `ثلاثة`), Turkish verb-suffix negation (`almayın`) and Russian
case endings on drug names (`метформина`).

## Native-speaker approval (Sprint 2, plan §10.2 S)

All records start as `"approved": false`: the translations are drafts. Before the proof slide:

1. The native speaker for the demo language reads every record of that language
   (`grep -h -- '-ru"' eval/sets/*.jsonl` for Russian). They check that `src` is how a real
   patient/client/partner would say it, that `ref` is a correct, natural translation, and that
   `perturbed.tgt_text` differs from `ref` **only** by the error named in `perturbation`.
2. Fix text in place (keep `facts` unchanged unless the sentence's meaning changed; keep the
   perturbation type). Then set `"approved": true` on that record.
3. Run `cd core && uv run pytest -q tests/test_eval_sets.py`.
4. Rerun the eval on approved records only:
   `uv run --project core python eval/run_eval.py --lang ru --approved-only`.

The proof table (`eval/report.py`) labels results from unapproved records as a draft set.

## Running

```bash
# any laptop: fact guard only (mock profile)
uv run --project core python eval/run_eval.py
# on the box: + translator, back-translation (gpt-oss) and bge-m3 similarity
RAUM_PROFILE=spark uv run --project core python eval/run_eval.py --profile spark --lang ru
# translator bake-off (§6.3)
uv run --project core python eval/bakeoff.py --ollama http://127.0.0.1:11434 --lang ru
# load test (§12.3): fixtures first, then N rooms x 2 bots
eval/make_fixtures.sh de ru
uv run --project core python eval/load_test.py --url ws://<spark-ip>:7880 --lang ru
# proof table (§12.2)
uv run --project core python eval/report.py --qr-seconds 7.4,8.1,6.9
```

Results go to `eval/out/` (git-ignored, not pushed to the box by `deploy/push.sh`).
