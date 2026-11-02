/**
 * Conversation model built from the §6.5 event stream. One store per room view.
 * Pure reducer + tiny external store for useSyncExternalStore.
 */
import { useSyncExternalStore } from "react";
import type { AnyEnvelope, Badge, EventData, FactCheck, ParticipantInfo, Role } from "./events";

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
        t.startedAt = e.ts;
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
        const cur = t.translations[e.data.target_lang];
        if (cur?.final) return;
        t.translations = {
          ...t.translations,
          [e.data.target_lang]: { text: (cur?.text ?? "") + e.data.delta, final: false },
        };
      });
      break;
    case "translation.final":
      touch((t) => {
        const prevText = t.translations[e.data.target_lang];
        if (prevText?.final && prevText.text !== e.data.text) {
          t.corrected = { ...(t.corrected ?? {}), [e.data.target_lang]: prevText.text };
        }
        t.translations = { ...t.translations, [e.data.target_lang]: { text: e.data.text, final: true } };
      });
      break;
    case "verify.facts":
      touch((t) => {
        t.facts = { ...t.facts, [e.data.target_lang]: e.data.facts };
        t.factsRed = {
          ...t.factsRed,
          [e.data.target_lang]: e.data.red ?? e.data.facts.some((f) => !f.match),
        };
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
  for (const f of t.flags) {
    if (!f.needs_confirm) continue;
    if (f.confirm_by && myRole && f.confirm_by !== myRole) continue;
    if (t.confirms[f.id ?? "_"]) continue;
    return f;
  }
  return null;
}

export function percentile(xs: number[], p: number): number | null {
  const v = xs.filter((x) => Number.isFinite(x)).sort((a, b) => a - b);
  if (!v.length) return null;
  const i = Math.min(v.length - 1, Math.max(0, Math.round((p / 100) * (v.length - 1))));
  return v[i];
}
