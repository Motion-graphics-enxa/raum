// Live draft text (translation.draft, src/lib/draft.ts): replaced in place, newest wins, the final takes its place,
// and in simultaneous mode it follows the committed segments without showing words twice. `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";
import { shownTranslation } from "../src/lib/draft.ts";
import type { AnyEnvelope } from "../src/lib/events.ts";
import { RoomStore } from "../src/lib/store.ts";

let ts = 1000;
const env = (type: string, data: Record<string, unknown>, utt = "u-1") =>
  ({ v: 1, type, room: "sz-1", utt, ts: ts++, data }) as unknown as AnyEnvelope;
const draft = (seq: number, text: string, seg?: number) =>
  env("translation.draft", { target_lang: "ar", text, seq, ...(seg != null ? { seg } : {}) });
const turnOf = (s: RoomStore) => s.get().turns[0];

test("consecutive: the newest draft replaces the last in place, the final takes its place", () => {
  const s = new RoomStore("sz-1");
  s.push([env("utterance.start", { speaker: "a-1", lang: "de" }), draft(1, "مرحبًا"), draft(3, "مرحبًا سيدة دمير")]);
  s.push(draft(2, "مرحبًا سيدة")); // late: an older seq never replaces a newer draft
  assert.deepEqual(shownTranslation(turnOf(s), "ar"), { text: "", draft: "مرحبًا سيدة دمير" });
  s.push(env("translation.delta", { target_lang: "ar", delta: "مرحبًا", seq: 1 })); // the final streams: shorter
  assert.deepEqual(shownTranslation(turnOf(s), "ar"), { text: "", draft: "مرحبًا سيدة دمير" }); // never shrinks
  s.push(env("translation.final", { target_lang: "ar", text: "مرحبًا سيدة دمير." }));
  assert.deepEqual(shownTranslation(turnOf(s), "ar"), { text: "مرحبًا سيدة دمير.", draft: "" });
});

test("simultaneous: committed segments, then the draft of the tail; a segment at its place covers it", () => {
  const s = new RoomStore("sz-1");
  s.push([env("utterance.start", { speaker: "a-1", lang: "de" }), draft(1, "مرحبًا سيدة")]);
  s.push(env("translation.delta", { target_lang: "ar", delta: "مرحبًا سيدة دمير.", seq: 1, seg: 0 }));
  // a draft from before the first commit: the segment says it now
  assert.deepEqual(shownTranslation(turnOf(s), "ar"), { text: "مرحبًا سيدة دمير.", draft: "" });
  s.push(draft(2, "أنا الدكتور", 1));
  assert.deepEqual(shownTranslation(turnOf(s), "ar"), { text: "مرحبًا سيدة دمير.", draft: "أنا الدكتور" });
  s.push(draft(3, "ما الذي", 2)); // the tail after a segment that has not started yet: hidden, no gap
  assert.equal(shownTranslation(turnOf(s), "ar").draft, "");
  s.push(env("translation.delta", { target_lang: "ar", delta: "أنا الدكتور فيبر.", seq: 1, seg: 1 }));
  assert.deepEqual(shownTranslation(turnOf(s), "ar"), { text: "مرحبًا سيدة دمير. أنا الدكتور فيبر.", draft: "ما الذي" });
  s.push(env("translation.final", { target_lang: "ar", text: "ما الذي أتى بكِ إليّ اليوم؟", seg: 2, segs: 3 }));
  assert.equal(shownTranslation(turnOf(s), "ar").draft, ""); // the tail segment covers it
});

test("a turn asked to be said again, or merged into the next, shows no draft", () => {
  const s = new RoomStore("sz-1");
  s.push([env("utterance.start", { speaker: "a-1", lang: "de" }), draft(1, "مرحبًا")]);
  s.push(env("utterance.continued", { speaker: "a-1", into: "u-2" }));
  assert.equal(shownTranslation(turnOf(s), "ar").draft, "");
});
