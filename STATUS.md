# RAUM build status

_Read by the ops session. Updated at every milestone. Last update: Mon 2 Nov, afternoon._

## What works (on `main`)
- **Contract:** event protocol `core/raum/events.py` <-> `app/src/lib/events.ts` (plan §6.5 + marked extensions); room schema `core/raum/modes/schema.py` + `modes/*.yaml`; `raum.yaml` + `profiles/*.yaml`; prompts (§20); glossaries; agent<->core link `core/raum/link.py`.
- **raum-core** (FastAPI, `uv run raum core`): join + LiveKit tokens with role/lang/device/path metadata, rooms, sessions, control, presence, end -> summary (mock), burn, compliance export, events WS mirror + backlog, privacy meter (nft or stub), seal, GPU stats, admin, agent link. Docs at `/api/docs`.
- **App** (`cd app && npm run dev`, port **5180**; 5173 is taken by another project on this Mac): Join `/j/:room?role=a|b`, Talk `/r/:room`, Summary `/r/:room/summary`, Table `/t/:room`, Admin `/admin`, listen-only `/l/:room`, setup `/`. Design via impeccable: `PRODUCT.md`, `.impeccable/surfaces/` (direction contract). Fonts self-hosted.
- **Tests:** `cd core && uv run pytest -q` (contract + core API).

## In progress (parallel branches, merging as they land)
- Pipeline engines (mock + real whisper.cpp/Ollama), RoomPipeline, LiveKit agent worker, `scripts/bot.py`.
- Fact guard (§6.8) + security (AES-GCM store, burn, hash-chained audit, `raum audit verify`).
- Eval (`eval/`) + deploy (`deploy/`, `install.sh`, `deploy/push.sh`).

## How to run on a laptop
```bash
scripts/dev.sh            # livekit-server --dev, core :8000, agent, app :5180
scripts/dev.sh --https    # self-signed https so phones on the LAN can use the mic
```
Open `http://localhost:5180/`, pick a room type, scan/open the two QR links, open `/t/<room>` for the table screen.

## Going live on the GX10
1. `raum.yaml`: `profile: mock` -> `profile: spark` (engines: whisper.cpp :8081, Ollama gemma4:26b / gpt-oss:120b / bge-m3).
2. `deploy/push.sh` (rsync + uv sync + npm build + restart services). Caddy proxies `/api` and `/rtc` on one origin.

## Blockers
- GX10 not reachable yet (ops session). Everything here runs on the mock profile.
