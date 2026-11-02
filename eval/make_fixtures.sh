#!/usr/bin/env bash
# Generate spoken test utterances for eval/load_test.py from the eval set sentences.
#
#   eval/make_fixtures.sh                  # de + ru + tr + ar, every source sentence
#   eval/make_fixtures.sh de ru            # only these languages
#   LIMIT=4 eval/make_fixtures.sh de tr    # first 4 sentences per language
#
# Output: eval/fixtures/gen/<lang>/<id>.wav  (16 kHz, mono, 16-bit PCM, ~50-150 KB each).
# The gen/ folder is ignored by git (root .gitignore only un-ignores eval/fixtures/*.wav),
# so the fixtures are always regenerated on demand and never bloat the repo.
#
# Engines, first one found wins:
#   macOS   say (voices Anna de, Milena ru, Yelda tr, Majed ar) + afconvert
#   Linux   piper  if PIPER_VOICE_<LANG> points to a .onnx voice (e.g. PIPER_VOICE_DE=/opt/raum/voices/de.onnx)
#           espeak-ng + ffmpeg otherwise
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${OUT:-$HERE/fixtures/gen}"
LIMIT="${LIMIT:-0}"
if [ "$#" -gt 0 ]; then LANGS=("$@"); else LANGS=(de ru tr ar); fi

voice_say() {
  case "$1" in
    de) echo "${SAY_VOICE_DE:-Anna}" ;;
    ru) echo "${SAY_VOICE_RU:-Milena}" ;;
    tr) echo "${SAY_VOICE_TR:-Yelda}" ;;
    ar) echo "${SAY_VOICE_AR:-Majed}" ;;
    *) echo "" ;;
  esac
}

# Print "<id>\t<text>" for every source sentence spoken in <lang> (German ones de-duplicated).
sentences() {
  python3 - "$HERE/sets" "$1" "$LIMIT" <<'PY'
import json, pathlib, sys
sets, lang, limit = pathlib.Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
seen, n = set(), 0
for p in sorted(sets.glob("*.jsonl")):
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r["src_lang"] != lang or r["src"] in seen:
            continue
        seen.add(r["src"])
        sid = r["id"].rsplit("-", 1)[0] if lang == "de" else r["id"]
        print(f"{sid}\t{r['src']}")
        n += 1
        if limit and n >= limit:
            sys.exit(0)
PY
}

synth() {  # synth <lang> <text> <out.wav>
  local lang="$1" text="$2" out="$3" tmp
  tmp="$(mktemp -t raumfx.XXXXXX)"
  if [ "$(uname -s)" = "Darwin" ] && command -v say >/dev/null; then
    local v; v="$(voice_say "$lang")"
    [ -n "$v" ] || { echo "no say voice for $lang" >&2; return 1; }
    say -v "$v" -o "$tmp.aiff" -- "$text"
    afconvert -f WAVE -d LEI16@16000 -c 1 "$tmp.aiff" "$out"
    rm -f "$tmp.aiff"
  else
    local up var
    up="$(echo "$lang" | tr '[:lower:]' '[:upper:]')"
    var="PIPER_VOICE_$up"
    if command -v piper >/dev/null && [ -n "${!var:-}" ]; then
      printf '%s' "$text" | piper --model "${!var}" --output_file "$tmp.wav" >/dev/null 2>&1
    elif command -v espeak-ng >/dev/null; then
      espeak-ng -v "$lang" -w "$tmp.wav" -- "$text"
    else
      echo "no TTS engine: install espeak-ng (apt-get install espeak-ng) or set $var for piper" >&2
      return 1
    fi
    ffmpeg -loglevel error -y -i "$tmp.wav" -ar 16000 -ac 1 -sample_fmt s16 "$out"
    rm -f "$tmp.wav"
  fi
  rm -f "$tmp"
}

total=0
for lang in "${LANGS[@]}"; do
  mkdir -p "$OUT/$lang"
  while IFS=$'\t' read -r id text; do
    [ -n "$id" ] || continue
    f="$OUT/$lang/$id.wav"
    if [ -s "$f" ]; then continue; fi
    synth "$lang" "$text" "$f"
    total=$((total + 1))
  done < <(sentences "$lang")
  echo "$lang: $(find "$OUT/$lang" -name '*.wav' | wc -l | tr -d ' ') files in $OUT/$lang"
done
echo "generated $total new file(s)"
