// The events link's reconnect state machine, with a fake clock and fake sockets: `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  BEAT_MS,
  OVERLAP_MS,
  STALE_MS,
  backoffMs,
  createEventLink,
  type LinkState,
  type SocketLike,
} from "../src/lib/link.ts";

class FakeSocket implements SocketLike {
  readyState = 0;
  sent: string[] = [];
  closed = false;
  url: string;
  onopen: ((ev: never) => unknown) | null = null;
  onmessage: ((ev: { data: unknown }) => unknown) | null = null;
  onclose: ((ev: never) => unknown) | null = null;
  onerror: ((ev: never) => unknown) | null = null;
  constructor(url: string) {
    this.url = url;
  }
  send(d: string) {
    this.sent.push(d);
  }
  close() {
    this.closed = true;
    this.readyState = 3;
  }
  // server side
  accept() {
    this.readyState = 1;
    this.onopen?.({});
  }
  recv(data: string) {
    this.onmessage?.({ data });
  }
  reset() {
    this.readyState = 3;
    this.onerror?.({});
    this.onclose?.({});
  }
}

function harness() {
  let now = 1_000_000;
  let nextId = 1;
  const timers = new Map<number, { at: number; fn: () => void; every?: number }>();
  const sockets: FakeSocket[] = [];
  const states: LinkState[] = [];
  const got: string[] = [];
  const link = createEventLink({
    url: (since) => (since ? `wss://box/api/events/gs-1?since=${since}` : "wss://box/api/events/gs-1"),
    open: (u) => {
      const s = new FakeSocket(u);
      sockets.push(s);
      return s;
    },
    onMessage: (data) => {
      got.push(data);
      const e = JSON.parse(data) as { type: string; ts: number };
      return e.type === "privacy.meter" ? null : e.ts;
    },
    onState: (s) => states.push(s),
    now: () => now,
    setTimeout: (fn, ms) => {
      timers.set(nextId, { at: now + ms, fn });
      return nextId++;
    },
    clearTimeout: (id) => void timers.delete(id),
    setInterval: (fn, ms) => {
      timers.set(nextId, { at: now + ms, fn, every: ms });
      return nextId++;
    },
    clearInterval: (id) => void timers.delete(id),
  });
  /** Advance the clock, firing due timers in order. */
  const advance = (ms: number) => {
    const end = now + ms;
    for (;;) {
      let due: [number, { at: number; fn: () => void; every?: number }] | null = null;
      for (const kv of timers) if (kv[1].at <= end && (!due || kv[1].at < due[1].at)) due = kv;
      if (!due) break;
      const [id, t] = due;
      now = t.at;
      if (t.every) t.at += t.every;
      else timers.delete(id);
      t.fn();
    }
    now = end;
  };
  const last = () => sockets[sockets.length - 1];
  return { link, sockets, states, got, advance, last, ev: (type: string, ts: number) => JSON.stringify({ type, ts }) };
}

test("backoff is fast first and capped at 5 s", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 9].map(backoffMs), [500, 1000, 2000, 4000, 5000, 5000]);
});

test("a dropped socket reconnects with backoff and resyncs with ?since=", () => {
  const h = harness();
  h.link.start();
  assert.equal(h.sockets.length, 1);
  assert.equal(h.last().url, "wss://box/api/events/gs-1"); // first connect: the whole backlog
  h.last().accept();
  assert.equal(h.states.at(-1), "connected");
  h.last().recv(h.ev("transcript.final", 50_000));
  h.last().recv(h.ev("privacy.meter", 99_000)); // ephemeral: never moves the cursor

  h.last().reset(); // ECONNRESET
  assert.equal(h.states.at(-1), "reconnecting");
  h.advance(499);
  assert.equal(h.sockets.length, 1);
  h.advance(1);
  assert.equal(h.sockets.length, 2);
  assert.equal(h.last().url, `wss://box/api/events/gs-1?since=${50_000 - OVERLAP_MS}`);

  // still failing: 1 s, then 2 s
  h.last().reset();
  h.advance(999);
  assert.equal(h.sockets.length, 2);
  h.advance(1);
  assert.equal(h.sockets.length, 3);
  h.last().reset();
  h.advance(2000);
  assert.equal(h.sockets.length, 4);

  h.last().accept(); // back: the backoff starts over
  assert.equal(h.states.at(-1), "connected");
  h.last().reset();
  h.advance(500);
  assert.equal(h.sockets.length, 5);
});

test("an open socket that goes silent is replaced (heartbeat), a live one is pinged", () => {
  const h = harness();
  h.link.start();
  h.last().accept();
  h.advance(BEAT_MS);
  assert.deepEqual(h.last().sent, ["ping"]);
  h.last().recv("pong"); // a pong keeps it alive, never reaches the store
  assert.deepEqual(h.got, []);
  h.advance(STALE_MS - BEAT_MS); // quiet since the pong, but not long enough
  assert.equal(h.sockets.length, 1);
  h.advance(STALE_MS); // nothing at all, readyState still OPEN: dead
  assert.equal(h.sockets.length, 2);
  assert.ok(h.sockets[0].closed);
  assert.equal(h.states.at(-1), "reconnecting");
});

test("wake: a quiet socket is replaced at once, a fresh one only pinged", () => {
  const h = harness();
  h.link.start();
  h.last().accept();
  h.last().recv(h.ev("transcript.final", 10_000));
  h.advance(1000);
  h.link.wake(); // just heard from core: keep it
  assert.equal(h.sockets.length, 1);
  assert.deepEqual(h.last().sent, ["ping"]);

  h.advance(7000); // back from the background, nothing for 7 s (core ticks every second)
  h.link.wake();
  h.advance(0);
  assert.equal(h.sockets.length, 2);
  assert.ok(h.sockets[0].closed);
  assert.match(h.last().url, /since=7000$/);
});

test("wake during a backoff tries now; a stale socket's late events are ignored", () => {
  const h = harness();
  h.link.start();
  const first = h.last();
  first.accept();
  first.reset();
  h.last(); // waiting 500 ms
  h.link.wake();
  h.advance(0);
  assert.equal(h.sockets.length, 2);
  first.recv(h.ev("transcript.final", 1)); // the old socket's handlers are gone
  assert.deepEqual(h.got, []);
});

test("stop closes the socket and schedules nothing", () => {
  const h = harness();
  h.link.start();
  h.last().accept();
  h.link.stop();
  assert.ok(h.last().closed);
  h.advance(60_000);
  assert.equal(h.sockets.length, 1);
});
