/**
 * RAUM event protocol (plan §6.5). TypeScript twin of core/raum/events.py.
 * Keep both in sync; core/tests/test_events.py checks that EVENT_TYPES match.
 *
 * Transport: LiveKit data channel, topic "raum.events", reliable, JSON.
 * Mirrored to /api/events/:room (WebSocket) for the table screen and the event API.
 */

export const PROTOCOL_VERSION = 1 as const;
export const EVENTS_TOPIC = "raum.events";

export type Role = "a" | "b" | "observer";
export type Path = "direct" | "tunnel" | "relayed";
export type Badge = "green" | "amber" | "red";
export type FlagKind =
  | "dose" | "allergy" | "deadline" | "amount" | "uncertain" | "date" | "number" | "mismatch" | "noted";
/** A checked fact (core/raum/pipeline/readback.py): noted = guard and checker green, nobody was asked;
 * unconfirmed = read back to the listener, no answer yet; confirmed / rejected = they said yes / no. */
export type FactStatus = "noted" | "unconfirmed" | "confirmed" | "rejected";

/** One fact of a noted / asked flag (a dose with its drug and frequency is one), its chip per language. */
export interface FlagFact {
  kind: string;
  texts: Record<string, string>;
  src?: string | null;
  tgt?: string | null;
  match?: boolean;
}
/** F-10: enrolling / short / unchecked pass; match passes; mismatch is dropped. */
export type VoiceLockStatus = "enrolling" | "match" | "mismatch" | "short" | "unchecked";

export interface ParticipantInfo {
  id: string;
  role: Role;
  lang: string;
  device: string;
  path: Path;
  rtt_ms?: number | null;
  audio_out?: "headphones" | "speaker" | "room";
  voice?: "own" | "standard"; // C-13: an AI copy of the speaker's voice, or the standard voice
}

export interface FactCheck {
  kind: string;
  src: string | null;
  tgt: string | null;
  match: boolean;
}

/** F-07: one figure read off a photographed document. */
export interface DocumentFact {
  kind: string; // date | amount | dose | deadline (also time, duration, percent, number)
  value: string; // in the reader's language
  source?: string | null; // as printed on the paper
  uncertain?: boolean; // the model was unsure, or the fact guard found other figures than printed
}

export interface SummaryItem {
  text: Record<string, string>;
  uncertain?: boolean;
}

export interface SummarySection {
  key: string;
  title: Record<string, string>;
  items: SummaryItem[];
}

/** A checked fact of the conversation and what became of it. */
export interface SummaryFact {
  kind: string;
  text: Record<string, string>;
  status: FactStatus;
  utt?: string | null;
}

/** Payload ("data") per event type. */
export interface EventData {
  "participant.joined": ParticipantInfo;
  "participant.left": { id: string };
  "utterance.start": { speaker: string; lang: string };
  "utterance.end": { speaker: string; lang: string };
  /** A sentence cut at a pause, merged again: this utterance (`utt`) goes on in `into`, which holds the whole
   * sentence. Its voice was stopped and nothing more of it comes; it is no statement of its own. */
  "utterance.continued": { speaker: string; into: string };
  "transcript.partial": { speaker: string; lang: string; text: string; seq: number };
  "transcript.final": { speaker: string; lang: string; text: string; asr_conf: number };
  "translation.delta": { target_lang: string; delta: string; seq?: number | null; seg?: number | null };
  "translation.final": { target_lang: string; text: string; seg?: number | null; segs?: number | null };
  /** Live draft while the speaker talks (never voiced): replaces the previous draft of this utterance and
   * language in place; newest `seq` wins. `seg`: simultaneous mode, the uncommitted tail after segments
   * 0 .. seg-1 (a segment >= seg covers it). The final translation (or that segment) takes its place. */
  "translation.draft": { target_lang: string; text: string; seq: number; seg?: number | null };
  "verify.facts": { target_lang: string; facts: FactCheck[]; red?: boolean; ms?: number | null; seg?: number | null };
  "verify.result": {
    target_lang: string;
    seg?: number | null;
    similarity: number | null;
    badge: Badge;
    reasons: string[];
    back_translation?: string | null;
  };
  flag: {
    kind: FlagKind;
    text: string;
    needs_confirm: boolean; // a yes/no is asked: small buttons, never modal; a spoken yes/no answers too
    id?: string | null;
    confirm_by?: Role | null;
    target_lang?: string | null;
    texts?: Record<string, string> | null;
    status?: "noted" | "unconfirmed" | null; // noted: nobody asked; unconfirmed: asked, no answer yet
    facts?: FlagFact[] | null; // the checked facts, one chip each
    show_to?: Role | null; // a flag nobody answers shows only on this role's screen (noted: "a")
    spoken?: boolean | null; // the question was read back aloud in target_lang
    why?: string[] | null; // "guard" | "check:amber" | "check:red" | "asr"
    seg?: number | null;
  };
  confirm: {
    by: string;
    accepted: boolean;
    flag_id?: string | null;
    via?: "button" | "voice" | null; // a tap, or a spoken yes/no
    answer_utt?: string | null; // via voice: the utterance that carried the answer
  };
  "repeat_back.result": {
    understood: "yes" | "partly" | "no";
    missing: string[];
    wrong: string[];
    instruction_utt?: string | null;
  };
  "offer.update": { amount: number; currency: string; by: string; ts: number; text?: string | null };
  "explain.draft": { source_utt: string; target_lang: string; text: string };
  "explain.sent": { source_utt: string; target_lang: string; text: string };
  "document.ready": {
    by: string;
    lang: string;
    title: string;
    text: string;
    facts: DocumentFact[];
    ms?: number | null;
    source_lang?: string | null;
    model?: string | null;
  };
  "pipeline.timing": {
    vad_ms?: number | null;
    asr_ms?: number | null;
    mt_first_ms?: number | null;
    mt_full_ms?: number | null;
    tts_first_ms?: number | null;
    check_ms?: number | null;
    target_lang?: string | null;
    denoise_ms?: number | null;
    models?: Record<string, string> | null;
    tts_synth_first_ms?: number | null; // first PCM from the voice engine
    tts_publish_first_ms?: number | null; // first frame accepted by the tts-<lang> AudioSource
    asr_from?: "partial" | "early" | "final" | null; // which transcript became final
    asr_early?: boolean | null; // the final ASR ran during the end-of-turn wait
    eot?: Record<string, unknown> | null; // semantic end of turn (verdict, probabilities, wait)
    echo_ms?: number | null; // voice echo check: clause synthesized -> heard back and compared (slowest clause)
    voice_clauses?: { spec: number; ahead: number; live: number } | null; // where each clause's audio came from
    stages?: Record<string, Record<string, number>> | null; // wait vs work per stage (core pipeline/stats.py)
    echo_check?: "match" | "unclear" | "mismatch" | "skipped" | null; // its worst result for the utterance
    simultaneous?: boolean | null;
    segs?: number | null;
  };
  "voice.state": { target_lang: string; playing: boolean; paused?: string[] };
  /** Voice echo check: the box heard its own voice say another figure than the text. The listener's card
   * notes "spoken: 50 mg · written: 500 mg"; the clause is said again in the standard voice. */
  "voice.echo": {
    target_lang: string;
    kind: string; // dose | number | date | time
    spoken: string; // the figure as the voice was heard to say it
    written: string; // the figure as written in the translation
    clause?: number | null; // the clause of this utterance's voice in that language, 0-based
    text?: string | null; // that clause as written
  };
  /** F-10 voice lock: one per utterance while the lock is on. accepted=false: not the device owner's voice,
   * the utterance is dropped (no transcript.final, not translated, spoken, stored or summarized). */
  "voice.lock": {
    speaker: string;
    accepted: boolean;
    status: VoiceLockStatus;
    score?: number | null; // cosine similarity with the owner's voice (glass box)
    threshold?: number | null;
    enrolled_s?: number | null; // seconds of the owner's speech learned so far
    enroll_s?: number | null; // seconds needed before the lock closes
    ms?: number | null; // embedding time (beside the transcription)
    waited_ms?: number | null; // how long the turn waited for the verdict after transcription
  };
  "session.state": {
    off_record: boolean;
    sealed: boolean;
    participants: ParticipantInfo[];
    mode: string;
    status?: "live" | "ended" | "burned";
    session_id?: string | null;
    human_interpreter_requested?: boolean;
    started_at?: number | null;
  };
  "privacy.meter": { third_party_bytes: number; tunnel_bytes: number; sealed: boolean; source?: string | null };
  "gpu.stats": { util_pct: number; models_loaded: string[]; mem_used_gb?: number | null; source?: string | null };
  "summary.ready": {
    template: string;
    lang_a: string;
    lang_b: string;
    sections: SummarySection[];
    file_note?: Record<string, string> | null;
    approved_by?: string[];
    generated_ms?: number | null;
    facts?: SummaryFact[]; // noted / unconfirmed / confirmed / rejected
  };
  "burn.done": { key_destroyed_at: number; audit_head_hash: string };
}

export const EVENT_TYPES = [
  "participant.joined",
  "participant.left",
  "utterance.start",
  "utterance.end",
  "utterance.continued",
  "transcript.partial",
  "transcript.final",
  "translation.delta",
  "translation.final",
  "translation.draft",
  "verify.facts",
  "verify.result",
  "flag",
  "confirm",
  "repeat_back.result",
  "offer.update",
  "explain.draft",
  "explain.sent",
  "document.ready",
  "pipeline.timing",
  "voice.state",
  "voice.echo",
  "voice.lock",
  "session.state",
  "privacy.meter",
  "gpu.stats",
  "summary.ready",
  "burn.done",
] as const;

export type EventType = (typeof EVENT_TYPES)[number];

export interface Envelope<T extends EventType = EventType> {
  v: 1;
  type: T;
  room: string;
  utt: string | null;
  ts: number;
  data: EventData[T];
}

export type AnyEnvelope = { [K in EventType]: Envelope<K> }[EventType];

const TYPE_SET = new Set<string>(EVENT_TYPES);

export function isEnvelope(x: unknown): x is AnyEnvelope {
  if (!x || typeof x !== "object") return false;
  const e = x as Record<string, unknown>;
  return e.v === 1 && typeof e.type === "string" && TYPE_SET.has(e.type) && typeof e.room === "string"
    && typeof e.ts === "number" && typeof e.data === "object" && e.data !== null;
}

export function parseEnvelope(raw: string | Uint8Array): AnyEnvelope | null {
  try {
    const text = typeof raw === "string" ? raw : new TextDecoder().decode(raw);
    const obj: unknown = JSON.parse(text);
    return isEnvelope(obj) ? obj : null;
  } catch {
    return null;
  }
}

/* ------------------------------------------------------------ control (device -> box) */

export type ControlAction =
  | "off_record"
  | "confirm"
  | "repeat_back"
  | "human_interpreter"
  | "explain_simply"
  | "explain_approve"
  | "correct_term"
  | "glossary"
  | "end"
  | "approve_summary"
  | "burn"
  | "mute"
  | "voice_lock_reset"; // F-10: learn the owner's voice again (by: identity, role, or none = all)

/** Body of POST /api/rooms/:room/control */
export interface Control {
  action: ControlAction;
  by?: string | null;
  value?: unknown;
  flag_id?: string | null;
  accepted?: boolean | null;
  utt?: string | null;
  text?: string | null;
}
