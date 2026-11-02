"""RAUM test bot: a fake phone in a LiveKit room (end-to-end tests now, the load test later).

    cd core && uv run python ../scripts/bot.py --room sz-4821 --role a --lang de --wav ../recordings/de.wav
    cd core && uv run python ../scripts/bot.py --room sz-4821 --role b --lang ar --listen

Mints its own token (livekit-api; dev keys devkey/secret by default), joins with participant
metadata ``{"role","lang","device","path"}`` like the app, and

  --wav FILE      publishes FILE as its microphone in real time (silence in between, so the VAD
                  can end each utterance); --repeat N times, one start every --every seconds
  --listen        prints every raum.events envelope with a timestamp

At the end it prints a summary: events by type and pipeline.timing medians.
Make test speech (macOS; keep audio out of git, recordings/ is ignored):

    say -v Anna -o x.aiff "Ich nehme zweimal täglich 500 mg Metformin." && \\
        afconvert -f WAVE -d LEI16@48000 x.aiff x.wav
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import signal
import statistics
import string
import sys
import time
import warnings
import wave
from collections import Counter, defaultdict

import numpy as np
from livekit import api, rtc

warnings.filterwarnings("ignore", message="The HMAC key is")  # dev keys (devkey/secret) are short on purpose

EVENTS_TOPIC = "raum.events"
RATE = 48000
FRAME_MS = 10


def mint_token(args: argparse.Namespace, identity: str) -> str:
    meta = {"role": args.role, "lang": args.lang, "device": args.device, "path": "direct"}
    tok = (api.AccessToken(args.api_key, args.api_secret)
           .with_identity(identity)
           .with_name(args.name or identity)
           .with_metadata(json.dumps(meta))
           .with_grants(api.VideoGrants(room_join=True, room=args.room, can_publish=True, can_subscribe=True,
                                        can_publish_data=True)))
    if args.dispatch:
        tok = tok.with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=args.dispatch)]))
    return tok.to_jwt()


def read_wav(path: str) -> np.ndarray:
    """Mono int16 at 48 kHz."""
    with wave.open(path, "rb") as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width != 2:
        raise SystemExit(f"{path}: need 16-bit PCM (got {8 * width}-bit)")
    a = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch).mean(axis=1)
    if rate != RATE:
        import soxr

        a = soxr.resample(a.astype(np.float32), rate, RATE)
    return np.clip(a, -32768, 32767).astype(np.int16)


class Bot:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        self.identity = args.identity or f"{args.role}-{suffix}"
        self.room = rtc.Room()
        self.t0 = time.monotonic()
        self.counts: Counter[str] = Counter()
        self.timings: dict[str, list[float]] = defaultdict(list)
        self.timing_rows: list[dict] = []
        self.done = asyncio.Event()

    def stamp(self) -> str:
        return f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} +{time.monotonic() - self.t0:7.3f}s"

    def on_data(self, packet: rtc.DataPacket) -> None:
        if packet.topic != EVENTS_TOPIC:
            return
        try:
            env = json.loads(packet.data)
        except ValueError:
            return
        etype = env.get("type", "?")
        self.counts[etype] += 1
        data = env.get("data", {})
        if etype == "pipeline.timing":
            row = {k: data.get(k) for k in ("vad_ms", "asr_ms", "mt_first_ms", "mt_full_ms", "tts_first_ms",
                                            "check_ms", "target_lang")}
            row["utt"] = env.get("utt")
            self.timing_rows.append(row)
            for k, v in row.items():
                if isinstance(v, (int, float)):
                    self.timings[k].append(float(v))
        if self.args.json:
            print(json.dumps(env, ensure_ascii=False), flush=True)
        elif self.args.listen:
            lag = int(time.time() * 1000) - int(env.get("ts", 0))
            shown = {k: v for k, v in data.items() if k != "models" and v not in (None, [], {})}
            text = json.dumps(shown, ensure_ascii=False)
            if len(text) > 160 and not self.args.full:
                text = text[:157] + "..."
            print(f"{self.stamp()} {env.get('utt') or '-':7} {etype:19} lag={lag:4d}ms {text}", flush=True)

    async def run(self) -> None:
        a = self.args
        self.room.on("data_received", self.on_data)
        self.room.on("disconnected", lambda *_: self.done.set())
        await self.room.connect(a.url, a.token or mint_token(a, self.identity))
        self.identity = self.room.local_participant.identity
        print(f"{self.stamp()} joined {a.room} as {self.identity} (role {a.role}, lang {a.lang})", flush=True)
        tasks = []
        if a.wav:
            tasks.append(asyncio.create_task(self.speak(read_wav(a.wav))))
        if a.duration:
            tasks.append(asyncio.create_task(self._stop_after(a.duration)))
        await self.done.wait()
        for t in tasks:
            t.cancel()
        await self.room.disconnect()
        self.summary()

    async def _stop_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self.done.set()

    async def speak(self, pcm: np.ndarray) -> None:
        a = self.args
        source = rtc.AudioSource(RATE, 1, queue_size_ms=200)
        track = rtc.LocalAudioTrack.create_audio_track("mic", source)
        await self.room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        step = RATE * FRAME_MS // 1000
        silence = np.zeros(step, dtype=np.int16)
        await asyncio.sleep(a.delay)
        speech_s = len(pcm) / RATE
        t_next = time.monotonic()

        async def send(chunk: np.ndarray) -> None:
            nonlocal t_next
            if len(chunk) < step:
                chunk = np.concatenate([chunk, silence[: step - len(chunk)]])
            await source.capture_frame(rtc.AudioFrame(data=chunk.tobytes(), sample_rate=RATE, num_channels=1,
                                                      samples_per_channel=step))
            t_next += FRAME_MS / 1000
            delay = t_next - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

        for i in range(a.repeat):
            start = time.monotonic()
            print(f"{self.stamp()} speaking {i + 1}/{a.repeat} ({speech_s:.1f} s)", flush=True)
            for j in range(0, len(pcm), step):
                await send(pcm[j:j + step])
            # silence until the next start (VAD needs it to end the utterance)
            gap = max(1.5, a.every - (time.monotonic() - start)) if i < a.repeat - 1 else a.tail
            end = time.monotonic() + gap
            while time.monotonic() < end:
                await send(silence)
        if not a.listen and not a.duration:
            self.done.set()

    def summary(self) -> None:
        print(f"\n{self.identity}: events received: " + ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items())))
        if self.timings:
            print("pipeline.timing (ms)   n    p50     p95     min     max")
            for k in ("vad_ms", "asr_ms", "mt_first_ms", "mt_full_ms", "tts_first_ms", "check_ms"):
                v = sorted(self.timings.get(k, []))
                if not v:
                    continue
                p95 = v[min(len(v) - 1, round(0.95 * (len(v) - 1)))]
                print(f"  {k:18} {len(v):3d} {statistics.median(v):7.0f} {p95:7.0f} {v[0]:7.0f} {v[-1]:7.0f}")
        if self.args.csv and self.timing_rows:
            import csv

            with open(self.args.csv, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(self.timing_rows[0]))
                w.writeheader()
                w.writerows(self.timing_rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--room", required=True, help="e.g. sz-4821")
    ap.add_argument("--role", choices=["a", "b", "observer"], required=True)
    ap.add_argument("--lang", required=True, help="e.g. de, ar, tr")
    ap.add_argument("--wav", help="16-bit PCM wav to publish as the microphone")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--every", type=float, default=12.0, help="seconds between utterance starts")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds before the first utterance")
    ap.add_argument("--tail", type=float, default=4.0, help="silence after the last utterance")
    ap.add_argument("--listen", action="store_true", help="print received raum.events")
    ap.add_argument("--json", action="store_true", help="print raw envelopes (one JSON per line)")
    ap.add_argument("--full", action="store_true", help="do not shorten printed events")
    ap.add_argument("--duration", type=float, help="leave after N seconds")
    ap.add_argument("--csv", help="write pipeline.timing rows to this CSV")
    ap.add_argument("--identity")
    ap.add_argument("--token", help="join with this token (e.g. from POST /api/join) instead of minting one")
    ap.add_argument("--name")
    ap.add_argument("--device", default="bot")
    ap.add_argument("--dispatch", help="explicitly dispatch this agent name (worker run with --agent-name)")
    ap.add_argument("--url", default=os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"))
    ap.add_argument("--api-key", default=os.environ.get("LIVEKIT_API_KEY", "devkey"))
    ap.add_argument("--api-secret", default=os.environ.get("LIVEKIT_API_SECRET", "secret"))
    args = ap.parse_args()
    if not args.wav:
        args.listen = args.listen or not args.json

    async def amain() -> None:
        bot = Bot(args)  # rtc.Room binds to the running loop: create it inside
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, bot.done.set)
        await bot.run()

    asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
