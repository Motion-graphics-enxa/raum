# Browser end-to-end checks

Two headless Chrome "phones" with fake microphones (Chrome's `--use-file-for-fake-audio-capture`;
the audio-service sandbox must be off for it to read the file) join one room on a running stack.

```bash
scripts/dev.sh                        # in another terminal
cd scripts/e2e && npm install && npm run audio
node two-phones.mjs http://localhost:5180 40 sprechzimmer a   # doctor speaks German, patient (ar) reads
node replay-shots.mjs http://localhost:5180 light             # scripted conversation via the agent link, every screen
```

Screenshots and logs go to `eval/out/e2e/` (gitignored). Needs Google Chrome in /Applications.
