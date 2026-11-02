/**
 * Conversation model built from the §6.5 event stream. One store per room view.
 * Pure reducer + tiny external store for useSyncExternalStore.
 */
import { useSyncExternalStore } from "react";
import type { AnyEnvelope, Badge, EventData, FactCheck, FactStatus, ParticipantInfo, Role } from "./events";

export type CheckState = "translating" | "pending" | Badge | "uncertain";

export interface Translation {
  text: string;
  final: boolean;
}

export interface Turn {
  utt: string;
  speaker: string;
  role: Role | null;
  lang: string;
  startedAt: number;
  endedAt: number | null;
  partial: string;
  partialSeq: number;
  text: string | null; // transcript.final
  asrConf: number | null;
  translations: Record<string, Translation>;
  facts: Record<string, FactCheck[]>;
  factsRed: Record<string, boolean>;
  verify: Record<string, EventData["verify.result"]>;
  timing: Record<string, EventData["pipeline.timing"]>;
  flags: EventData["flag"][];
  confirms: Record<string, EventData["confirm"]>;
  uncertain: boolean;
  repeatBack?: EventData["repeat_back.result"];
  explainDraft?: EventData["explain.draft"];
  explainSent?: EventData["explain.sent"];
  corrected?: Record<string, string>; // lang -> previous text (struck and replaced)
  ignored?: EventData["voice.lock"]; // F-10: not the device owner's voice, dropped by the box
  continuedIn?: string; // a sentence cut at a pause: it goes on in this utterance (voice stopped, no check)
  echo?: Record<string, EventData["voice.echo"][]>; // lang -> figures the voice said otherwise than written
  // simultaneous mode: a translation arrives in segments (seg 0, 1, ...); the last final carries segs
  segs?: Record<string, { parts: Record<number, Translation>; total: number | null }>;
  segFacts?: Record<string, Record<number, FactCheck[]>>;
}

export interface Offer extends Omit<EventData["offer.update"], "ts"> {
  ts: number;
  utt: string | null;
}

export interface RoomModel {
  room: string;
  turns: Turn[];
  byUtt: Record<string, number>;
  participants: Record<string, ParticipantInfo>;
  session: EventData["session.state"] | null;
  privacy: EventData["privacy.meter"] | null;
  gpu: EventData["gpu.stats"] | null;
  summary: EventData["summary.ready"] | null;
  burn: EventData["burn.done"] | null;
  offers: Offer[];
  documents: (EventData["document.ready"] & { ts: number })[]; // F-07, in arrival order
  speaking: Record<string, number>; // speaker -> utterance.start ts (cleared on end)
  voicePaused: string[]; // identities whose mic the box paused while its voice plays (C-14)
  lastTs: number;
  version: number;
}

export function emptyModel(room: string): RoomModel {
  return {
    room,
    turns: [],
    byUtt: {},
    participants: {},
    session: null,
    privacy: null,
    gpu: null,
    summary: null,
    burn: null,
    offers: [],
    documents: [],
    speaking: {},
    voicePaused: [],
    lastTs: 0,
    version: 0,
  };
}

function newTurn(utt: string, speaker: string, lang: string, ts: number, role: Role | null): Turn {
  return {
    utt,
    speaker,
    role,
    lang,
    startedAt: ts,
    endedAt: null,
    partial: "",
    partialSeq: -1,
    text: null,
    asrConf: null,
    translations: {},
    facts: {},
    factsRed: {},
    verify: {},
    timing: {},
    flags: [],
    confirms: {},
    uncertain: false,
  };
}

function roleOf(m: RoomModel, speaker: string): Role | null {
  const p = m.participants[speaker];
  if (p) return p.role;
  const r = speaker.split("-")[0];
  return r === "a" || r === "b" ? r : null;
}

/** Mutating reducer on a shallow-copied model (cheap; turns are replaced on change). */
export function apply(prev: RoomModel, e: AnyEnvelope): RoomModel {
  const m: RoomModel = { ...prev, version: prev.version + 1, lastTs: Math.max(prev.lastTs, e.ts) };
  const utt = e.utt;

  const touch = (fn: (t: Turn) => Turn | void, create?: { speaker: string; lang: string }): void => {
    if (!utt) return;
    let idx = m.byUtt[utt];
    if (idx === undefined) {
      if (!create) {
        // event for an unknown utterance (joined late): create a placeholder
        create = { speaker: "", lang: "" };
      }
      const t0 = newTurn(utt, create.speaker, create.lang, e.ts, roleOf(m, create.speaker));
      m.turns = [...m.turns, t0];
      m.byUtt = { ...m.byUtt, [utt]: m.turns.length - 1 };
      idx = m.turns.length - 1;
    }
    const cur = { ...m.turns[idx] };
    const out = fn(cur) || cur;
    const turns = m.turns.slice();
    turns[idx] = out;
    m.turns = turns;
  };

  switch (e.type) {
    case "participant.joined": {
      m.participants = { ...m.participants, [e.data.id]: e.data };
      break;
    }
    case "participant.left": {
      const p = { ...m.participants };
      delete p[e.data.id];
      m.participants = p;
      break;
    }
    case "utterance.start": {
      m.speaking = { ...m.speaking, [e.data.speaker]: e.ts };
      touch((t) => {
        t.speaker = e.data.speaker;
        t.lang = e.data.lang;
        t.role = roleOf(m, e.data.speaker);
        t.startedAt = Math.min(t.startedAt || e.ts, e.ts); // a merged utterance starts again: keep its place
        t.endedAt = null;
      }, { speaker: e.data.speaker, lang: e.data.lang });
      break;
    }
    case "utterance.end": {
      const s = { ...m.speaking };
      delete s[e.data.speaker];
      m.speaking = s;
      touch((t) => {
        t.endedAt = e.ts;
        if (!t.speaker) t.speaker = e.data.speaker;
      }, { speaker: e.data.speaker, lang: e.data.lang });
      break;
    }
    case "utterance.continued":
      touch((t) => {
        t.continuedIn = e.data.into;
        t.endedAt = t.endedAt ?? e.ts; // it may have been stopped before its utterance.end
        if (!t.speaker) t.speaker = e.data.speaker;
      }, { speaker: e.data.speaker, lang: "" });
      if (utt) m.offers = m.offers.filter((o) => o.utt !== utt); // the merged sentence states them again
      break;
    case "transcript.partial":
      touch((t) => {
        if (e.data.seq >= t.partialSeq) {
          t.partial = e.data.text;
          t.partialSeq = e.data.seq;
        }
        if (!t.speaker) t.speaker = e.data.speaker;
        if (!t.lang) t.lang = e.data.lang;
        t.role = t.role ?? roleOf(m, e.data.speaker);
      }, { speaker: e.data.speaker, lang: e.data.lang });
      break;
    case "transcript.final":
      touch((t) => {
        t.text = e.data.text;
        t.asrConf = e.data.asr_conf;
        t.speaker = e.data.speaker;
        t.lang = e.data.lang;
        t.role = t.role ?? roleOf(m, e.data.speaker);
      }, { speaker: e.data.speaker, lang: e.data.lang });
      break;
    case "translation.delta":
      touch((t) => {
        const lang = e.data.target_lang;
        if (e.data.seg != null) {
          const sg = segState(t, lang);
          const part = sg.parts[e.data.seg];
          if (part?.final) return;
          sg.parts = { ...sg.parts, [e.data.seg]: { text: (part?.text ?? "") + e.data.delta, final: false } };
          t.translations = { ...t.translations, [lang]: joinSegs(sg) };
          return;
        }
        const cur = t.translations[lang];
        if (cur?.final) return;
        t.translations = { ...t.translations, [lang]: { text: (cur?.text ?? "") + e.data.delta, final: false } };
      });
      break;
    case "translation.final":
      touch((t) => {
        const lang = e.data.target_lang;
        if (e.data.seg != null) {
          const sg = segState(t, lang);
          sg.parts = { ...sg.parts, [e.data.seg]: { text: e.data.text, final: true } }; // may repeat: keyed by seg
          if (e.data.segs != null) sg.total = e.data.segs;
          t.translations = { ...t.translations, [lang]: joinSegs(sg) };
          return;
        }
        // a final without seg replaces everything before it (also a merged utterance's re-final)
        if (t.segs?.[lang]) {
          const { [lang]: _drop, ...rest } = t.segs;
          t.segs = rest;
        }
        const prev = t.translations[lang];
        const extended = prev && e.data.text.startsWith(prev.text.trim()); // the speaker went on: not a correction
        if (prev?.final && prev.text !== e.data.text && !extended) {
          t.corrected = { ...(t.corrected ?? {}), [lang]: prev.text };
        }
        t.translations = { ...t.translations, [lang]: { text: e.data.text, final: true } };
      });
      break;
    case "verify.facts":
      touch((t) => {
        const lang = e.data.target_lang;
        const red = e.data.red ?? e.data.facts.some((f) => !f.match);
        if (e.data.seg != null) {
          const per = { ...(t.segFacts?.[lang] ?? {}), [e.data.seg]: e.data.facts };
          t.segFacts = { ...(t.segFacts ?? {}), [lang]: per };
          const order = Object.keys(per).map(Number).sort((a, b) => a - b);
          t.facts = { ...t.facts, [lang]: order.flatMap((k) => per[k]) };
          t.factsRed = { ...t.factsRed, [lang]: Boolean(t.factsRed[lang]) || red };
          return;
        }
        t.facts = { ...t.facts, [lang]: e.data.facts };
        t.factsRed = { ...t.factsRed, [lang]: red };
      });
      break;
    case "verify.result":
      touch((t) => {
        t.verify = { ...t.verify, [e.data.target_lang]: e.data };
      });
      break;
    case "pipeline.timing":
      touch((t) => {
        t.timing = { ...t.timing, [e.data.target_lang ?? "_"]: e.data };
      });
      break;
    case "flag":
      touch((t) => {
        const id = e.data.id ?? `${utt}-${e.data.kind}-${t.flags.length}`;
        if (t.flags.some((f) => (f.id ?? "") === id)) return;
        t.flags = [...t.flags, { ...e.data, id }];
        if (e.data.kind === "uncertain") t.uncertain = true;
      });
      break;
    case "confirm":
      if (utt) {
        touch((t) => {
          const key = e.data.flag_id ?? "_";
          t.confirms = { ...t.confirms, [key]: e.data };
        });
      } else {
        // confirm without utt: attach to the flag wherever it is
        const id = e.data.flag_id;
        if (id) {
          m.turns = m.turns.map((t) =>
            t.flags.some((f) => f.id === id) ? { ...t, confirms: { ...t.confirms, [id]: e.data } } : t,
          );
        }
      }
      break;
    case "repeat_back.result":
      touch((t) => {
        t.repeatBack = e.data;
      });
      break;
    case "explain.draft":
      touch((t) => {
        t.explainDraft = e.data;
      });
      break;
    case "explain.sent":
      touch((t) => {
        t.explainSent = e.data;
      });
      break;
    case "offer.update":
      m.offers = [...m.offers, { ...e.data, utt }];
      break;
    case "session.state": {
      m.session = e.data;
      const ps: Record<string, ParticipantInfo> = {};
      for (const p of e.data.participants) ps[p.id] = p;
      if (e.data.participants.length || !Object.keys(m.participants).length) m.participants = ps;
      const prevId = prev.session?.session_id ?? null;
      if (prevId && e.data.session_id && e.data.session_id !== prevId) {
        // a fresh session in the same room (after a burn): start a clean sheet
        return { ...emptyModel(m.room), session: e.data, participants: ps, version: m.version };
      }
      break;
    }
    case "document.ready":
      m.documents = [...m.documents, { ...e.data, ts: e.ts }];
      break;
    case "voice.lock":
      if (!e.data.accepted) touch((t) => {
        t.ignored = e.data;
        if (!t.speaker) t.speaker = e.data.speaker;
      }, { speaker: e.data.speaker, lang: "" });
      break;
    case "voice.echo":
      touch((t) => {
        const lang = e.data.target_lang;
        const same = (n: EventData["voice.echo"]) =>
          n.clause === e.data.clause && n.kind === e.data.kind && n.written === e.data.written;
        const notes = t.echo?.[lang] ?? [];
        if (notes.some(same)) return;
        t.echo = { ...(t.echo ?? {}), [lang]: [...notes, e.data] };
      });
      break;
    case "voice.state":
      m.voicePaused = e.data.playing ? e.data.paused ?? [] : [];
      break;
    case "privacy.meter":
      m.privacy = e.data;
      break;
    case "gpu.stats":
      m.gpu = e.data;
      break;
    case "summary.ready":
      m.summary = e.data;
      break;
    case "burn.done":
      m.burn = e.data;
      m.summary = null;
      m.documents = [];
      break;
    default:
      break;
  }
  return m;
}

/* -------------------------------------------------------------- external store */

export class RoomStore {
  private model: RoomModel;
  private listeners = new Set<() => void>();
  private seen = new Set<string>();

  constructor(room: string) {
    this.model = emptyModel(room);
  }

  get = (): RoomModel => this.model;

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  /** Feed envelopes (deduplicated: data channel + backlog may overlap). */
  push(events: AnyEnvelope | AnyEnvelope[]): void {
    const list = Array.isArray(events) ? events : [events];
    let m = this.model;
    let changed = false;
    for (const e of list) {
      if (e.room && e.room !== m.room) continue;
      const key = dedupeKey(e);
      if (key) {
        if (this.seen.has(key)) continue;
        this.seen.add(key);
      }
      m = apply(m, e);
      changed = true;
    }
    if (changed) {
      this.model = m;
      this.listeners.forEach((l) => l());
    }
  }

  reset(): void {
    this.model = emptyModel(this.model.room);
    this.seen.clear();
    this.listeners.forEach((l) => l());
  }
}

function dedupeKey(e: AnyEnvelope): string | null {
  // Streaming deltas can legitimately repeat text; key them by seq when present, else ts.
  if (e.type === "privacy.meter" || e.type === "gpu.stats" || e.type === "session.state" || e.type === "voice.state")
    return null;
  const d = e.data as unknown as Record<string, unknown>;
  const extra =
    e.type === "translation.delta"
      ? `${d.target_lang}:${d.seq ?? ""}:${d.delta}`
      : e.type === "voice.echo" // several figures of one clause can share a millisecond
        ? `${d.target_lang}:${d.clause ?? ""}:${d.kind}:${d.written}`
        : e.type === "transcript.partial"
        ? String(d.seq)
        : "target_lang" in d
          ? String(d.target_lang)
          : "id" in d
            ? String(d.id)
            : "";
  return `${e.type}|${e.utt ?? ""}|${e.ts}|${extra}`;
}

export function useRoomModel(store: RoomStore): RoomModel {
  return useSyncExternalStore(store.subscribe, store.get, store.get);
}

/* -------------------------------------------------------------- derived */

export function checkState(t: Turn, lang: string): CheckState {
  if (t.uncertain) return "uncertain";
  const tr = t.translations[lang];
  if (!tr || !tr.final) return "translating";
  if (t.factsRed[lang]) return "red";
  const v = t.verify[lang];
  if (!v) return "pending";
  return v.badge;
}

/** Facts for display from the listener's side: src = speaker's value, tgt = what the listener read. */
export function factsFor(t: Turn, lang: string): FactCheck[] {
  return t.facts[lang] ?? [];
}

export function openFlag(t: Turn, myRole: Role | null): EventData["flag"] | null {
  if (t.continuedIn) return null; // merged into a later turn: that one asks, if anything
  for (const f of t.flags) {
    if (!f.needs_confirm) continue;
    if (f.confirm_by && myRole && f.confirm_by !== myRole) continue;
    if (t.confirms[f.id ?? "_"]) continue;
    return f;
  }
  return null;
}

/* -------------------------------------------------------------- checked facts (core/raum/pipeline/readback.py) */

type Flag = EventData["flag"];

/** What became of a flag's facts: noted (nobody asked), or asked and then confirmed / rejected / unconfirmed. */
export function flagStatus(t: Turn, f: Flag): FactStatus | null {
  if (f.status === "noted") return "noted";
  if (f.status !== "unconfirmed" && !f.needs_confirm) return null;
  const c = t.confirms[f.id ?? "_"];
  return c ? (c.accepted ? "confirmed" : "rejected") : "unconfirmed";
}

/** The read-back this device answers on this turn: asked of my role (or my language), newest first. A flag
 * from an older core without ``status`` that still asks a yes/no counts too. */
export function readBackFor(t: Turn, myRole: Role | null, myLang: string): Flag | null {
  for (let i = t.flags.length - 1; i >= 0; i--) {
    const f = t.flags[i];
    if (!f.needs_confirm) continue;
    if (f.confirm_by ? f.confirm_by !== myRole : f.target_lang && f.target_lang !== myLang) continue;
    return f;
  }
  return null;
}

export interface FactChip {
  key: string;
  kind: string;
  text: string;
  status: FactStatus;
}

const RANK: Record<FactStatus, number> = { noted: 0, confirmed: 1, unconfirmed: 2, rejected: 3 };

/** The professional's fact list for one turn: every checked fact, in my language, once (the least sure status of
 * the same chip wins, as in the summary). ``skip``: a flag shown as a read-back instead. */
export function factChips(t: Turn, lang: string, skip?: Flag | null): FactChip[] {
  const out = new Map<string, FactChip>();
  for (const f of t.flags) {
    if (!f.facts?.length || f === skip || (skip?.id && f.id === skip.id)) continue;
    const status = flagStatus(t, f);
    if (!status) continue;
    for (const x of f.facts) {
      const text = x.texts[lang] ?? x.texts.en ?? Object.values(x.texts)[0];
      if (!text) continue;
      const key = `${x.kind}|${text.toLocaleLowerCase()}`;
      const old = out.get(key);
      if (!old || RANK[status] > RANK[old.status]) out.set(key, { key, kind: x.kind, text, status });
    }
  }
  return [...out.values()];
}

/** Has this device spoken since the read-back? Then only the buttons answer it (the box listens to one reply). */
export function spokeSince(turns: Turn[], identity: string, flagTurn: Turn): boolean {
  const i = turns.indexOf(flagTurn);
  return i >= 0 && turns.slice(i + 1).some((u) => u.speaker === identity && !!u.text);
}

export function percentile(xs: number[], p: number): number | null {
  const v = xs.filter((x) => Number.isFinite(x)).sort((a, b) => a - b);
  if (!v.length) return null;
  const i = Math.min(v.length - 1, Math.max(0, Math.round((p / 100) * (v.length - 1))));
  return v[i];
}

/** The segment state of one target language (simultaneous mode), created on first use. */
function segState(t: Turn, lang: string): { parts: Record<number, Translation>; total: number | null } {
  const segs = { ...(t.segs ?? {}) };
  const sg = segs[lang] ? { ...segs[lang] } : { parts: {}, total: null };
  segs[lang] = sg;
  t.segs = segs;
  return sg;
}

/** Segments in order, joined with a space; final once every segment up to ``segs`` is final. */
function joinSegs(sg: { parts: Record<number, Translation>; total: number | null }): Translation {
  const order = Object.keys(sg.parts).map(Number).sort((a, b) => a - b);
  const text = order.map((k) => sg.parts[k].text.trim()).filter(Boolean).join(" ");
  const final = sg.total != null && order.length >= sg.total && order.slice(0, sg.total).every((k) => sg.parts[k].final);
  return { text, final };
}
