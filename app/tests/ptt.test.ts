// Push-to-talk state machine (src/lib/ptt.ts): pointer, keyboard, cancel and the tail. `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  PTT_TAIL_MS,
  createPtt,
  deviceHandsFree,
  isTypingTarget,
  keyAction,
  rememberHandsFree,
  withinSlop,
  type PttMessage,
  type PttPhase,
} from "../src/lib/ptt.ts";

/** Fake clock + timers; records every mic change, message and phase. */
function rig() {
  let now = 1000;
  let seq = 0;
  const timers = new Map<number, { at: number; fn: () => void }>();
  const mic: boolean[] = [];
  const sent: PttMessage[] = [];
  const phases: PttPhase[] = [];
  const ptt = createPtt({
    setMic: (on) => mic.push(on),
    send: (m) => sent.push(m),
    now: () => now,
    setTimeout: (fn, ms) => {
      timers.set(++seq, { at: now + ms, fn });
      return seq;
    },
    clearTimeout: (id) => void timers.delete(id as number),
    onPhase: (p) => phases.push(p),
  });
  const advance = (ms: number) => {
    now += ms;
    for (const [id, t] of [...timers].sort((a, b) => a[1].at - b[1].at)) {
      if (t.at <= now) {
        timers.delete(id);
        t.fn();
      }
    }
  };
  return { ptt, mic, sent, phases, advance, timers };
}

test("pointer: press opens the mic and tells the agent; release ends the turn, the mic closes after the tail", () => {
  const r = rig();
  assert.equal(r.ptt.press("pointer:1"), true);
  assert.deepEqual(r.mic, [true]);
  assert.deepEqual(r.sent, [{ type: "ptt.press", at: 1000 }]);
  r.advance(2000);
  assert.equal(r.ptt.release("pointer:1"), true);
  // the release goes out at once (the agent waits the tail itself), the mic stays open for it
  assert.deepEqual(r.sent[1], { type: "ptt.release", at: 3000, tail_ms: PTT_TAIL_MS });
  assert.equal(r.ptt.phase, "tail");
  assert.deepEqual(r.mic, [true]);
  r.advance(PTT_TAIL_MS - 1);
  assert.deepEqual(r.mic, [true]); // the last syllable is still going out
  r.advance(1);
  assert.deepEqual(r.mic, [true, false]);
  assert.equal(r.ptt.phase, "idle");
  assert.deepEqual(r.phases, ["held", "tail", "idle"]);
});

test("pointercancel and leaving the button end the hold like a release", () => {
  const r = rig();
  r.ptt.press("pointer:7");
  r.ptt.cancel(); // pointercancel / lostpointercapture / window blur
  assert.equal(r.sent.at(-1)?.type, "ptt.release");
  r.advance(PTT_TAIL_MS);
  assert.deepEqual(r.mic, [true, false]);
  r.ptt.cancel(); // nothing held: nothing happens
  assert.equal(r.sent.length, 2);

  const rect = { left: 100, top: 100, right: 196, bottom: 196 };
  assert.equal(withinSlop(150, 150, rect), true);
  assert.equal(withinSlop(230, 150, rect), true); // a finger sliding a little: still holding
  assert.equal(withinSlop(260, 150, rect), false); // off the button: let go
  assert.equal(withinSlop(150, 40, rect), false);
});

test("pressed again within the tail: the mic never closes and the turn goes on", () => {
  const r = rig();
  r.ptt.press("pointer:1");
  r.ptt.release("pointer:1");
  r.advance(60);
  assert.equal(r.ptt.press("pointer:2"), true);
  r.advance(PTT_TAIL_MS * 3);
  assert.deepEqual(r.mic, [true]); // opened once, never closed
  assert.equal(r.ptt.phase, "held");
  assert.deepEqual(
    r.sent.map((m) => m.type),
    ["ptt.press", "ptt.release", "ptt.press"],
  );
  assert.equal(r.timers.size, 0);
});

test("one holder at a time: a second pointer or the key while held is ignored, so is a stray release", () => {
  const r = rig();
  r.ptt.press("pointer:1");
  assert.equal(r.ptt.press("key"), false);
  assert.equal(r.ptt.press("pointer:2"), false);
  assert.equal(r.ptt.release("pointer:2"), false); // not the holder
  assert.equal(r.ptt.release("key"), false);
  assert.equal(r.ptt.phase, "held");
  assert.equal(r.sent.length, 1);
  assert.equal(r.ptt.release("pointer:1"), true);
  assert.equal(r.ptt.release("pointer:1"), false); // a second pointerup
  assert.equal(r.sent.length, 2);
});

test("keyboard: Space holds, key repeat is swallowed, typing is left alone", () => {
  const r = rig();
  const body = { tagName: "BODY" };
  assert.equal(keyAction({ code: "Space", target: body }, true), "press");
  assert.equal(keyAction({ code: "Space", target: body, repeat: true }, true), "swallow");
  assert.equal(keyAction({ code: "Space", target: body }, false), "release");
  assert.equal(keyAction({ key: " ", target: body }, true), "press");
  assert.equal(keyAction({ code: "Enter", target: body }, true), null);
  assert.equal(keyAction({ code: "Space", target: body, metaKey: true }, true), null);
  assert.equal(keyAction({ code: "Space", target: { tagName: "INPUT", type: "text" } }, true), null);
  assert.equal(keyAction({ code: "Space", target: { tagName: "TEXTAREA" } }, true), null);
  assert.equal(keyAction({ code: "Space", target: { tagName: "DIV", isContentEditable: true } }, true), null);
  assert.equal(keyAction({ code: "Space", target: { tagName: "BUTTON" } }, true), "press");
  // up always lets go: the focus may have moved into a field while Space was down
  assert.equal(keyAction({ code: "Space", target: { tagName: "INPUT", type: "text" } }, false), "release");
  assert.equal(isTypingTarget({ tagName: "INPUT", type: "checkbox" }), false);
  assert.equal(isTypingTarget({ tagName: "INPUT" }), true);
  assert.equal(isTypingTarget(null), false);

  // the keydown / repeat / keyup sequence through the machine
  for (const [down, repeat] of [[true, false], [true, true], [true, true], [false, false]] as const) {
    const a = keyAction({ code: "Space", target: body, repeat }, down);
    if (a === "press") r.ptt.press("key");
    if (a === "release") r.ptt.release("key");
  }
  assert.deepEqual(
    r.sent.map((m) => m.type),
    ["ptt.press", "ptt.release"],
  );
  r.advance(PTT_TAIL_MS);
  assert.deepEqual(r.mic, [true, false]);
});

test("dispose while held closes the mic now, releases, and leaves no timer", () => {
  const r = rig();
  r.ptt.press("key");
  r.ptt.dispose();
  assert.deepEqual(r.mic, [true, false]);
  assert.deepEqual(r.sent.at(-1), { type: "ptt.release", at: 1000, tail_ms: 0 });
  assert.equal(r.timers.size, 0);
  assert.equal(r.ptt.phase, "idle");

  const t = rig(); // in the tail: the release went out already; just close
  t.ptt.press("key");
  t.ptt.release("key");
  t.ptt.dispose();
  assert.equal(t.sent.length, 2);
  assert.deepEqual(t.mic, [true, false]);
  assert.equal(t.timers.size, 0);
});

test("hands-free is remembered per device and never throws", () => {
  const box = new Map<string, string>();
  const storage = { getItem: (k: string) => box.get(k) ?? null, setItem: (k: string, v: string) => void box.set(k, v) };
  assert.equal(deviceHandsFree(storage), false); // push-to-talk is the default
  rememberHandsFree(true, storage);
  assert.equal(deviceHandsFree(storage), true);
  rememberHandsFree(false, storage);
  assert.equal(deviceHandsFree(storage), false);
  const blocked = {
    getItem: () => {
      throw new Error("SecurityError");
    },
    setItem: () => {
      throw new Error("QuotaExceededError");
    },
  };
  assert.equal(deviceHandsFree(blocked), false);
  rememberHandsFree(true, blocked);
  assert.equal(deviceHandsFree(null), false);
});
