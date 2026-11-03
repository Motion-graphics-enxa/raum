/** Title block, talk control, read-back and fact notes, latency chain. */
import type { ModeConfig } from "../lib/api";
import type { EventData, FactStatus, Path } from "../lib/events";
import { UI_LANG, isRtl, langName, pick, t } from "../lib/i18n";
import type { LinkState } from "../lib/live";
import { withinSlop } from "../lib/ptt";
import type { FactChip } from "../lib/store";
import { Hatch, fmtMs } from "./Drawing";
import { ArrowRight, Check, Cross, Headphones, Menu, Mic, MicOff, Speaker } from "./Icons";
import "./sheet.css";

const PATH_KEY: Record<Path, Record<string, string>> = {
  direct: { de: "Direkt", en: "Direct", tr: "Doğrudan", ru: "Напрямую", ar: "مباشر" },
  tunnel: { de: "Tunnel", en: "Tunnel", tr: "Tünel", ru: "Туннель", ar: "نفق" },
  relayed: { de: "Relais", en: "Relayed", tr: "Aktarılan", ru: "Через ретранслятор", ar: "مُرحَّل" },
};

export function pathLabel(path: Path, lang: string): string {
  return pick(PATH_KEY[path], lang);
}

export function TitleBlock({
  mode,
  room,
  lang,
  pair,
  path = "direct",
  rtt,
  link,
  offRecord,
  onMenu,
  roles,
  audioOut,
}: {
  mode: ModeConfig;
  room: string;
  lang: string;
  pair: [string, string | null];
  path?: Path;
  rtt?: number | null;
  link: LinkState;
  offRecord?: boolean;
  onMenu?: () => void;
  /** who is who on this phone: [me, the other person], e.g. ["You", "Patient"] */
  roles?: [string, string];
  /** how this phone hears the translation */
  audioOut?: "headphones" | "speaker";
}) {
  const live = link === "connected";
  const statusKey = offRecord
    ? "talk.off_record"
    : link === "connected"
      ? "talk.live"
      : link === "reconnecting"
        ? "talk.reconnecting"
        : link === "connecting"
          ? "talk.connecting"
          : "talk.disconnected_short";
  const statusClass = offRecord ? "" : live ? "is-live" : link === "disconnected" ? "is-lost" : "is-reconnecting";
  // Talk names a dropped link in a banner right under the header: on a phone the pill keeps only its mark then,
  // so the room name and id keep their room (sheet.css)
  const named = !offRecord && (link === "reconnecting" || link === "disconnected");
  const [you, them] = roles ?? [t(lang, "talk.you"), t(lang, "talk.other")];
  return (
    <header className={`tblock tblock--${mode.id}`} data-off={offRecord ? "1" : undefined}>
      <div className="tblock__row">
        <Hatch mode={mode.id} size={30} className="tblock__hatch" />
        <div className="tblock__title">
          <span className="tblock__name">{pick(mode.name, UI_LANG)}</span>
          <span className="tblock__gloss">
            {mode.name.de && mode.name.de !== pick(mode.name, UI_LANG) ? (
              <>
                <span className="tblock__de">{mode.name.de}</span>
                <span className="tblock__sep" aria-hidden="true">
                  ·
                </span>
              </>
            ) : null}
            <span className="visually-hidden">{t(lang, "join.room")} </span>
            <span className="mono">{room.toUpperCase()}</span>
          </span>
        </div>
        <span
          className={`tblock__status ${statusClass} ${named ? "is-named" : ""}`}
          role="status"
          title={`${pathLabel(path, lang)}${rtt != null ? ` · ${rtt} ms` : ""}`}
        >
          <span className="tblock__dot" aria-hidden="true" />
          <span className="tblock__status-text">{t(lang, statusKey)}</span>
        </span>
        {onMenu ? (
          <button type="button" className="tblock__menu" onClick={onMenu} aria-label={t(lang, "talk.menu")}>
            <Menu />
          </button>
        ) : null}
      </div>
      <dl className="tblock__cells">
        <div className="tblock__cell tblock__cell--you">
          <dt>{you}</dt>
          <dd>{langName(pair[0], UI_LANG)}</dd>
        </div>
        <ArrowRight className="icon tblock__arrow" />
        <div className="tblock__cell tblock__cell--them">
          <dt>{them}</dt>
          <dd>{pair[1] ? langName(pair[1], UI_LANG) : t(lang, "talk.not_joined")}</dd>
        </div>
        <div className="tblock__cell tblock__cell--end">
          <dt>{t(lang, "talk.you_hear_via")}</dt>
          <dd>
            {audioOut === "speaker" ? <Speaker /> : <Headphones />}
            <span>{t(lang, audioOut === "speaker" ? "join.speaker_only" : "join.headphones")}</span>
          </dd>
        </div>
      </dl>
    </header>
  );
}

/** Why the mic cannot be used right now (push-to-talk). */
export type TalkBlocked = "other_speaking" | "voice_playing" | null;
/** What the orb shows. "sending": released, the box is turning the words into text. */
export type TalkPhase = "idle" | "live" | "sending" | "blocked";

/**
 * One dominant control: the mic orb. Two modes:
 *  - "handsfree" (default, today's behaviour): the box listens with Silero VAD; a tap toggles the mic.
 *  - "hold" (push-to-talk): press and hold to talk, release to send. onHoldStart / onHoldEnd fire on
 *    pointer down / up (and cancel), and on Space down / up when the orb has focus.
 * The behaviour lives in lib/ptt.ts + live.ts (Talk.tsx wires them); this component only draws the states and
 * reports gestures (pointer captured; lost capture or leaving the orb by more than the slop also ends a hold).
 * Classes for styling and tests: .talk[data-mode][data-state][data-purpose] (see DESIGN.md, Talk Control).
 */
export function TalkControl({
  on,
  level,
  disabled,
  lang,
  onToggle,
  error,
  mode = "handsfree",
  phase,
  blocked = null,
  answering = false,
  onHoldStart,
  onHoldEnd,
  onModeChange,
}: {
  on: boolean;
  level: number;
  disabled?: boolean;
  lang: string;
  onToggle: () => void;
  error?: string | null;
  mode?: "handsfree" | "hold";
  phase?: TalkPhase;
  blocked?: TalkBlocked;
  /** a read-back waits for this device's yes/no: "Hold to answer" */
  answering?: boolean;
  onHoldStart?: () => void;
  onHoldEnd?: () => void;
  /** shows the hands-free switch under the orb */
  onModeChange?: (mode: "handsfree" | "hold") => void;
}) {
  const rings = [0, 1, 2];
  const hold = mode === "hold";
  const state: TalkPhase | "disabled" =
    phase ?? (blocked ? "blocked" : disabled ? "disabled" : on ? "live" : "idle");
  const live = state === "live";
  const label = error
    ? t(lang, "talk.mic_denied")
    : state === "blocked"
      ? t(lang, blocked === "voice_playing" ? "talk.mic_wait_voice" : "talk.mic_wait_other")
      : state === "sending"
        ? t(lang, "talk.sending")
        : hold
          ? live
            ? t(lang, "talk.release_to_send")
            : t(lang, answering ? "talk.hold_to_answer" : "talk.hold_to_talk")
          : live
            ? t(lang, "talk.listening")
            : t(lang, "talk.tap_to_talk");
  const off = disabled || state === "blocked" || state === "disabled";
  const holdOn = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (!hold || off || (e.pointerType === "mouse" && e.button !== 0)) return;
    try {
      e.currentTarget.setPointerCapture?.(e.pointerId); // a finger sliding a little keeps holding
    } catch {
      /* capture is a nicety */
    }
    navigator.vibrate?.(12); // Android: a short tick under the thumb (iOS has no web vibration)
    onHoldStart?.();
  };
  const holdOff = () => {
    if (!hold) return;
    onHoldEnd?.();
  };
  // lib/ptt.ts: leaving the orb by more than the slop lets go (a release that is not held is a no-op)
  const holdMove = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (hold && !withinSlop(e.clientX, e.clientY, e.currentTarget.getBoundingClientRect())) onHoldEnd?.();
  };
  return (
    <div
      className={`talk ${on ? "is-on" : ""} ${off ? "is-disabled" : ""}`}
      data-mode={mode}
      data-state={state}
      data-purpose={answering ? "answer" : undefined}
    >
      <div className="talk__stage">
        <svg className="talk__contours" viewBox="0 0 200 200" aria-hidden="true">
          {rings.map((i) => {
            const r = 52 + i * 13 + (live ? level * (18 + i * 10) : 0);
            return <circle key={i} cx="100" cy="100" r={r} style={{ opacity: live ? 0.6 - i * 0.16 + level * 0.3 : 0.14 }} />;
          })}
        </svg>
        <button
          type="button"
          className="talk__btn"
          onClick={hold ? undefined : onToggle}
          onPointerDown={hold ? holdOn : undefined}
          onPointerUp={hold ? holdOff : undefined}
          onPointerCancel={hold ? holdOff : undefined}
          onLostPointerCapture={hold ? holdOff : undefined}
          onPointerMove={hold ? holdMove : undefined}
          onContextMenu={hold ? (e) => e.preventDefault() : undefined}
          onKeyDown={
            hold
              ? (e) => {
                  if ((e.key === " " || e.key === "Enter") && !e.repeat && !off) {
                    e.preventDefault();
                    onHoldStart?.();
                  }
                }
              : undefined
          }
          onKeyUp={
            hold
              ? (e) => {
                  if (e.key === " " || e.key === "Enter") {
                    e.preventDefault();
                    onHoldEnd?.();
                  }
                }
              : undefined
          }
          disabled={off}
          aria-pressed={live}
          aria-label={label}
          aria-keyshortcuts={hold ? "Space" : undefined}
        >
          {/* the struck mic only where it cannot send: hands-free with the mic off, blocked, or disabled */}
          {live || state === "sending" || (hold && state === "idle") ? <Mic /> : <MicOff />}
        </button>
      </div>
      <p className="talk__label" aria-live="polite">
        {label}
      </p>
      {hold && !off ? (
        <p className="talk__hint talk__hint--keys" aria-hidden="true">
          {t(lang, "talk.hold_space_pre")} <kbd>Space</kbd>
        </p>
      ) : null}
      {onModeChange ? (
        <button
          type="button"
          role="switch"
          className="talk__mode"
          aria-checked={!hold}
          onClick={() => onModeChange(hold ? "handsfree" : "hold")}
        >
          <span className="talk__switch" aria-hidden="true" />
          {t(lang, "talk.handsfree")}
        </button>
      ) : null}
    </div>
  );
}

/**
 * The read-back, like a human interpreter (core/raum/pipeline/readback.py): one short question on the turn it
 * is about ("Nur zur Sicherheit: 400 mg?"), spoken by the box and answered aloud; the two small buttons are
 * the quiet second way. Never modal, never in the way: the conversation goes on around it. A drawing note
 * (corner ticks in the pen: ochre unsure, red mismatch); answered, it settles to one line.
 */
export function ReadBack({
  flag,
  lang,
  status,
  answer,
  canSpeak,
  onAnswer,
}: {
  flag: EventData["flag"];
  lang: string;
  status: FactStatus | null;
  answer?: EventData["confirm"];
  canSpeak: boolean; // the box still takes a spoken yes/no (this device has not spoken since)
  onAnswer: (accepted: boolean) => void;
}) {
  const text = flag.texts?.[lang] ?? flag.text;
  const rtl = isRtl(lang);
  const id = `rb-${flag.id ?? "x"}`;
  const done = status === "confirmed" || status === "rejected";
  const tone = flag.kind === "mismatch" ? "bad" : "unsure";
  return (
    <section className={`rb rb--${tone} ${done ? "is-done" : ""}`} aria-labelledby={id} aria-live="polite" dir={rtl ? "rtl" : "ltr"}>
      <p id={id} className="rb__q" lang={lang}>
        {rtl ? isolateLtr(text) : text}
      </p>
      {done ? (
        <p className={`rb__done rb__done--${status}`}>
          {status === "confirmed" ? <Check /> : <Cross />}
          <span>{t(lang, status === "confirmed" ? "talk.confirmed" : "talk.rejected")}</span>
          {answer?.via === "voice" ? <span className="rb__via">{t(lang, "talk.via_voice")}</span> : null}
        </p>
      ) : (
        <div className="rb__row">
          <div className="rb__answers" role="group" aria-label={t(lang, "talk.read_back")}>
            <button type="button" className="btn btn--sm btn--ok-line" onClick={() => onAnswer(true)}>
              <Check />
              {t(lang, "talk.confirm_yes")}
            </button>
            <button type="button" className="btn btn--sm btn--bad" onClick={() => onAnswer(false)}>
              <Cross />
              {t(lang, "talk.confirm_no")}
            </button>
          </div>
          {flag.spoken && canSpeak ? (
            <p className="rb__hint">
              <Speaker />
              <span>{t(lang, "talk.say_yes_no")}</span>
            </p>
          ) : null}
        </div>
      )}
    </section>
  );
}

/** The professional's fact list on a turn: quiet notes ("✓ Allergie: Penicillin"), and what became of the
 * facts that were read back. State is the glyph and the line type (solid noted / confirmed, dotted unconfirmed),
 * named in words when it is not plain "noted"; the pen colours the glyph only. */
export function FactNotes({ chips, lang }: { chips: FactChip[]; lang: string }) {
  if (!chips.length) return null;
  return (
    <ul className="fnotes" aria-label={t(lang, "talk.facts_noted")} dir={isRtl(lang) ? "rtl" : "ltr"}>
      {chips.map((c) => (
        <li key={c.key} className={`fnote fnote--${c.status}`}>
          <span className="fnote__glyph" aria-hidden="true">
            {c.status === "unconfirmed" ? <span className="fnote__ring" /> : c.status === "rejected" ? <Cross /> : <Check />}
          </span>
          <span className="fnote__text" dir="auto">
            {isRtl(lang) ? isolateLtr(c.text) : c.text}
          </span>
          <span className={c.status === "noted" ? "visually-hidden" : "fnote__status"}>{t(lang, `fact.${c.status}`)}</span>
        </li>
      ))}
    </ul>
  );
}

/** Wrap Latin/digit runs ("12. November", "500 mg") in <bdi> so RTL text keeps them intact. */
export function isolateLtr(text: string): React.ReactNode[] {
  const re = /[0-9A-Za-z\u00C0-\u024F][0-9A-Za-z\u00C0-\u024F.,:×/%€$+\- ]*[0-9A-Za-z\u00C0-\u024F%€$]\.?|[0-9A-Za-z]/g;
  const out: React.ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(
      <bdi key={i++} dir="ltr">
        {m[0]}
      </bdi>,
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/* ---------------------------------------------------------------- Maßkette (latency dimension chain) */

export interface Stage {
  key: string;
  label: Record<string, string>;
  ms: number | null;
}

const STAGE_LABEL: Record<string, Record<string, string>> = {
  endpoint: { de: "Ende", en: "End" },
  asr: { de: "Text", en: "Text" },
  mt_first: { de: "Erste Wörter", en: "First words" },
  mt_full: { de: "Satz", en: "Sentence" },
  check: { de: "Prüfung", en: "Check" },
  tts: { de: "Stimme", en: "Voice" },
};

/**
 * Stage durations from pipeline.timing (see core/raum/pipeline/room.py): vad_ms is the endpoint
 * delay and asr_ms the transcription time (durations); mt_first_ms, mt_full_ms and tts_first_ms are
 * cumulative from the moment the speaker stopped; check_ms runs from translation.final.
 */
export function stagesOf(tm: EventData["pipeline.timing"] | undefined): Stage[] {
  if (!tm) return [];
  const vad = tm.vad_ms ?? null;
  const asr = tm.asr_ms ?? null;
  const first = tm.mt_first_ms ?? null;
  const full = tm.mt_full_ms ?? null;
  const tts = tm.tts_first_ms ?? null;
  const seg = (a: number | null, b: number | null) => (a != null && b != null ? Math.max(0, b - a) : null);
  const beforeMt = (vad ?? 0) + (asr ?? 0);
  return [
    { key: "endpoint", label: STAGE_LABEL.endpoint, ms: vad },
    { key: "asr", label: STAGE_LABEL.asr, ms: asr },
    { key: "mt_first", label: STAGE_LABEL.mt_first, ms: first != null ? Math.max(0, first - beforeMt) : null },
    { key: "mt_full", label: STAGE_LABEL.mt_full, ms: seg(first, full) },
    { key: "check", label: STAGE_LABEL.check, ms: tm.check_ms ?? null },
    { key: "tts", label: STAGE_LABEL.tts, ms: tts != null && full != null && tts > full ? tts - full : null },
  ].filter((s) => s.ms != null) as Stage[];
}

/** First translated words on the listener's screen after the speaker stopped (§3: < 1.0 s). */
export function firstWordsMs(tm: EventData["pipeline.timing"] | undefined): number | null {
  return tm?.mt_first_ms ?? null;
}

/**
 * The turn drawn to scale: one time axis from the moment the speaker stopped. The live path
 * (endpoint, transcript, first words, sentence) is a dimension chain; the async check is dashed;
 * the 1.0 s target sits on the same axis as a DIN dash-dot line with first words and voice marked.
 */
export function Timeline({
  timing,
  lang,
  big = false,
  p50 = null,
  p95 = null,
  full50 = null,
}: {
  timing: EventData["pipeline.timing"] | undefined;
  lang: string;
  big?: boolean;
  p50?: number | null;
  p95?: number | null;
  full50?: number | null;
}) {
  if (!timing) return null;
  const vad = timing.vad_ms ?? 0;
  const asr = timing.asr_ms ?? 0;
  const first = timing.mt_first_ms ?? null;
  const full = timing.mt_full_ms ?? null;
  const check = timing.check_ms ?? null;
  const tts = timing.tts_first_ms ?? null;
  const segs: { key: string; a: number; b: number; async?: boolean }[] = [];
  let t = 0;
  const push = (key: string, end: number | null, async = false) => {
    if (end == null || end <= t) return;
    segs.push({ key, a: t, b: end, async });
    t = end;
  };
  push("endpoint", vad);
  push("asr", vad + asr);
  push("mt_first", first);
  push("mt_full", full);
  if (check != null && full != null) push("check", full + check, true);
  const end = Math.max(t, tts ?? 0, 1000);
  const scale = Math.max(1500, end * 1.04);
  const pct = (ms: number) => `${(ms / scale) * 100}%`;
  const over = first != null && first > 1000;
  return (
    <figure className={`tl ${big ? "tl--big" : ""}`} aria-label="Pipeline">
      <div className="tl__chain">
        {segs.map((sg, i) => {
          const wide = (sg.b - sg.a) / scale > (big ? 0.11 : 0.16);
          return (
            <span
              key={sg.key}
              className={`tl__seg ${sg.async ? "is-async" : ""}`}
              style={{ insetInlineStart: pct(sg.a), width: pct(sg.b - sg.a), animationDelay: `${i * 70}ms` }}
            >
              <span className="tl__ms mono">{wide ? fmtMs(sg.b - sg.a) : ""}</span>
            </span>
          );
        })}
        {segs.length ? <span className="tl__tick" style={{ insetInlineStart: pct(segs[segs.length - 1].b) }} /> : null}
      </div>
      <div className="tl__budget">
        <span className="tl__target" style={{ width: pct(1000) }} />
        <span className="tl__tlabel mono" style={{ insetInlineStart: pct(1000) }}>
          1.0 s
        </span>
        {first != null ? (
          <span className={`tl__mark ${over ? "is-over" : ""}`} style={{ insetInlineStart: pct(first) }}>
            <span className="tl__mlabel mono">{fmtMs(first)}</span>
          </span>
        ) : null}
        {tts != null ? (
          <span className="tl__mark tl__mark--voice" style={{ insetInlineStart: pct(tts) }}>
            <span className="tl__mlabel tl__mlabel--below mono">
              {pick(STAGE_LABEL.tts, lang)} {fmtMs(tts)}
            </span>
          </span>
        ) : null}
      </div>
      <ol className="tl__legend">
        {segs.map((sg) => (
          <li key={sg.key} className={sg.async ? "is-async" : ""}>
            <span className="tl__swatch" aria-hidden="true" />
            <span className="tl__name">{pick(STAGE_LABEL[sg.key], lang)}</span>
            <span className="tl__lms mono">{fmtMs(sg.b - sg.a)}</span>
          </li>
        ))}
      </ol>
      {big ? (
        <figcaption className="tl__stats mono">
          <span>
            {pick(BUDGET_LABEL, lang)} 1.0 s
          </span>
          <span>p50 {p50 != null ? fmtMs(p50) : "—"}</span>
          <span>p95 {p95 != null ? fmtMs(p95) : "—"}</span>
          <span>
            {pick(FULL_LABEL, lang)} p50 {full50 != null ? fmtMs(full50) : "—"}
          </span>
        </figcaption>
      ) : null}
    </figure>
  );
}

const BUDGET_LABEL = { de: "Ziel erste Wörter", en: "Target first words" };
const FULL_LABEL = { de: "Satz", en: "Sentence" };
