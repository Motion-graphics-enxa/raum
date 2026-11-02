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

Reference hardware: ASUS Ascent GX10 (NVIDIA GB10). Hardware profiles for RTX PCs, Apple Silicon, Jetson and laptops are defined in `profiles/`.

## Run it

On any laptop, with mock models (real microphones, real voice detection, canned translations):

```bash
brew install livekit            # or any livekit-server binary
scripts/dev.sh                  # LiveKit --dev, raum-core :8000, raum-agent, app :5180
open http://localhost:5180/     # open a room: QR codes for both phones and the table screen
```

Use `scripts/dev.sh --https` so phones on the same network can use the microphone. On the box, switch `raum.yaml` to `profile: spark` and deploy with `deploy/push.sh`. Tests: `cd core && uv run pytest -q`.

| Path | What |
|---|---|
| `core/` | raum-core (FastAPI) and raum-agent (LiveKit Agents worker), Python 3.12 + uv |
| `app/` | phone and table-screen app (React, Vite, livekit-client) |
| `modes/` | the three rooms as YAML files |
| `raum.yaml`, `profiles/` | model registry and hardware profiles |
| `eval/` | test sets, evaluation, model bake-off, load test |
| `deploy/` | Caddy, LiveKit, nftables privacy meter, services, push script |

## Status

Hackathon build in progress (Macromedia Berlin Hackathon, 2–3 November 2026). The full build plan is in [plan.md](plan.md); live status in [STATUS.md](STATUS.md).

## License

Apache 2.0. See [LICENSE](LICENSE).
