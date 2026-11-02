# RAUM build status

_Read by the ops session. Updated at every milestone. Last update: Mon 2 Nov, ~14:45._

## What works (on `main`, verified on the M2 Mac)
- **Full loop with real microphones (mock engines):** two Chrome phones join a room; the doctor's mic audio goes through LiveKit -> raum-agent -> Silero VAD -> mock ASR -> streamed mock translation -> second check. The patient's phone shows the Arabic translation with a green check about 1.2 s after the speaker stopped, and the table screen shows the stage timings. The pipeline's own bot test: first words p50 ~0.8 s, full sentence ~1.1 s (mock latencies from the §3 table).
- **raum-core** (`uv run raum core`): join + LiveKit tokens (role/lang/device/path metadata), rooms, sessions, control (off-record, confirm, repeat-back, explain-simply, human interpreter), presence (RTT badge), end -> summary (mock), approve, burn, compliance export, events WS mirror + backlog, privacy meter (nft or stub), seal, GPU stats, admin, agent link. Docs: `/api/docs`.
- **raum-agent** (`uv run raum agent start`): LiveKit Agents worker, auto-dispatch to every room, Silero VAD per speaker, RoomPipeline (§6.4 events, partials, per-language streaming, uncertain flag, confirm flags, offer tracker, repeat-back, explain-simply, V-04/V-05), link to core with reconnect. Capacity = rooms (`RAUM_AGENT_MAX_ROOMS`, default 12), not CPU.
- **Engines** (`core/raum/pipeline/`): mock for every stage; real `whisper.cpp` (`/inference`, verbose_json), Ollama translate/verify/embed, Piper TTS, DeepFilterNet (optional). **Real engines are only tested against mocked HTTP**; expect small fixes on the first real run.
- **App** (port **5180**): Join, Talk, Summary (approve, print, take-home QR, burn), Table (bilingual + glass box + privacy meter + device plates + join QR codes), Admin, listen-only `/l/:room`, setup `/`. Design via impeccable (`PRODUCT.md`, `.impeccable/surfaces/`). Self-hosted fonts, no third-party requests. RTL verified with Arabic. Light (Lichtpause) and dark (Blaupause).
- **Eval** (`eval/`): 90 records (ru, tr, ar), `run_eval.py`, `bakeoff.py`, `load_test.py`, `report.py`.
- **Deploy** (`deploy/`): `push.sh` -> `gx10:~/Desktop/HackathonRaum/raum`, `box.sh` (user-space core + agent + optional app preview), Caddyfile, compose, raum.nft, systemd units (dedicated box only), `install.sh`.
- **Tests:** `cd core && uv run pytest -q` -> 257 passed.

## In progress
- **Fact guard + security** (AES-256-GCM session store, burn, hash-chained audit, `raum audit verify`) on a branch. Until merged, core keeps sessions and audit in memory and `verify.facts` is empty (no fact dimensions on real runs; the replay script shows them).
- impeccable finish review -> fixes -> DESIGN.md.

## How to run on a laptop
```bash
scripts/dev.sh            # livekit-server --dev (reused if running), core :8000, agent, app :5180
scripts/dev.sh --https    # self-signed https so phones on the LAN can use the mic
cd core && uv run python ../scripts/replay.py --room sz-1234   # scripted conversation into a room (UI test, no models)
cd core && uv run python ../scripts/bot.py --room sz-1234 --role a --lang de --wav x.wav   # speaking bot
```

## Going live on the GX10 (ops)
1. `deploy/push.sh` (from a clean worktree of origin/main). Restarts via `deploy/box.sh` on the shared box.
2. Real engines: `RAUM_PROFILE=spark deploy/box.sh restart` (or `profile: spark` in raum.yaml). Needs whisper-server :8081, Ollama :11434 with gemma4:26b, gpt-oss:120b, bge-m3. If gpt-oss:120b isn't pulled yet, override `models.verify.model: gpt-oss:20b` or `engine: off` in raum.yaml.
3. App for phones: `RAUM_APP=1 RAUM_HTTPS=1 deploy/box.sh restart` serves the build on :5180 (proxies /api and /rtc). Port 443 on the box is taken; don't bind it.

## Blockers
- None on this side. Real-engine response formats untested until the box runs them.
