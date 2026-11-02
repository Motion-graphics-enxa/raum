"""Listener bot that also records the voice tracks it receives (tts-*) to WAV, with frame arrival times.

    cd ~/Desktop/HackathonRaum/raum/core && uv run python /tmp/raum-gaps/rec.py --room sz-6001 --role b --lang en \
        --duration 30 --out /tmp/raum-gaps/run1
Writes run1.wav (received audio, 24 kHz mono, contiguous frames as delivered), run1.json (frame arrival times,
events with monotonic stamps, pipeline.timing in full).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import wave

import numpy as np

sys.path.insert(0, "/home/deeptranslate/Desktop/HackathonRaum/raum/scripts")
import bot as B  # noqa: E402
from livekit import rtc  # noqa: E402

OUT_RATE = 24000


class Rec(B.Bot):
    def __init__(self, args):
        super().__init__(args)
        self.frames: list[np.ndarray] = []
        self.arrivals: list[tuple[float, int]] = []  # (t since t0, samples)
        self.events: list[dict] = []
        self.track_names: list[str] = []
        self.tasks: list[asyncio.Task] = []

    def on_data(self, packet):
        if packet.topic == B.EVENTS_TOPIC:
            try:
                env = json.loads(packet.data)
                self.events.append({"t": time.monotonic() - self.t0, "type": env.get("type"), "utt": env.get("utt"),
                                    "data": {k: v for k, v in (env.get("data") or {}).items() if k != "models"}})
            except ValueError:
                pass
        super().on_data(packet)

    async def _record(self, track):
        stream = rtc.AudioStream(track, sample_rate=OUT_RATE, num_channels=1)
        async for ev in stream:
            f = ev.frame
            a = np.frombuffer(f.data, dtype=np.int16).copy()
            self.frames.append(a)
            self.arrivals.append((time.monotonic() - self.t0, len(a)))

    async def run(self):
        def on_sub(track, pub, participant):
            if track.kind == rtc.TrackKind.KIND_AUDIO and track.name.startswith("tts-"):
                self.track_names.append(f"{participant.identity}:{track.name}:{pub.sid}")
                print(f"{self.stamp()} recording {participant.identity} {track.name}", flush=True)
                self.tasks.append(asyncio.ensure_future(self._record(track)))
        self.room.on("track_subscribed", on_sub)
        await super().run()

    def summary(self):
        super().summary()
        out = self.args.out
        pcm = np.concatenate(self.frames) if self.frames else np.zeros(0, np.int16)
        with wave.open(out + ".wav", "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(OUT_RATE)
            w.writeframes(pcm.tobytes())
        with open(out + ".json", "w") as fh:
            json.dump({"tracks": self.track_names, "arrivals": self.arrivals, "events": self.events}, fh,
                      ensure_ascii=False)
        print(f"wrote {out}.wav ({len(pcm) / OUT_RATE:.1f} s) and {out}.json", flush=True)


def main():
    sys.argv, out = list(sys.argv), None
    if "--out" in sys.argv:
        i = sys.argv.index("--out")
        out = sys.argv[i + 1]
        del sys.argv[i:i + 2]
    ap_args = None
    # reuse bot's parser
    import argparse
    orig = argparse.ArgumentParser.parse_args

    def parse(self, *a, **k):
        ns = orig(self, *a, **k)
        ns.out = out or "/tmp/raum-gaps/rec"
        return ns
    argparse.ArgumentParser.parse_args = parse
    B.Bot = Rec  # bot.main() builds Bot(args)
    B.main()


if __name__ == "__main__":
    main()
