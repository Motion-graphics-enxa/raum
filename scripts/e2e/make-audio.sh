#!/usr/bin/env bash
# Spoken test audio with macOS `say` (de doctor, ar patient) + silence, 48 kHz mono 16-bit,
# looped with silence so Silero VAD sees clean turns. Output: recordings/e2e/ (gitignored).
set -euo pipefail
OUT="$(cd "$(dirname "$0")/../.." && pwd)/recordings/e2e"
mkdir -p "$OUT" && cd "$OUT"
say -v Anna -o de1.aiff "Nehmen Sie zweimal täglich fünfhundert Milligramm Metformin."
say -v Majed -o ar1.aiff "أنا أتناول خمسمئة ملغ من الميتفورمين مرتين يوميا، وعندي حساسية من البنسلين."
python3 - <<'PY'
import subprocess, wave
for name in ("de1", "ar1"):
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@48000", "-c", "1", f"{name}.aiff", f"{name}_raw.wav"], check=True)
    with wave.open(f"{name}_raw.wav") as w:
        frames, sr = w.readframes(w.getnframes()), w.getframerate()
    silence = b"\x00\x00" * sr * 6
    with wave.open(f"{name}_loop.wav", "wb") as o:
        o.setnchannels(1); o.setsampwidth(2); o.setframerate(sr)
        o.writeframes(silence[: sr * 2 * 2] + frames + silence)
with wave.open("silence.wav", "wb") as o:
    o.setnchannels(1); o.setsampwidth(2); o.setframerate(48000); o.writeframes(b"\x00\x00" * 48000 * 10)
PY
rm -f ./*.aiff ./*_raw.wav
echo "audio in $OUT"
