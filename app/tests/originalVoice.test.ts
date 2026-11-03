// Original voice on a call (src/lib/originalVoice.ts): which track, when, how loud, ducking. `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  DUCK_LEVEL,
  ORIGINAL_VOLUME,
  RAMP_MS,
  callOn,
  createOriginalVoice,
  duckedVolume,
  isOtherVoice,
  loadOriginalVoice,
  originalVoiceOn,
  saveOriginalVoice,
} from "../src/lib/originalVoice.ts";

test("the reference's level: the slider, capped at 0.08 while the translation speaks", () => {
  assert.equal(duckedVolume(0.35, false), 0.35);
  assert.equal(duckedVolume(0.35, true), DUCK_LEVEL);
  assert.equal(duckedVolume(0.05, true), 0.05); // never louder for ducking
  assert.equal(duckedVolume(3, false), 1);
  assert.equal(duckedVolume(Number.NaN, false), ORIGINAL_VOLUME);
});

test("a call: auto through the tunnel, never in the same room unless asked", () => {
  assert.equal(callOn("auto", ["direct", "direct"]), false);
  assert.equal(callOn("auto", ["direct", "tunnel"]), true);
  assert.equal(callOn("off", ["tunnel"]), false);
  assert.equal(callOn("on", ["direct"]), true);
  assert.equal(originalVoiceOn({ call: "on", volume: 0 }, []), false); // slider at zero: off, the box is told
  assert.equal(originalVoiceOn({ call: "auto", volume: 0.35 }, ["tunnel"]), true);
});

test("remembered per device, defaults on bad data", () => {
  const box = new Map<string, string>();
  const st = { getItem: (k: string) => box.get(k) ?? null, setItem: (k: string, v: string) => void box.set(k, v) };
  assert.deepEqual(loadOriginalVoice(st), { call: "auto", volume: ORIGINAL_VOLUME });
  saveOriginalVoice({ call: "off", volume: 0.6 }, st);
  assert.deepEqual(loadOriginalVoice(st), { call: "off", volume: 0.6 });
  box.set("raum:original_voice", "{broken");
  assert.deepEqual(loadOriginalVoice(st), { call: "auto", volume: ORIGINAL_VOLUME });
  assert.deepEqual(loadOriginalVoice(null), { call: "auto", volume: ORIGINAL_VOLUME });
});

const mic = { kind: "audio", source: "microphone" };
const phone = (identity: string, meta: Record<string, string> = {}) => ({ identity, metadata: JSON.stringify(meta) });

test("only the other person's phone mic: not mine, not the agent, not the speakerphone or table", () => {
  assert.equal(isOtherVoice(phone("a-doc1", { role: "a" }), mic, "b-me"), true);
  assert.equal(isOtherVoice(phone("a-doc1"), mic, "b-me"), true); // role from the identity
  assert.equal(isOtherVoice(phone("b-me", { role: "b" }), mic, "b-me"), false);
  assert.equal(isOtherVoice(phone("raum-agent"), mic, "b-me"), false);
  assert.equal(isOtherVoice(phone("agent-AJ_x"), mic, "b-me"), false);
  assert.equal(isOtherVoice(phone("a-spk", { role: "a", device: "speakerphone", audio_out: "room" }), mic, "b-me"), false);
  assert.equal(isOtherVoice(phone("t-1", { role: "observer" }), mic, "b-me"), false);
  assert.equal(isOtherVoice(phone("a-doc1", { role: "a" }), { kind: "audio", source: "unknown" }, "b-me"), false);
  assert.equal(isOtherVoice(phone("a-doc1", { role: "a" }), { kind: "video", source: "microphone" }, "b-me"), false);
});

/** A fake LiveKit room: participants with publications, events, tracks that record their volume. */
function rig(opts: { webAudio?: boolean } = {}) {
  const handlers = new Map<string, Set<(...a: unknown[]) => void>>();
  const subscribed = new Map<string, boolean>();
  const pubs: Record<string, { trackSid: string; kind: string; source: string; setSubscribed: (on: boolean) => void }> = {};
  const pub = (sid: string, source = "microphone") =>
    (pubs[sid] = { trackSid: sid, kind: "audio", source, setSubscribed: (on: boolean) => void subscribed.set(sid, on) });
  const doctor = { ...phone("a-doc1", { role: "a" }), trackPublications: new Map([["TR_d", pub("TR_d")]]) };
  const agent = { ...phone("raum-agent"), trackPublications: new Map([["TR_v", pub("TR_v", "unknown")]]) };
  const room = {
    remoteParticipants: new Map<string, unknown>([
      [doctor.identity, doctor],
      [agent.identity, agent],
    ]),
    on: (ev: string, fn: (...a: unknown[]) => void) => {
      if (!handlers.has(ev)) handlers.set(ev, new Set());
      handlers.get(ev)!.add(fn);
      return room;
    },
    off: (ev: string, fn: (...a: unknown[]) => void) => {
      handlers.get(ev)?.delete(fn);
      return room;
    },
  };
  const emit = (ev: string, ...a: unknown[]) => handlers.get(ev)?.forEach((fn) => fn(...a));
  const attached: { dataset: Record<string, string>; removed: boolean; remove: () => void }[] = [];
  const track = (sid: string) => {
    const t = {
      sid,
      volumes: [] as number[],
      ctx: null as unknown,
      setVolume: (v: number) => void t.volumes.push(v),
      setAudioContext: (c: unknown) => void (t.ctx = c),
      attach: () => {
        const el = { dataset: {} as Record<string, string>, removed: false, remove: () => void (el.removed = true) };
        attached.push(el);
        return el;
      },
      detach: () => attached.filter((e) => !e.removed),
    };
    return t;
  };
  let clock = 0;
  const frames: ((t: number) => void)[] = [];
  const doc = { body: { appendChild: () => undefined }, addEventListener: () => undefined, removeEventListener: () => undefined };
  const ov = createOriginalVoice(room as never, "b-me", {
    doc: doc as never,
    webAudio: opts.webAudio ?? false,
    raf: (fn) => frames.push(fn),
    now: () => clock,
  });
  const run = (ms: number) => {
    clock += ms;
    const now = frames.splice(0);
    now.forEach((f) => f(clock));
  };
  const settle = () => {
    for (let i = 0; i < 40 && frames.length; i++) run(16);
  };
  return { room, emit, subscribed, pubs, doctor, agent, track, attached, ov, settle };
}

test("subscribes to the other person's mic only while on, plays it at the slider's level", () => {
  const r = rig();
  r.ov.set({ enabled: false, volume: 0.35, ducked: false });
  assert.equal(r.subscribed.size, 0); // the same room: nothing subscribed
  r.ov.set({ enabled: true, volume: 0.35, ducked: false });
  assert.deepEqual([...r.subscribed], [["TR_d", true]]); // the doctor's mic, not the agent's voice
  const t = r.track("TR_d");
  r.emit("trackSubscribed", t, r.pubs.TR_d, r.doctor);
  assert.equal(r.ov.playing(), 1);
  assert.equal(r.attached[0].dataset.raumOriginal, "1");
  assert.equal(t.volumes.at(-1), 0.35);
  r.ov.set({ enabled: false, volume: 0.35, ducked: false });
  assert.equal(r.subscribed.get("TR_d"), false);
  r.emit("trackUnsubscribed", t, r.pubs.TR_d);
  assert.equal(r.ov.playing(), 0);
  assert.ok(r.attached.every((e) => e.removed));
});

test("ducks to 0.08 under the translation in a short ramp, and comes back", () => {
  const r = rig();
  r.ov.set({ enabled: true, volume: 0.35, ducked: false });
  const t = r.track("TR_d");
  r.emit("trackSubscribed", t, r.pubs.TR_d, r.doctor);
  r.ov.set({ enabled: true, volume: 0.35, ducked: true });
  r.settle();
  assert.equal(t.volumes.at(-1), DUCK_LEVEL);
  assert.ok(t.volumes.length > 3, "stepped, not a jump"); // over RAMP_MS
  assert.ok(t.volumes.slice(1).every((v, i, a) => i === 0 || v <= a[i - 1] + 1e-9), "monotonic down");
  r.ov.set({ enabled: true, volume: 0.35, ducked: false });
  r.settle();
  assert.equal(t.volumes.at(-1), 0.35);
  assert.ok(RAMP_MS <= 200);
});

test("iOS path: Web Audio gain set before attach (the element itself stays muted)", () => {
  const r = rig({ webAudio: true });
  const created: unknown[] = [];
  const g = globalThis as unknown as { AudioContext?: unknown };
  const saved = g.AudioContext;
  g.AudioContext = class {
    state = "suspended";
    constructor() {
      created.push(this);
    }
    resume() {
      return Promise.resolve();
    }
    close() {
      return Promise.resolve();
    }
  };
  try {
    r.ov.set({ enabled: true, volume: 0.35, ducked: false });
    const t = r.track("TR_d");
    r.emit("trackSubscribed", t, r.pubs.TR_d, r.doctor);
    assert.equal(created.length, 1);
    assert.equal(t.ctx, created[0]);
    r.ov.set({ enabled: true, volume: 0.35, ducked: true });
    assert.equal(t.volumes.at(-1), DUCK_LEVEL); // the gain node ramps it (setTargetAtTime)
    r.ov.dispose();
  } finally {
    g.AudioContext = saved;
  }
});

test("a late publication and a reconnect are picked up; a stray subscription while off is dropped", () => {
  const r = rig();
  r.ov.set({ enabled: true, volume: 0.35, ducked: false });
  r.subscribed.clear();
  r.emit("reconnected");
  assert.equal(r.subscribed.get("TR_d"), true);
  const late = { trackSid: "TR_d2", kind: "audio", source: "microphone", setSubscribed: (on: boolean) => void r.subscribed.set("TR_d2", on) };
  r.emit("trackPublished", late, r.doctor);
  assert.equal(r.subscribed.get("TR_d2"), true);
  r.ov.set({ enabled: false, volume: 0.35, ducked: false });
  r.emit("trackSubscribed", r.track("TR_d2"), late, r.doctor);
  assert.equal(r.subscribed.get("TR_d2"), false);
  assert.equal(r.ov.playing(), 0);
  r.ov.dispose();
});
