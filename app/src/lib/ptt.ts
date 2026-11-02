/**
 * Push-to-talk: hold the button (or Space) and speak, let go and the box ends the turn at once.
 *
 *   press   -> mic open (unmute the published track) + {type: "ptt.press"} to the agent
 *   release -> {type: "ptt.release", tail_ms} to the agent at once; the mic stays open for the tail (150 ms,
 *              so the last syllable is not clipped), then mutes. The agent ends that device's turn after the
 *              tail: no VAD silence and no end-of-turn verdict (core RoomPipeline.end_turn).
 *   press again within the tail -> the mute is skipped and the agent's end is cancelled: the same turn goes on.
 *
 * Holders: one pointer (by id, captured so a finger sliding a little does not end it; leaving the button
 * by more than the slop does) or the keyboard (Space; key repeat ignored; never while typing). A second
 * holder while one holds is ignored. Pure (no DOM): timers and the clock are injected, so node tests drive it.
 */

export const PTT_TAIL_MS = 150;
/** How far (px) a captured pointer may wander outside the button before the hold ends. */
export const PTT_SLOP_PX = 40;

export type PttMessage =
  | { type: "ptt.press"; at: number }
  | { type: "ptt.release"; at: number; tail_ms: number };

/** idle: mic muted · held: mic open · tail: let go, mic open for the tail, then idle. */
export type PttPhase = "idle" | "held" | "tail";

export interface PttDeps {
  /** Open (unmute) or close (mute) the already-published mic track. */
  setMic: (open: boolean) => void;
  /** Reliable data message to the agent (topic raum.control). */
  send: (msg: PttMessage) => void;
  now: () => number;
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (id: unknown) => void;
  onPhase?: (phase: PttPhase) => void;
  tailMs?: number;
}

export interface Ptt {
  readonly phase: PttPhase;
  /** Start holding as `holder` ("key", "pointer:<id>"). False: ignored (someone else holds). */
  press: (holder: string) => boolean;
  /** Stop holding, if `holder` is the one holding. False: ignored. */
  release: (holder: string) => boolean;
  /** Whoever holds lets go (window blur, button disabled, mode switched). */
  cancel: () => void;
  /** Unmount: no timers left; an open mic is closed now and a hold is released. */
  dispose: () => void;
}

export function createPtt(deps: PttDeps): Ptt {
  const tailMs = deps.tailMs ?? PTT_TAIL_MS;
  let phase: PttPhase = "idle";
  let holder: string | null = null;
  let timer: unknown = null;

  const set = (p: PttPhase) => {
    if (p === phase) return;
    phase = p;
    deps.onPhase?.(p);
  };
  const clearTail = () => {
    if (timer !== null) deps.clearTimeout(timer);
    timer = null;
  };

  const press = (who: string): boolean => {
    if (phase === "held") return false; // one holder at a time; also a key repeat that slipped through
    if (phase === "idle") deps.setMic(true); // in the tail it is still open
    clearTail();
    holder = who;
    set("held");
    deps.send({ type: "ptt.press", at: deps.now() });
    return true;
  };

  const letGo = () => {
    holder = null;
    set("tail");
    deps.send({ type: "ptt.release", at: deps.now(), tail_ms: tailMs });
    timer = deps.setTimeout(() => {
      timer = null;
      if (phase !== "tail") return;
      deps.setMic(false);
      set("idle");
    }, tailMs);
  };

  return {
    get phase() {
      return phase;
    },
    press,
    release: (who: string) => {
      if (phase !== "held" || who !== holder) return false;
      letGo();
      return true;
    },
    cancel: () => {
      if (phase === "held") letGo();
    },
    dispose: () => {
      if (phase === "held") deps.send({ type: "ptt.release", at: deps.now(), tail_ms: 0 });
      clearTail();
      if (phase !== "idle") deps.setMic(false);
      holder = null;
      set("idle");
    },
  };
}

/* ------------------------------------------------------------ input: what counts as a press / release */

interface KeyLike {
  code?: string;
  key?: string;
  repeat?: boolean;
  target?: unknown;
  altKey?: boolean;
  ctrlKey?: boolean;
  metaKey?: boolean;
}

/** The focus is somewhere you type: Space there is a space, not a press. */
export function isTypingTarget(t: unknown): boolean {
  if (!t || typeof t !== "object") return false;
  const el = t as { tagName?: string; isContentEditable?: boolean; type?: string };
  if (el.isContentEditable) return true;
  const tag = (el.tagName ?? "").toUpperCase();
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag !== "INPUT") return false;
  const type = (el.type ?? "text").toLowerCase();
  return !["button", "checkbox", "radio", "range", "submit", "reset", "file", "color", "image"].includes(type);
}

/** Space as the hold key. "press" / "release": act (and preventDefault); "swallow": preventDefault only
 * (key repeat); null: not ours. Space up always releases (a no-op unless the key holds): the focus may have
 * moved into a field while it was down. */
export function keyAction(ev: KeyLike, down: boolean): "press" | "release" | "swallow" | null {
  if (ev.code !== "Space" && ev.key !== " ") return null;
  if (!down) return "release";
  if (ev.altKey || ev.ctrlKey || ev.metaKey) return null;
  if (isTypingTarget(ev.target)) return null;
  return ev.repeat ? "swallow" : "press";
}

/** A captured pointer at (x, y): still on the button (its rect grown by the slop)? */
export function withinSlop(
  x: number,
  y: number,
  rect: { left: number; top: number; right: number; bottom: number },
  slop = PTT_SLOP_PX,
): boolean {
  return x >= rect.left - slop && x <= rect.right + slop && y >= rect.top - slop && y <= rect.bottom + slop;
}

/** Hands-free (today's always-on mic + VAD) or push-to-talk (the default), remembered per device. */
const HANDS_FREE_KEY = "raum:hands_free";

export function deviceHandsFree(storage: Pick<Storage, "getItem"> | null = globalStorage()): boolean {
  try {
    return storage?.getItem(HANDS_FREE_KEY) === "1";
  } catch {
    return false;
  }
}

export function rememberHandsFree(on: boolean, storage: Pick<Storage, "setItem"> | null = globalStorage()): void {
  try {
    storage?.setItem(HANDS_FREE_KEY, on ? "1" : "0");
  } catch {
    /* private mode, blocked storage: this session only */
  }
}

function globalStorage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}
