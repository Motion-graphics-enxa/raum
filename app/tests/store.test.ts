// RoomStore under a reconnect: the ?since= backlog overlaps what was already shown. `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";
import type { AnyEnvelope } from "../src/lib/events.ts";
import { RoomStore, otherLangOf } from "../src/lib/store.ts";

const env = (type: string, ts: number, data: Record<string, unknown>, utt: string | null = "u-1") =>
  ({ v: 1, type, room: "gs-1", utt, ts, data }) as unknown as AnyEnvelope;

const turn = [
  env("utterance.start", 1000, { speaker: "a-1", lang: "en" }),
  env("transcript.final", 2000, { speaker: "a-1", lang: "en", text: "Hello there", asr_conf: 0.9 }),
  env("translation.delta", 2100, { target_lang: "ru", delta: "Привет", seq: 0 }),
  env("translation.final", 2500, { target_lang: "ru", text: "Привет" }),
];

test("an overlapping backlog after a reconnect adds nothing twice and loses nothing", () => {
  const s = new RoomStore("gs-1");
  s.push(turn.slice(0, 3)); // live, then the link drops
  s.push(turn); // reconnect with ?since=: the overlap comes again, plus what was missed
  const m = s.get();
  assert.equal(m.turns.length, 1);
  assert.equal(m.turns[0].text, "Hello there");
  assert.deepEqual(m.turns[0].translations.ru, { text: "Привет", final: true });
  const v = m.version;
  s.push(turn); // the data channel delivers the same events again
  assert.equal(s.get().version, v);
});

test("the header pair uses the participants' real languages, then the turns, then the mode", () => {
  const s = new RoomStore("gs-1");
  s.push(turn);
  const turns = s.get().turns;
  const en = { id: "a-1", role: "a" as const, lang: "en", device: "phone", path: "direct" as const };
  assert.equal(otherLangOf([en], "b-1", "b", "ru", [], "de"), "en");
  assert.equal(otherLangOf([], "b-1", "b", "ru", turns, "de"), "en"); // nobody listed yet: what was said
  assert.equal(otherLangOf([], "b-1", "b", "ru", [], "de"), "de"); // nothing known: the mode default
  const both = [{ ...en, role: "b" as const }, { ...en, id: "b-2", role: "b" as const, lang: "ru" }];
  assert.equal(otherLangOf(both, "b-2", "b", "ru", [], "de"), "en"); // both scanned as "b": the other one
});
