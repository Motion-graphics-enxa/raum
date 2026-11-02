/**
 * LiveKit connection for a phone in a room: mic publish, data-channel events, voice track,
 * reconnect + backlog, presence pings. The agent is the only thing we subscribe to:
 * the other person is physically present, so we only play the box's voice in our language.
 */
import { useEffect, useRef, useState } from "react";
import {
  ConnectionState,
  RemoteAudioTrack,
  RemoteTrack,
  RemoteTrackPublication,
  Room,
  RoomEvent,
  Track,
} from "livekit-client";
import { api, listOf, livekitUrl } from "./api";
import { EVENTS_TOPIC, parseEnvelope, type AnyEnvelope } from "./events";
import type { RoomStore } from "./store";

export type LinkState = "connecting" | "connected" | "reconnecting" | "disconnected";

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
  disconnect: () => void;
}

export function useLiveRoom(session: LiveSession | null, store: RoomStore, voiceLang?: string): LiveRoom {
  const [state, setState] = useState<LinkState>("connecting");
  const [micOn, setMicOn] = useState(false);
  const [micError, setMicError] = useState<string | null>(null);
  const [level, setLevel] = useState(0);
  const [rttMs, setRtt] = useState<number | null>(null);
  const roomRef = useRef<Room | null>(null);
  const analyser = useRef<{ ctx: AudioContext; node: AnalyserNode; raf: number } | null>(null);
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
    const room = new Room({ adaptiveStream: false, dynacast: false, disconnectOnPageLeave: true });
    roomRef.current = room;

    const backlog = async () => {
      try {
        const since = store.get().lastTs ? store.get().lastTs - 1 : 0;
        const evs = listOf<AnyEnvelope>(await api.events(session.room, since), "events");
        store.push(evs);
      } catch {
        /* core may be restarting; live events still flow */
      }
    };

    room.on(RoomEvent.DataReceived, (payload: Uint8Array, _p, _kind, topic?: string) => {
      if (topic && topic !== EVENTS_TOPIC) return;
      const env = parseEnvelope(payload);
      if (env) store.push(env);
    });
    room.on(RoomEvent.ConnectionStateChanged, (s: ConnectionState) => {
      if (s === ConnectionState.Connected) setState("connected");
      else if (s === ConnectionState.Reconnecting || s === ConnectionState.SignalReconnecting) setState("reconnecting");
      else if (s === ConnectionState.Disconnected) setState("disconnected");
      else setState("connecting");
    });
    room.on(RoomEvent.Reconnected, () => void backlog());

    // Voice output: subscribe only to the agent's track for our language (tts-<lang>).
    const wantVoice = (pub: RemoteTrackPublication) =>
      pub.kind === Track.Kind.Audio && pub.trackName === `tts-${voiceRef.current}`;
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

    (async () => {
      try {
        await room.connect(livekitUrl(session.livekit_url), session.token, { autoSubscribe: false });
        if (cancelled) return;
        setState("connected");
        room.remoteParticipants.forEach((p) =>
          p.trackPublications.forEach((pub) => {
            if (wantVoice(pub)) pub.setSubscribed(true);
          }),
        );
        void room.startAudio().catch(() => undefined);
        await backlog();
      } catch (err) {
        if (cancelled) return; // StrictMode remount / navigation: not a failure
        console.warn("[raum] connect failed", err);
        setState("disconnected");
      }
    })();

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
    void ping();
    const iv = window.setInterval(ping, 5000);

    return () => {
      cancelled = true;
      window.clearInterval(iv);
      stopAnalyser(analyser);
      void room.disconnect();
      roomRef.current = null;
    };
  }, [session, store]);

  const setMic = async (on: boolean) => {
    const room = roomRef.current;
    if (!room) return;
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

  return {
    state,
    micOn,
    micError,
    level,
    setMic,
    rttMs,
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

/** WebSocket mirror (table screen, listen-only, summary): /api/events/:room with reconnect. */
export function useEventSocket(room: string, store: RoomStore, url: string): LinkState {
  const [state, setState] = useState<LinkState>("connecting");
  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry = 500;
    let timer = 0;
    const open = () => {
      setState((s) => (s === "connected" ? "reconnecting" : s));
      ws = new WebSocket(url);
      ws.onopen = () => {
        retry = 500;
        setState("connected");
      };
      ws.onmessage = (m) => {
        const env = parseEnvelope(typeof m.data === "string" ? m.data : "");
        if (env) store.push(env);
      };
      ws.onclose = () => {
        if (closed) return;
        setState("reconnecting");
        timer = window.setTimeout(open, retry);
        retry = Math.min(retry * 2, 5000);
      };
      ws.onerror = () => ws?.close();
    };
    open();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      ws?.close();
    };
  }, [room, store, url]);
  return state;
}
