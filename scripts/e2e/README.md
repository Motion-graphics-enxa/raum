# Browser end-to-end checks

Two headless Chrome "phones" with fake microphones (Chrome's `--use-file-for-fake-audio-capture`;
the audio-service sandbox must be off for it to read the file) join one room on a running stack.

```bash
scripts/dev.sh                        # in another terminal
cd scripts/e2e && npm install && npm run audio
node two-phones.mjs http://localhost:5180 40 sprechzimmer a   # doctor speaks German, patient (ar) reads
node replay-shots.mjs http://localhost:5180 light             # scripted conversation via the agent link, every screen
node admin-shot.mjs http://localhost:5180                     # admin console (F-05), light/dark/phone width
```

Screenshots and logs go to `eval/out/e2e/` (gitignored). Needs Google Chrome in /Applications.

## C-12 reconnect and resume: a phone drops for 10 s, the conversation continues

Acceptance (plan §5.1): kill Wi-Fi on a phone for 10 s; the conversation continues. Concretely:
every turn spoken while the phone was away is translated into its language and is on its screen
after it comes back; the session (id, encryption key, turns) never restarts; utterance ids never
repeat; the microphone is on again if it was on; the phone's RoomStore is kept (turns not reset).

### 1. Bots (any machine; `reconnect.py`)

A speaking bot (role a, de, `scripts/bot.py --token`) talks every 6 s. A listening phone (role b, ar;
same catch-up logic as `app/src/lib/live.ts`) records every envelope; after a reconnect or a rejoin
it fetches `GET /api/rooms/:room/events?since=` and merges, deduplicated. Both join through
`POST /api/join`, so their tokens carry core's room configuration.

```bash
say -v Anna -o /tmp/de.aiff "Nehmen Sie zweimal täglich fünfhundert Milligramm Metformin." \
  && afconvert -f WAVE -d LEI16@48000 /tmp/de.aiff /tmp/de.wav
cd core
uv run python ../scripts/e2e/reconnect.py --wav /tmp/de.wav --scenario stop  --gap 10   # frozen, no goodbye
uv run python ../scripts/e2e/reconnect.py --wav /tmp/de.wav --scenario stop  --gap 30   # past LiveKit's drop
uv run python ../scripts/e2e/reconnect.py --wav /tmp/de.wav --scenario kill  --gap 10   # app killed, reopened
uv run python ../scripts/e2e/reconnect.py --wav /tmp/de.wav --scenario leave --gap 10   # dropped, rejoins
uv run python ../scripts/e2e/reconnect.py --wav /tmp/de.wav --scenario empty --gap 25   # whole room gone
```

Exit 0 = nothing missing at the end, no turn left untranslated for the phone, utterance ids
increasing, same session. `--core` / `--url` point at another stack. On a LiveKit server shared
with other stacks, isolate the agent: run it with `RAUM_AGENT_NAME=<name>` and core with the same
variable (core's tokens then dispatch only that agent). For `empty`, the gap must exceed the room's
departure timeout (`network.departure_timeout_s`; set it to 8 for a quick run).

### 2. Chrome (`reconnect-browser.mjs`, the real `live.ts`)

Needs the dev server (`npm run dev`: the `window.__raumLive` test hook exists only in dev builds) and
`npm run audio`. Chrome's offline emulation stops the page's HTTP and WebSockets but not WebRTC, so the
drop itself comes from LiveKit:

```bash
node reconnect-browser.mjs http://localhost:5180 10 leave   # server drops the phone; offline 10 s; live.ts rejoins
node reconnect-browser.mjs http://localhost:5180 10 full    # offline 10 s during LiveKit's own full reconnect
```

Pass: every Arabic turn of the session is in the phone's store, the RoomStore is the same object
as before the drop, the mic is published and live again.

### 3. Real phones (the acceptance run)

Two phones in a room (`/admin` -> new room -> QR codes), mics on, the doctor talks. Switch the
patient's Wi-Fi off for 10 s, keep talking, switch it back on. Expected: "Reconnecting..." banner,
then the turns spoken meanwhile appear (translated, checked) without a tap, the mic dot is live
again. Watch `/admin`: after ~20 s offline the patient's plate goes and comes back on return
(10 s is usually bridged without a drop); agent stays "verbunden"; session start time unchanged.
Repeat with both phones off for 30 s.

### Results (2 Nov, M2 Mac, livekit-server 1.13.11 --dev, mock profile)

| Scenario | What LiveKit / the agent saw | Phone got | Missing |
|---|---|---|---|
| stop 10 s | nothing: no reconnect, participant never left; data held and delivered on resume (5.3 s late) | all live | 0 |
| stop 30 s | server dropped the phone after 21.2 s; client full reconnect 0.2 s after resume | 5 gap turns via backlog (107 events) | 0 |
| kill 10 s | ghost kept until the same identity rejoined (+11.0 s) | 94 backlog events on rejoin | 0 |
| leave 10 s | left +0.3 s, joined again +10.1 s | gap turns via backlog | 0 (2 before the fix) |
| empty 25 s (departure 8 s) | room closed, new agent job | turns continue at u-0004 | 0 (ids restarted at u-0001 before the fix) |
| Chrome leave + offline 10 s | 3 failed rejoins (1, 2, 4 s), rejoined 0.3 s after `online` | gap turn, same store, mic live | 0 |
| Chrome full reconnect, offline 10 s | LiveKit reconnected 2.5 s after the network returned | gap turn, same store, mic live | 0 |

Timings that matter:

- **Client** (livekit-client 2.23): up to 10 reconnect attempts, delays 0, 0.3, 1.2, 2.7, 4.8, then
  7 s x5 (each +-50 % jitter), about 44 s of backoff plus 15 s per attempt (signal and peer
  connection timeouts). After that `live.ts` keeps rejoining with the same token every 1-8 s, and
  at once on `online` / page visible. Final: we left, duplicate identity, removed, room deleted,
  token refused (the Talk screen then offers a fresh join).
- **Server**: a silent participant is dropped after about 21 s (measured). A room closes
  `departure_timeout` after the last participant left; agents do not keep it open. The dev server
  default is 20 s; RAUM's tokens set 120 s (`network.departure_timeout_s`) and `empty_timeout` 300 s.
- **Agent**: a phone that left stays a translation target for 120 s (`RAUM_REJOIN_GRACE_S`), so its
  language keeps being produced while it is away. If the room closes anyway, the next agent job gets
  the session's highest utterance number from core (`utt_seq` in the link state) and continues after
  it. Lost in that case: the pipeline's conversation memory (V-04) and live corrections (V-05).
- **Core**: the session never depends on LiveKit or the agent. Auto-burn (T-03, 30 min in
  Sprechzimmer) counts from the last conversation event; presence pings and reconnects do not
  extend it, so a 10 s drop is irrelevant and an abandoned room still forgets on time.
