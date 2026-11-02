# RAUM

**Translates. Checks. Forgets.**

RAUM is a private AI interpreter that lives in the room. It cleans the audio, translates both ways, checks every translation and writes the minutes. Nothing leaves the box.

Built for conversations where a leak is a legal problem:

| Room | People | What RAUM guards |
|---|---|---|
| Sprechzimmer | Doctor and patient | Doses, allergies, a take-home summary in the patient's language |
| Kanzlei | Lawyer and client | Deadlines and dates, plain-language explanations the lawyer approves |
| Verhandlungsraum | CEO and foreign partner | Every price offered, minutes both sides approve |

## How it works

Everyone joins with their own device by scanning a QR code. Each sentence moves through models that all run on one box:

1. **Silero VAD** detects speech, **DeepFilterNet** removes noise.
2. **Whisper large-v3-turbo** transcribes it.
3. **Gemma 4** translates it, streamed to every listener.
4. **Code** checks every number, date and name; **gpt-oss** translates it back and **bge-m3** scores the meaning.
5. **Piper** speaks it.

Sessions are encrypted with a key held only in memory. Burn destroys the key. A hash-chained audit log records what happened, never what was said.

Reference hardware: NVIDIA DGX Spark. Hardware profiles for RTX PCs, Apple Silicon, Jetson and laptops are defined in `profiles/`.

## Status

Hackathon build in progress (Macromedia Berlin Hackathon, 2–3 November 2026). The full build plan is in [plan.md](plan.md).

## License

Apache 2.0. See [LICENSE](LICENSE).
