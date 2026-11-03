/**
 * Original voice: on a call (the two people in different places), this phone plays the other person's own mic
 * quietly under the translated voice, and ducks it while the translation speaks.
 *
 * Adapted from youssef-110/raum, call/public/index.html (`setOrigVolume`: the remote <audio> at the slider's
 * volume, capped at 0.08 while the translation speaks), Apache License 2.0. Their call carried the voice
 * peer to peer with public STUN servers; here the other phone's mic track comes over the room's own LiveKit on
 * the box, so nothing leaves the room.
 *
 *  - Call: "auto" (the default) is on when either phone came in through the tunnel (path "tunnel"), else off;
 *    "on" / "off" by hand. In the same room it stays off: the other person is heard anyway, and a second copy
 *    from the phone would echo.
 *  - Volume: the "Original voice" slider, default 0.35. While this phone's translated voice plays (voice.state
 *    for its language) it ducks to DUCK_LEVEL and comes back after.
 *  - Echo: the mic is captured with echo cancellation (live.ts MIC_OPTIONS); push-to-talk (the default) keeps it
 *    closed unless held; and while the original voice is on the box is told (ParticipantInfo.original_voice), so
 *    it pauses this phone's mic while the other person voices (core pipeline/livevoice.py).
 *  - Playback: the element's own volume where the browser honours it (Chrome, Firefox: there the echo canceller
 *    takes remote WebRTC playback as its reference); iOS Safari ignores HTMLMediaElement.volume, so there the
 *    track goes through a Web Audio gain (on iOS the system echo canceller covers all output).
 *
 * No livekit-client import at runtime (types only): node tests drive it with a fake room.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { RemoteAudioTrack, RemoteParticipant, RemoteTrack, RemoteTrackPublication, Room } from "livekit-client";

export const ORIGINAL_VOLUME = 0.35;
export const DUCK_LEVEL = 0.08;
/** Element volume steps from one level to the next over this long (Web Audio ramps on its own). */
export const RAMP_MS = 160;

export type CallSetting = "auto" | "on" | "off";
export interface OriginalVoicePrefs {
  call: CallSetting;
  volume: number;
}

const clamp01 = (v: number) => (Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : ORIGINAL_VOLUME);

/** The level the original voice plays at: the slider's, at most DUCK_LEVEL while the translation speaks. */
export function duckedVolume(volume: number, ducked: boolean): number {
  const v = clamp01(volume);
  return ducked ? Math.min(DUCK_LEVEL, v) : v;
}

/** Are the two people on a call? "auto": someone came in through the tunnel. */
export function callOn(call: CallSetting, paths: ReadonlyArray<string | null | undefined>): boolean {
  if (call === "on") return true;
  if (call === "off") return false;
  return paths.some((p) => p === "tunnel");
}

/** Does this phone play the other person's own voice? (a call, and the slider above zero) */
export function originalVoiceOn(prefs: OriginalVoicePrefs, paths: ReadonlyArray<string | null | undefined>): boolean {
  return callOn(prefs.call, paths) && clamp01(prefs.volume) > 0;
}

/* ------------------------------------------------------------ remembered per device */

const KEY = "raum:original_voice";

export function loadOriginalVoice(storage: Pick<Storage, "getItem"> | null = globalStorage()): OriginalVoicePrefs {
  try {
    const raw = storage?.getItem(KEY);
    const v = raw ? (JSON.parse(raw) as Partial<OriginalVoicePrefs>) : {};
    const call: CallSetting = v.call === "on" || v.call === "off" ? v.call : "auto";
    return { call, volume: typeof v.volume === "number" ? clamp01(v.volume) : ORIGINAL_VOLUME };
  } catch {
    return { call: "auto", volume: ORIGINAL_VOLUME };
  }
}

export function saveOriginalVoice(prefs: OriginalVoicePrefs, storage: Pick<Storage, "setItem"> | null = globalStorage()) {
  try {
    storage?.setItem(KEY, JSON.stringify({ call: prefs.call, volume: clamp01(prefs.volume) }));
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

/* ------------------------------------------------------------ which track is the other person's voice */

interface ParticipantLike {
  identity: string;
  metadata?: string;
  attributes?: Readonly<Record<string, string>>;
}
interface PublicationLike {
  kind?: string;
  source?: string;
  trackSid?: string;
  setSubscribed?: (on: boolean) => void;
}

/** A phone of the conversation's other side: role a / b from its token (core api/join.py), not this phone,
 * not the agent (its voice is the translation), not a speakerphone or an observer (the table screen). */
export function isOtherPerson(p: ParticipantLike, me: string): boolean {
  if (!p.identity || p.identity === me || p.identity.startsWith("agent-") || p.identity === "raum-agent") return false;
  let meta: Record<string, unknown> = {};
  try {
    const v = p.metadata ? JSON.parse(p.metadata) : {};
    meta = v && typeof v === "object" ? (v as Record<string, unknown>) : {};
  } catch {
    meta = {};
  }
  const get = (k: string) => String(meta[k] ?? p.attributes?.[k] ?? "");
  const head = p.identity.split("-", 1)[0];
  const role = get("role") || head;
  if (role !== "a" && role !== "b") return false;
  return get("device") !== "speakerphone" && get("audio_out") !== "room";
}

/** The other person's mic track (LiveKit `Track.Source.Microphone`, an audio track). */
export function isOtherVoice(p: ParticipantLike, pub: PublicationLike, me: string): boolean {
  return pub.kind === "audio" && pub.source === "microphone" && isOtherPerson(p, me);
}

let volumeWorks: boolean | null = null;
/** Does this browser honour HTMLMediaElement.volume? (iOS Safari: no, it always reads 1) */
export function elementVolumeWorks(doc: Document | undefined = typeof document === "undefined" ? undefined : document) {
  if (volumeWorks !== null) return volumeWorks;
  try {
    const a = doc?.createElement("audio");
    if (!a) return (volumeWorks = true);
    a.volume = 0.5;
    volumeWorks = Math.abs(a.volume - 0.5) < 0.01;
  } catch {
    volumeWorks = true;
  }
  return volumeWorks;
}

/* ------------------------------------------------------------ the controller on one LiveKit room */

export interface OriginalVoiceState {
  enabled: boolean;
  volume: number;
  ducked: boolean;
}

export interface OriginalVoice {
  set: (next: OriginalVoiceState) => void;
  /** Tracks playing now (for tests and the glass box). */
  playing: () => number;
  dispose: () => void;
}

type RoomLike = Pick<Room, "on" | "off"> & {
  remoteParticipants: Map<string, RemoteParticipant>;
};

export interface OriginalVoiceDeps {
  doc?: Document;
  webAudio?: boolean; // default: when element volume does not work (iOS)
  raf?: (fn: (t: number) => void) => number;
  now?: () => number;
}

export function createOriginalVoice(room: RoomLike, me: string, deps: OriginalVoiceDeps = {}): OriginalVoice {
  const doc = deps.doc ?? (typeof document === "undefined" ? undefined : document);
  const webAudio = deps.webAudio ?? !elementVolumeWorks(doc);
  const raf = deps.raf ?? ((fn: (t: number) => void) => requestAnimationFrame(fn));
  const now = deps.now ?? (() => performance.now());
  let state: OriginalVoiceState = { enabled: false, volume: ORIGINAL_VOLUME, ducked: false };
  // our own handle on each element: LiveKit detaches the track itself on unsubscribe, so track.detach() then
  // returns nothing and the element would stay in the page
  const tracks = new Map<string, { track: RemoteAudioTrack; el: HTMLMediaElement; level: number; ramp: number }>();
  let ctx: AudioContext | null = null;
  let disposed = false;

  const target = () => duckedVolume(state.volume, state.ducked);

  const apply = (key: string) => {
    const t = tracks.get(key);
    if (!t) return;
    const to = target();
    if (webAudio || Math.abs(to - t.level) < 0.005) {
      t.level = to;
      t.track.setVolume(to); // Web Audio: setTargetAtTime ramps it
      return;
    }
    const from = t.level;
    const start = now();
    const ramp = ++t.ramp;
    const step = () => {
      const cur = tracks.get(key);
      if (!cur || cur.ramp !== ramp || disposed) return; // a newer change took over
      const k = Math.min(1, (now() - start) / RAMP_MS);
      cur.level = k >= 1 ? to : from + (to - from) * (1 - (1 - k) ** 3); // ease-out, landing exactly on it
      cur.track.setVolume(cur.level);
      if (k < 1) raf(step);
    };
    raf(step);
  };

  const subscribeAll = (on: boolean) => {
    room.remoteParticipants.forEach((p) =>
      p.trackPublications.forEach((pub) => {
        if (isOtherVoice(p, pub, me)) pub.setSubscribed(on);
      }),
    );
  };

  const onPublished = (pub: RemoteTrackPublication, p: RemoteParticipant) => {
    if (state.enabled && isOtherVoice(p, pub, me)) pub.setSubscribed(true);
  };
  const onSubscribed = (track: RemoteTrack, pub: RemoteTrackPublication, p: RemoteParticipant) => {
    if (!isOtherVoice(p, pub, me)) return;
    if (!state.enabled) {
      pub.setSubscribed(false);
      return;
    }
    const audio = track as RemoteAudioTrack;
    if (webAudio) {
      ctx ??= new AudioContext();
      audio.setAudioContext(ctx); // before attach: the element stays muted, the gain node plays
    }
    const el = audio.attach();
    el.dataset.raumOriginal = "1";
    doc?.body.appendChild(el);
    const key = pub.trackSid ?? track.sid ?? `${p.identity}`;
    drop(key); // a resubscribe of the same track: one element only
    tracks.set(key, { track: audio, el, level: target(), ramp: 0 });
    audio.setVolume(target());
  };
  const drop = (key: string) => {
    const t = tracks.get(key);
    if (!t) return;
    tracks.delete(key);
    t.track.detach(t.el);
    t.el.remove();
  };
  const onUnsubscribed = (track: RemoteTrack, pub: RemoteTrackPublication) => {
    drop(pub.trackSid ?? track.sid ?? "");
  };
  const onBack = () => {
    if (state.enabled) subscribeAll(true);
  };
  // iOS: an AudioContext starts only from a user gesture (the Talk screen is tapped all the time)
  const resume = () => {
    if (ctx && ctx.state !== "running") void ctx.resume().catch(() => undefined);
  };

  room.on("trackPublished" as never, onPublished as never);
  room.on("trackSubscribed" as never, onSubscribed as never);
  room.on("trackUnsubscribed" as never, onUnsubscribed as never);
  // tracks already published when this phone connects come with the connect, not as trackPublished events
  room.on("connected" as never, onBack as never);
  room.on("reconnected" as never, onBack as never);
  doc?.addEventListener("pointerdown", resume, { passive: true });

  return {
    set: (next) => {
      const was = state;
      state = { enabled: next.enabled, volume: clamp01(next.volume), ducked: next.ducked };
      if (next.enabled !== was.enabled) {
        if (!next.enabled) [...tracks.keys()].forEach(drop); // silent at once, not when the server confirms
        subscribeAll(next.enabled);
      }
      if (state.volume !== was.volume || state.ducked !== was.ducked) [...tracks.keys()].forEach(apply);
    },
    playing: () => tracks.size,
    dispose: () => {
      disposed = true;
      room.off("trackPublished" as never, onPublished as never);
      room.off("trackSubscribed" as never, onSubscribed as never);
      room.off("trackUnsubscribed" as never, onUnsubscribed as never);
      room.off("connected" as never, onBack as never);
      room.off("reconnected" as never, onBack as never);
      doc?.removeEventListener("pointerdown", resume);
      [...tracks.keys()].forEach(drop);
      if (state.enabled) subscribeAll(false);
      void ctx?.close().catch(() => undefined);
      ctx = null;
    },
  };
}

/* ------------------------------------------------------------ the hook the Talk screen uses */

export interface UseOriginalVoice {
  prefs: OriginalVoicePrefs;
  /** On a call (whatever the slider says): the slider is offered. */
  call: boolean;
  /** Playing the other person's voice now (a call and a volume above zero). */
  on: boolean;
  setCall: (c: CallSetting) => void;
  setVolume: (v: number) => void;
}

/**
 * @param room   the LiveKit room (`useLiveRoom(...).room`), null until connected
 * @param paths  every participant's path (ParticipantInfo.path): "auto" turns the call on through the tunnel
 * @param ducked this phone's translated voice is playing (voice.state for its language)
 * @param report tells the box (POST /api/rooms/{room}/participant {original_voice}); called on every change
 */
export function useOriginalVoice(
  room: Room | null,
  me: string,
  paths: ReadonlyArray<string | null | undefined>,
  ducked: boolean,
  report?: (on: boolean) => void,
): UseOriginalVoice {
  const [prefs, setPrefs] = useState<OriginalVoicePrefs>(() => loadOriginalVoice());
  const pathKey = paths.join(",");
  const call = useMemo(() => callOn(prefs.call, paths), [prefs.call, pathKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const on = call && prefs.volume > 0;
  const ctl = useRef<OriginalVoice | null>(null);
  const latest = useRef<OriginalVoiceState>({ enabled: on, volume: prefs.volume, ducked });
  latest.current = { enabled: on, volume: prefs.volume, ducked };

  useEffect(() => {
    if (!room) return;
    const ov = createOriginalVoice(room as unknown as RoomLike, me);
    ctl.current = ov;
    ov.set(latest.current);
    return () => {
      ov.dispose();
      ctl.current = null;
    };
  }, [room, me]);
  useEffect(() => {
    ctl.current?.set({ enabled: on, volume: prefs.volume, ducked });
  }, [on, prefs.volume, ducked]);
  const reportRef = useRef(report);
  reportRef.current = report;
  useEffect(() => {
    reportRef.current?.(on);
  }, [on]);

  return {
    prefs,
    call,
    on,
    setCall: (c) =>
      setPrefs((p) => {
        const next = { ...p, call: c };
        saveOriginalVoice(next);
        return next;
      }),
    setVolume: (v) =>
      setPrefs((p) => {
        const next = { ...p, volume: clamp01(v) };
        saveOriginalVoice(next);
        return next;
      }),
  };
}
