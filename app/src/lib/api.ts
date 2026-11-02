/** raum-core REST client. Same origin: Vite (dev) and Caddy (box) proxy /api and /rtc. */
import type { AnyEnvelope, Control, ParticipantInfo, Role } from "./events";

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
