/** raum-core REST client. Same origin: Vite (dev) and Caddy (box) proxy /api and /rtc. */
import type { AnyEnvelope, Control, EventData, ParticipantInfo, Role } from "./events";

export interface RoleConfig {
  label: Record<string, string>;
  default_lang: string;
}

export interface ModeConfig {
  id: string;
  prefix: string;
  name: Record<string, string>;
  tagline?: Record<string, string> | null;
  icon: string;
  roles: { a: RoleConfig; b: RoleConfig };
  register: string;
  guard: { kinds: string[]; confirm_on: string[] };
  summary: { sections: string[]; takehome_for?: "a" | "b" | null; file_note_for?: "a" | "b" | null };
  buttons: string[];
  retention?: { auto_burn_minutes?: number | null };
}

export interface JoinResponse {
  token: string;
  livekit_url: string | null;
  identity: string;
  room: string;
  role: Role;
  lang: string;
  mode: ModeConfig;
  session_id: string;
}

export interface RoomInfo {
  room: string;
  mode: ModeConfig | string;
  status?: string;
  participants?: ParticipantInfo[];
  agent_connected?: boolean;
  state?: Record<string, unknown>;
  [k: string]: unknown;
}

/** GET /api/admin/latency: p50/p95 per pipeline.timing field (ms) for each room's session. */
export interface LatencyStat {
  n: number;
  p50: number | null;
  p95: number | null;
}
export type TimingKey = "vad_ms" | "asr_ms" | "mt_first_ms" | "mt_full_ms" | "tts_first_ms" | "check_ms";
export type RoomLatency = { n: number; over_budget: number } & Record<TimingKey, LatencyStat>;
export interface AdminLatency {
  budget_ms: number;
  window: number;
  rooms: Record<string, RoomLatency>;
}

/** GET /api/admin/audit/verify: the audit log's hash chain recomputed (T-04). */
export interface AuditVerify {
  ok: boolean | null;
  lines: number | null;
  first_bad: number | null;
  reason: string | null;
  head_hash: string;
  checked_at: number;
  ms: number;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, msg);
  }
  const text = await res.text();
  return (text ? JSON.parse(text) : null) as T;
}

export const api = {
  join: (b: { room: string; role: Role; lang: string; device: string; consent: boolean; audio_out?: string }) =>
    req<JoinResponse>("POST", "/api/join", b),
  modes: () => req<ModeConfig[] | Record<string, ModeConfig>>("GET", "/api/modes"),
  rooms: () => req<RoomInfo[] | { rooms: RoomInfo[] }>("GET", "/api/rooms"),
  createRoom: (mode: string) => req<{ room: string }>("POST", "/api/rooms", { mode }),
  room: (room: string) => req<RoomInfo>("GET", `/api/rooms/${room}`),
  events: (room: string, since = 0) =>
    req<AnyEnvelope[] | { events: AnyEnvelope[] }>("GET", `/api/rooms/${room}/events?since=${since}`),
  control: (room: string, c: Control) => req<unknown>("POST", `/api/rooms/${room}/control`, c),
  participant: (room: string, identity: string, change: { lang?: string; audio_out?: string }) =>
    req<ParticipantInfo>("POST", `/api/rooms/${room}/participant`, { identity, ...change }),
  presence: (room: string, identity: string, rtt_ms: number) =>
    req<unknown>("POST", `/api/rooms/${room}/presence`, { identity, rtt_ms }),
  end: (room: string) => req<unknown>("POST", `/api/rooms/${room}/end`),
  summary: (room: string) => req<unknown>("GET", `/api/rooms/${room}/summary`),
  approve: (room: string, role: string) => req<unknown>("POST", `/api/rooms/${room}/summary/approve`, { role }),
  burn: (room: string) => req<unknown>("POST", `/api/rooms/${room}/burn`),
  compliance: (room: string) => req<unknown>("GET", `/api/rooms/${room}/compliance`),
  adminStatus: () => req<Record<string, unknown>>("GET", "/api/admin/status"),
  adminLatency: () => req<AdminLatency>("GET", "/api/admin/latency"),
  auditVerify: () => req<AuditVerify>("GET", "/api/admin/audit/verify"),
  seal: (on: boolean) => req<unknown>("POST", "/api/admin/seal", { on }),
  burnAll: () => req<unknown>("POST", "/api/admin/burn-all"),
  health: () => req<unknown>("GET", "/api/health"),
};

export function listOf<T>(x: T[] | Record<string, T[]> | Record<string, T> | null | undefined, key?: string): T[] {
  if (!x) return [];
  if (Array.isArray(x)) return x;
  if (key && Array.isArray((x as Record<string, T[]>)[key])) return (x as Record<string, T[]>)[key];
  return Object.values(x as Record<string, T>);
}

/** ws(s)://<this origin>/api/events/:room */
export function eventsSocketUrl(room: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/api/events/${encodeURIComponent(room)}`;
}

/** LiveKit URL: core may return null/"auto" -> same origin (proxied /rtc). */
export function livekitUrl(fromCore: string | null | undefined): string {
  if (fromCore && fromCore !== "auto") return fromCore;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}`;
}

/* ------------------------------------------------------------ uploads (F-07 document camera, N-02 glossary) */

/** One glossary term in the glossaries/*.yaml shape: {de, kind?, keep?, <lang>: [forms]}. */
export type GlossaryTerm = { de: string; kind?: string; keep?: boolean } & Record<string, unknown>;

export interface GlossaryUploadResult {
  ok: boolean;
  room: string;
  terms: number; // terms in the upload
  added: number; // new to the session glossary
  updated: number; // already known: the uploaded forms are now preferred
  total: number; // terms in the session glossary now
  langs: string[];
  forwarded: boolean; // reached the interpreter now (else when it links)
  uploaded: GlossaryTerm[];
}

export interface GlossaryInfo {
  room: string;
  source: string | null; // the room's glossary file
  uploaded: number; // terms uploaded this session
  langs: string[];
  terms: GlossaryTerm[];
}

async function postForm<T>(path: string, form: FormData): Promise<T> {
  // no content-type header: the browser sets multipart/form-data with its boundary
  const res = await fetch(path, { method: "POST", body: form });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, msg);
  }
  return (await res.json()) as T;
}

export const uploads = {
  /** F-07: POST the photo (multipart image, by, lang). Resolves with the document.ready payload. */
  document: (room: string, image: Blob, by: string, lang: string, filename = "document.jpg") => {
    const f = new FormData();
    f.append("by", by);
    f.append("lang", lang);
    f.append("image", image, filename);
    return postForm<EventData["document.ready"]>(`/api/rooms/${encodeURIComponent(room)}/document`, f);
  },
  /** N-02: a .yaml (glossaries/*.yaml format) or .csv (de,<lang>,...) file for the room's session. */
  glossary: (room: string, file: Blob, filename: string, by?: string) => {
    const f = new FormData();
    if (by) f.append("by", by);
    f.append("file", file, filename);
    return postForm<GlossaryUploadResult>(`/api/rooms/${encodeURIComponent(room)}/glossary`, f);
  },
  glossaryInfo: (room: string) => req<GlossaryInfo>("GET", `/api/rooms/${encodeURIComponent(room)}/glossary`),
};
