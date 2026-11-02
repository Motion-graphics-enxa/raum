/**
 * The events link (WS /api/events/:room) as a small state machine: no DOM, no React, its timers and
 * socket injected, so `node --test` drives it with a fake clock (app/tests/link.test.ts).
 *
 * Phones break long-lived sockets in ways desktop browsers do not: a locked screen or a background tab
 * gets its TCP connection reset (vite logs `ws proxy socket error: read ECONNRESET`), and on resume the
 * socket may still say OPEN while nothing arrives. So:
 *  - close / error: reconnect with backoff (0.5 s, 1 s, 2 s, 4 s, then every 5 s);
 *  - heartbeat: core sends privacy.meter + gpu.stats every second and answers "ping" with "pong";
 *    nothing at all for STALE_MS means the socket is dead whatever readyState says: reconnect;
 *  - wake (visible / online / pageshow / focus): a socket quiet for WAKE_STALE_MS is replaced at once;
 *  - every reconnect asks for `?since=` a little before the newest event we hold; the store drops the
 *    overlap (RoomStore dedupes), so nothing is lost and nothing shows twice.
 */

export type LinkState = "connecting" | "connected" | "reconnecting" | "disconnected";

/** Backoff after a failed or dropped socket: fast first, capped. */
export const BACKOFF_MS = [500, 1000, 2000, 4000, 5000] as const;
export const backoffMs = (attempt: number): number => BACKOFF_MS[Math.min(Math.max(0, attempt), BACKOFF_MS.length - 1)];
/** Heartbeat cadence: check liveness and send "ping". */
export const BEAT_MS = 5000;
/** Nothing received for this long: the socket is dead (core ticks every second, pongs every beat). */
export const STALE_MS = 18000;
/** On a wake, a socket this quiet is replaced right away instead of waiting for the watchdog. */
export const WAKE_STALE_MS = 6000;
/** Catch-up starts this long before the newest event held: events may arrive out of order. */
export const OVERLAP_MS = 3000;

export const PING = "ping";
export const PONG = "pong";

const CONNECTING = 0;
const OPEN = 1;

/** The part of a browser WebSocket the link uses (`never`: a WebSocket's typed handlers fit). */
export interface SocketLike {
  readyState: number;
  send(data: string): void;
  close(): void;
  onopen: ((ev: never) => unknown) | null;
  onmessage: ((ev: never) => unknown) | null;
  onclose: ((ev: never) => unknown) | null;
  onerror: ((ev: never) => unknown) | null;
}

export interface LinkDeps {
  /** The socket URL for a catch-up cursor (0: everything). */
  url: (since: number) => string;
  open: (url: string) => SocketLike;
  /** One text frame (not "pong"). Returns the ts of a session event (moves the cursor), else null. */
  onMessage: (data: string) => number | null | undefined;
  onState: (s: LinkState) => void;
  now: () => number;
  setTimeout: (fn: () => void, ms: number) => number;
  clearTimeout: (id: number) => void;
  setInterval: (fn: () => void, ms: number) => number;
  clearInterval: (id: number) => void;
  log?: (msg: string) => void;
}

export interface EventLink {
  start(): void;
  /** The page came back (visible, online, pageshow, focus): check the socket, replace it if dead. */
  wake(): void;
  stop(): void;
  /** `?since=` for the next connect (exposed for tests). */
  since(): number;
}

/** `since` from the newest session event held: a little earlier, the store drops the overlap. */
export const sinceOf = (newestTs: number): number => (newestTs ? Math.max(0, newestTs - OVERLAP_MS) : 0);

export function createEventLink(d: LinkDeps): EventLink {
  let ws: SocketLike | null = null;
  let openedAt = 0;
  let lastRx = 0;
  let attempt = 0;
  let everOpen = false;
  let stopped = true;
  let newest = 0;
  let retry: number | null = null;
  let beat: number | null = null;
  let state: LinkState | null = null;

  const setState = (s: LinkState) => {
    if (s === state) return;
    state = s;
    d.onState(s);
  };
  const down = () => setState(everOpen ? "reconnecting" : "connecting");

  /** Forget the current socket: its late events must not reach the store or schedule anything. */
  const drop = () => {
    const s = ws;
    if (!s) return;
    ws = null;
    s.onopen = s.onmessage = s.onclose = s.onerror = null;
    try {
      s.close();
    } catch {
      /* already gone */
    }
  };

  const clearRetry = () => {
    if (retry !== null) d.clearTimeout(retry);
    retry = null;
  };

  const connect = () => {
    retry = null;
    if (stopped) return;
    drop();
    down();
    let s: SocketLike;
    try {
      s = d.open(d.url(sinceOf(newest)));
    } catch {
      schedule();
      return;
    }
    ws = s;
    openedAt = lastRx = d.now();
    s.onopen = () => {
      if (ws !== s) return;
      attempt = 0;
      everOpen = true;
      lastRx = d.now();
      setState("connected");
    };
    s.onmessage = (m: { data: unknown }) => {
      if (ws !== s) return;
      lastRx = d.now();
      const data = typeof m.data === "string" ? m.data : "";
      if (!data || data === PONG) return;
      const ts = d.onMessage(data);
      if (typeof ts === "number" && ts > newest) newest = ts;
    };
    s.onclose = () => {
      if (ws !== s) return;
      ws = null;
      schedule();
    };
    s.onerror = () => {
      if (ws !== s) return;
      drop();
      schedule();
    };
  };

  /** Next attempt after the backoff (or now). */
  const schedule = (now = false) => {
    if (stopped) return;
    down();
    clearRetry();
    const delay = now ? 0 : backoffMs(attempt);
    attempt += 1;
    retry = d.setTimeout(connect, delay);
  };

  const reconnectNow = (why: string) => {
    d.log?.(`[raum] events link: ${why}, reconnecting`);
    drop();
    attempt = 0;
    schedule(true);
  };

  const onBeat = () => {
    if (stopped || !ws) return;
    if (d.now() - lastRx > STALE_MS) {
      reconnectNow(ws.readyState === OPEN ? "silent while open" : "stuck connecting");
      return;
    }
    if (ws.readyState === OPEN) {
      try {
        ws.send(PING);
      } catch {
        reconnectNow("ping failed");
      }
    }
  };

  return {
    start() {
      if (!stopped) return;
      stopped = false;
      beat = d.setInterval(onBeat, BEAT_MS);
      connect();
    },
    wake() {
      if (stopped) return;
      const now = d.now();
      if (!ws) {
        // waiting out a backoff: try now
        if (retry !== null) {
          clearRetry();
          attempt = 0;
          schedule(true);
        }
        return;
      }
      if (ws.readyState === CONNECTING) {
        if (now - openedAt > WAKE_STALE_MS) reconnectNow("handshake hung");
        return;
      }
      if (ws.readyState !== OPEN || now - lastRx > WAKE_STALE_MS) {
        reconnectNow(`quiet ${Math.round((now - lastRx) / 1000)} s after wake`);
        return;
      }
      try {
        ws.send(PING);
      } catch {
        reconnectNow("ping failed");
      }
    },
    stop() {
      stopped = true;
      clearRetry();
      if (beat !== null) d.clearInterval(beat);
      beat = null;
      drop();
    },
    since: () => sinceOf(newest),
  };
}
