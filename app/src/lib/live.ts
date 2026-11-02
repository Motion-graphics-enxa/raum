/**
 * LiveKit connection for a phone in a room: mic publish, data-channel events, voice track,
 * reconnect + backlog, presence pings. The agent is the only thing we subscribe to:
 * the other person is physically present, so we only play the box's voice in our language.
 *
 * Reconnect and resume (C-12). The session lives on the box, so a phone that drops only has to
 * come back and catch up:
 *  - LiveKit's own reconnect (resume or full, ~45 s of retries) ends in `Reconnected`;
 *  - if it gives up, or the server dropped us, we rejoin the same room with the same token
 *    (same identity) and keep retrying with backoff, sooner when the browser says it is online
 *    or the page becomes visible again;
 *  - either way we then fetch core's backlog from just before the newest event we hold (the store
 *    deduplicates), turn the microphone back on if it was on, and keep the RoomStore: turns are
 *    never reset.
 */
import { useEffect, useRef, useState } from "react";
import {
  ConnectionError,
  ConnectionErrorReason,
  ConnectionState,
  DisconnectReason,
  RemoteAudioTrack,
  RemoteTrack,
  RemoteTrackPublication,
  Room,
  RoomEvent,
  Track,
} from "livekit-client";
import { api, listOf, livekitUrl } from "./api";
import { EVENTS_TOPIC, parseEnvelope, type AnyEnvelope } from "./events";
import { createEventLink, OVERLAP_MS, type LinkState } from "./link";
import type { RoomStore } from "./store";

export type { LinkState } from "./link";

export interface LiveSession {
  room: string;
  token: string;
  livekit_url: string | null;
  identity: string;
  lang: string;
}

export interface LiveRoom {
  state: LinkState;
  micOn: boolean;
  micError: string | null;
  level: number; // 0..1, smoothed
  setMic: (on: boolean) => Promise<void>;
  rttMs: number | null;
  /** The browser refused to start the box's voice without a tap (iOS autoplay): show "Tap to hear". */
  audioBlocked: boolean;
  /** Start the voice from a user gesture. Text rendering never waits for it. */
  startAudio: () => void;
  disconnect: () => void;
}

/** Envelopes that are not part of the session record (they never move the backlog cursor). */
const EPHEMERAL = new Set<string>(["privacy.meter", "gpu.stats"]);
/** Catch-up starts a little before the newest event we hold: agent and core events may arrive out
 * of order, and the store drops the duplicates. */
const BACKLOG_OVERLAP_MS = OVERLAP_MS;
const REJOIN_MIN_MS = 1000;
const REJOIN_MAX_MS = 8000;
/** Disconnects a rejoin must not undo: we left, another tab took our identity, the box removed us
 * or deleted the room. */
const FINAL_DISCONNECT = new Set<DisconnectReason>([
  DisconnectReason.CLIENT_INITIATED,
  DisconnectReason.DUPLICATE_IDENTITY,
  DisconnectReason.PARTICIPANT_REMOVED,
  DisconnectReason.ROOM_DELETED,
]);

/** Newest session event seen on one channel, for `?since=` on the way back. */
function tsCursor() {
  let ts = 0;
  return {
    take: (e: AnyEnvelope | AnyEnvelope[]) => {
      for (const x of Array.isArray(e) ? e : [e]) if (!EPHEMERAL.has(x.type)) ts = Math.max(ts, x.ts);
    },
    since: () => (ts ? Math.max(0, ts - BACKLOG_OVERLAP_MS) : 0),
  };
}

export function useLiveRoom(session: LiveSession | null, store: RoomStore, voiceLang?: string): LiveRoom {
  const [state, setState] = useState<LinkState>("connecting");
  const [micOn, setMicOn] = useState(false);
  const [micError, setMicError] = useState<string | null>(null);
  const [level, setLevel] = useState(0);
  const [rttMs, setRtt] = useState<number | null>(null);
  const [audioBlocked, setAudioBlocked] = useState(false);
  const roomRef = useRef<Room | null>(null);
  const analyser = useRef<{ ctx: AudioContext; node: AnalyserNode; raf: number } | null>(null);
  const micWanted = useRef(false); // what the user chose; restored after a reconnect or rejoin
  const setMicRef = useRef<(on: boolean) => Promise<void>>(async () => undefined);
  const voiceRef = useRef(voiceLang ?? session?.lang ?? "");
  voiceRef.current = voiceLang ?? session?.lang ?? "";

  // Language changed after joining: follow the box's voice track for the new language.
  useEffect(() => {
    const room = roomRef.current;
    if (!room) return;
    room.remoteParticipants.forEach((p) =>
      p.trackPublications.forEach((pub) => {
        if (pub.kind === Track.Kind.Audio && pub.trackName?.startsWith("tts-")) {
          pub.setSubscribed(pub.trackName === `tts-${voiceRef.current}`);
        }
      }),
    );
  }, [voiceLang]);

  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    let connecting = false;
    let rejoining = false;
    let attempt = 0;
    let retryTimer = 0;
    const cursor = tsCursor();
    const room = new Room({ adaptiveStream: false, dynacast: false, disconnectOnPageLeave: true });
    roomRef.current = room;
    if (import.meta.env.DEV) (window as unknown as { __raumLive?: unknown }).__raumLive = { room, store };

    const backlog = async () => {
      try {
        const evs = listOf<AnyEnvelope>(await api.events(session.room, cursor.since()), "events");
        cursor.take(evs);
        store.push(evs);
      } catch {
        /* core may be restarting; live events still flow */
      }
    };

    const restoreMic = async () => {
      if (!micWanted.current || room.state !== ConnectionState.Connected) return;
      const pub = room.localParticipant.getTrackPublication(Track.Source.Microphone);
      const mst = pub?.track?.mediaStreamTrack;
      if (pub && !pub.isMuted && mst && mst.readyState === "live") return;
      await setMicRef.current(true);
    };

    // Presence: measure RTT to the box and report it (glass-box connection badge, G-03).
    const ping = async () => {
      const t0 = performance.now();
      try {
        await api.health();
        const rtt = Math.round(performance.now() - t0);
        setRtt(rtt);
        await api.presence(session.room, session.identity, rtt).catch(() => undefined);
      } catch {
        setRtt(null);
      }
    };

    // Voice output: subscribe only to the agent's track for our language (tts-<lang>).
    const wantVoice = (pub: RemoteTrackPublication) =>
      pub.kind === Track.Kind.Audio && pub.trackName === `tts-${voiceRef.current}`;
    const subscribeVoice = () =>
      room.remoteParticipants.forEach((p) =>
        p.trackPublications.forEach((pub) => {
          if (wantVoice(pub)) pub.setSubscribed(true);
        }),
      );

    const scheduleRejoin = (now = false) => {
      if (cancelled) return;
      rejoining = true;
      setState("reconnecting");
      window.clearTimeout(retryTimer);
      const delay = now ? 0 : Math.min(REJOIN_MAX_MS, REJOIN_MIN_MS * 2 ** attempt);
      attempt += 1;
      retryTimer = window.setTimeout(() => void connect("rejoin"), delay);
    };

    const connect = async (why: "join" | "rejoin") => {
      if (cancelled || connecting || room.state !== ConnectionState.Disconnected) return;
      connecting = true;
      try {
        await room.connect(livekitUrl(session.livekit_url), session.token, { autoSubscribe: false });
        connecting = false;
        if (cancelled) return;
        attempt = 0;
        rejoining = false;
        setState("connected");
        subscribeVoice();
        // Text first: the backlog never waits for the voice (iOS may hold startAudio until a tap).
        void room
          .startAudio()
          .catch(() => undefined)
          .finally(() => setAudioBlocked(!room.canPlaybackAudio));
        await backlog();
        if (why === "rejoin") {
          console.info("[raum] rejoined", session.room);
          void ping();
          await restoreMic();
        }
      } catch (err) {
        connecting = false;
        if (cancelled) return; // StrictMode remount / navigation: not a failure
        console.warn(`[raum] ${why} failed`, err);
        if (err instanceof ConnectionError && err.reason === ConnectionErrorReason.NotAllowed) {
          rejoining = false;
          setState("disconnected"); // token refused: the Talk screen offers a fresh join
          return;
        }
        scheduleRejoin();
      }
    };

    room.on(RoomEvent.DataReceived, (payload: Uint8Array, _p, _kind, topic?: string) => {
      if (topic && topic !== EVENTS_TOPIC) return;
      const env = parseEnvelope(payload);
      if (env) {
        cursor.take(env);
        store.push(env);
      }
    });
    room.on(RoomEvent.ConnectionStateChanged, (s: ConnectionState) => {
      if (s === ConnectionState.Connected) setState("connected");
      else if (s === ConnectionState.Reconnecting || s === ConnectionState.SignalReconnecting) setState("reconnecting");
      else if (s === ConnectionState.Connecting) setState(rejoining ? "reconnecting" : "connecting");
      // Disconnected: RoomEvent.Disconnected decides between rejoining and giving up
    });
    room.on(RoomEvent.AudioPlaybackStatusChanged, () => setAudioBlocked(!room.canPlaybackAudio));
    room.on(RoomEvent.Reconnected, () => {
      void backlog();
      void restoreMic();
    });
    room.on(RoomEvent.Disconnected, (reason?: DisconnectReason) => {
      stopAnalyser(analyser);
      setLevel(0);
      if (cancelled || connecting) return; // a failed connect() schedules its own retry
      if (reason !== undefined && FINAL_DISCONNECT.has(reason)) {
        setState("disconnected");
        return;
      }
      console.info("[raum] disconnected, rejoining", reason);
      scheduleRejoin();
    });
    room.on(RoomEvent.TrackPublished, (pub: RemoteTrackPublication) => {
      if (wantVoice(pub)) pub.setSubscribed(true);
    });
    room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack, pub: RemoteTrackPublication) => {
      if (track.kind === Track.Kind.Audio && wantVoice(pub)) {
        const el = (track as RemoteAudioTrack).attach();
        el.dataset.raumVoice = "1";
        document.body.appendChild(el);
      }
    });
    room.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
      track.detach().forEach((el) => el.remove());
    });

    // Back online, or back in the foreground (phones suspend background tabs): retry right away,
    // or catch up on anything a frozen page missed.
    const wake = () => {
      if (cancelled) return;
      if (rejoining && room.state === ConnectionState.Disconnected) scheduleRejoin(true);
      else if (room.state === ConnectionState.Connected) void backlog();
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") wake();
    };
    window.addEventListener("online", wake);
    window.addEventListener("pageshow", wake);
    window.addEventListener("focus", wake);
    document.addEventListener("visibilitychange", onVisible);

    void connect("join");
    void ping();
    const iv = window.setInterval(ping, 5000);

    return () => {
      cancelled = true;
      window.clearTimeout(retryTimer);
      window.clearInterval(iv);
      window.removeEventListener("online", wake);
      window.removeEventListener("pageshow", wake);
      window.removeEventListener("focus", wake);
      document.removeEventListener("visibilitychange", onVisible);
      stopAnalyser(analyser);
      void room.disconnect();
      roomRef.current = null;
    };
  }, [session, store]);

  const setMic = async (on: boolean) => {
    const room = roomRef.current;
    if (!room) return;
    micWanted.current = on;
    try {
      setMicError(null);
      await room.localParticipant.setMicrophoneEnabled(on, {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      });
      setMicOn(on);
      stopAnalyser(analyser);
      if (on) {
        const mst = room.localParticipant.getTrackPublication(Track.Source.Microphone)?.track?.mediaStreamTrack;
        if (mst) startAnalyser(mst, analyser, setLevel);
      } else {
        setLevel(0);
      }
      void api.control(room.name, { action: "mute", value: !on, by: session?.identity }).catch(() => undefined);
    } catch (err) {
      console.warn("[raum] mic", err);
      setMicOn(false);
      setMicError(err instanceof Error ? err.name || err.message : String(err));
    }
  };
  setMicRef.current = setMic;

  return {
    state,
    micOn,
    micError,
    level,
    setMic,
    rttMs,
    audioBlocked,
    startAudio: () => {
      const room = roomRef.current;
      if (!room || room.canPlaybackAudio) return;
      void room
        .startAudio()
        .catch(() => undefined)
        .finally(() => setAudioBlocked(!room.canPlaybackAudio));
    },
    disconnect: () => void roomRef.current?.disconnect(),
  };
}

function startAnalyser(
  track: MediaStreamTrack,
  ref: React.MutableRefObject<{ ctx: AudioContext; node: AnalyserNode; raf: number } | null>,
  setLevel: (n: number) => void,
) {
  try {
    const ctx = new AudioContext();
    const src = ctx.createMediaStreamSource(new MediaStream([track]));
    const node = ctx.createAnalyser();
    node.fftSize = 512;
    src.connect(node);
    const buf = new Float32Array(node.fftSize);
    let smooth = 0;
    let last = 0;
    const tick = (now: number) => {
      node.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      const v = Math.min(1, rms * 6);
      smooth = v > smooth ? v * 0.6 + smooth * 0.4 : v * 0.12 + smooth * 0.88;
      if (now - last > 50) {
        setLevel(smooth);
        last = now;
      }
      if (ref.current) ref.current.raf = requestAnimationFrame(tick);
    };
    ref.current = { ctx, node, raf: requestAnimationFrame(tick) };
  } catch {
    /* analyser is cosmetic */
  }
}

function stopAnalyser(ref: React.MutableRefObject<{ ctx: AudioContext; node: AnalyserNode; raf: number } | null>) {
  if (!ref.current) return;
  cancelAnimationFrame(ref.current.raf);
  void ref.current.ctx.close().catch(() => undefined);
  ref.current = null;
}

/** WebSocket mirror (table screen, listen-only, summary, and the talk screen's second source):
 * /api/events/:room through the events link (./link.ts: backoff, heartbeat, wake, `?since=` resync). */
export function useEventSocket(room: string, store: RoomStore, url: string): LinkState {
  const [state, setState] = useState<LinkState>("connecting");
  useEffect(() => {
    const link = createEventLink({
      url: (since) => (since ? `${url}${url.includes("?") ? "&" : "?"}since=${since}` : url),
      open: (u) => new WebSocket(u),
      onMessage: (data) => {
        const env = parseEnvelope(data);
        if (!env) return null;
        store.push(env);
        return EPHEMERAL.has(env.type) ? null : env.ts;
      },
      onState: setState,
      now: () => Date.now(),
      setTimeout: (fn, ms) => window.setTimeout(fn, ms),
      clearTimeout: (id) => window.clearTimeout(id),
      setInterval: (fn, ms) => window.setInterval(fn, ms),
      clearInterval: (id) => window.clearInterval(id),
      log: (msg) => console.info(msg, room),
    });
    const wake = () => link.wake();
    const onVisible = () => {
      if (document.visibilityState === "visible") wake();
    };
    window.addEventListener("online", wake);
    window.addEventListener("pageshow", wake);
    window.addEventListener("focus", wake);
    document.addEventListener("visibilitychange", onVisible);
    link.start();
    return () => {
      window.removeEventListener("online", wake);
      window.removeEventListener("pageshow", wake);
      window.removeEventListener("focus", wake);
      document.removeEventListener("visibilitychange", onVisible);
      link.stop();
    };
  }, [room, store, url]);
  return state;
}
