/** Title block, talk control, confirm sheet, latency chain. */
import { useEffect, useRef } from "react";
import type { ModeConfig } from "../lib/api";
import type { EventData, Path } from "../lib/events";
import { pick, t } from "../lib/i18n";
import type { LinkState } from "../lib/live";
import { Dimension, Hatch, fmtMs } from "./Drawing";
import { Menu, Mic, MicOff } from "./Icons";
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
}) {
  const live = link === "connected";
  const statusKey = offRecord
    ? "talk.off_record"
    : link === "connected"
      ? "talk.listening"
      : link === "reconnecting"
        ? "talk.reconnecting"
        : link === "connecting"
          ? "talk.connecting"
          : "talk.disconnected";
  return (
    <header className={`tblock tblock--${mode.id}`} data-off={offRecord ? "1" : undefined}>
      <div className="tblock__row">
        <Hatch mode={mode.id} size={22} className="tblock__hatch" />
        <div className="tblock__title">
          <span className="tblock__name">{mode.name.de ?? pick(mode.name, lang)}</span>
          {lang !== "de" ? <span className="tblock__gloss">{pick(mode.name, lang)}</span> : null}
        </div>
        <span className={`tblock__status ${live ? "is-live" : ""}`} role="status">
          <span className="tblock__dot" aria-hidden="true" />
          <span className="tblock__status-text">{statusKey === "talk.listening" ? "Live" : t(lang, statusKey)}</span>
        </span>
        {onMenu ? (
          <button type="button" className="tblock__menu" onClick={onMenu} aria-label={t(lang, "talk.menu")}>
            <Menu />
          </button>
        ) : null}
      </div>
      <dl className="tblock__cells">
        <div className="tblock__cell">
          <dt>{t(lang, "join.room")}</dt>
          <dd className="mono">{room.toUpperCase()}</dd>
        </div>
        <div className="tblock__cell">
          <dt className="visually-hidden">{t(lang, "join.language")}</dt>
          <dd className="mono">
            {pair[0].toUpperCase()}
            <span className="tblock__arrow" aria-hidden="true">
              {" "}
              ⇄{" "}
            </span>
            {(pair[1] ?? "··").toUpperCase()}
          </dd>
        </div>
        <div className="tblock__cell tblock__cell--end">
          <dt className="visually-hidden">Path</dt>
          <dd className="mono">
            {pathLabel(path, lang)}
            {rtt != null ? ` ${rtt} ms` : ""}
          </dd>
        </div>
      </dl>
    </header>
  );
}

/** One dominant control. Open mic with Silero VAD on the box; tap toggles. Level drawn as contour lines. */
export function TalkControl({
  on,
  level,
  disabled,
  lang,
  onToggle,
  error,
}: {
  on: boolean;
  level: number;
  disabled?: boolean;
  lang: string;
  onToggle: () => void;
  error?: string | null;
}) {
  const rings = [0, 1, 2];
  return (
    <div className={`talk ${on ? "is-on" : ""} ${disabled ? "is-disabled" : ""}`}>
      <div className="talk__stage">
        <svg className="talk__contours" viewBox="0 0 200 200" aria-hidden="true">
          {rings.map((i) => {
            const r = 54 + i * 14 + (on ? level * (18 + i * 10) : 0);
            return <circle key={i} cx="100" cy="100" r={r} style={{ opacity: on ? 0.55 - i * 0.15 + level * 0.3 : 0.18 }} />;
          })}
        </svg>
        <button
          type="button"
          className="talk__btn"
          onClick={onToggle}
          disabled={disabled}
          aria-pressed={on}
          aria-label={on ? t(lang, "talk.tap_to_mute") : t(lang, "talk.tap_to_talk")}
        >
          {on ? <Mic /> : <MicOff />}
        </button>
      </div>
      <p className="talk__label" aria-live="polite">
        {error ? t(lang, "talk.mic_denied") : on ? t(lang, "talk.listening") : t(lang, "talk.tap_to_talk")}
      </p>
    </div>
  );
}

export function ConfirmSheet({
  flag,
  lang,
  onAnswer,
  kindHint,
}: {
  flag: EventData["flag"];
  lang: string;
  onAnswer: (accepted: boolean) => void;
  kindHint?: string | null;
}) {
  const yes = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    yes.current?.focus();
    if ("vibrate" in navigator && flag.kind !== "uncertain") {
      try {
        navigator.vibrate?.([30, 60, 30]);
      } catch {
        /* iOS has no vibration */
      }
    }
  }, [flag.id, flag.kind]);
  const text = flag.texts?.[lang] ?? flag.text;
  const red = flag.kind === "mismatch";
  return (
    <div className="confirm" role="alertdialog" aria-modal="true" aria-labelledby="confirm-q">
      <div className={`confirm__sheet ${red ? "is-red" : ""}`}>
        <div className="confirm__kind">
          <Dimension
            value={t(lang, `kind.${flag.kind === "mismatch" ? kindHint ?? "number" : flag.kind}`)}
            state={red ? "bad" : "pending"}
            lang={lang}
          />
        </div>
        <p id="confirm-q" className="confirm__q" dir="auto">
          {text}
        </p>
        <div className="confirm__actions">
          <button ref={yes} type="button" className="btn btn--lg btn--ok" onClick={() => onAnswer(true)}>
            {t(lang, "talk.confirm_yes")}
          </button>
          <button type="button" className="btn btn--lg btn--bad" onClick={() => onAnswer(false)}>
            {t(lang, "talk.confirm_no")}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- Maßkette (latency dimension chain) */

export interface Stage {
  key: string;
  label: Record<string, string>;
  ms: number | null;
}

const STAGE_LABEL: Record<string, Record<string, string>> = {
  endpoint: { de: "Ende erkannt", en: "End detected" },
  asr: { de: "Transkribiert", en: "Transcribed" },
  mt_first: { de: "Erste Wörter", en: "First words" },
  mt_full: { de: "Satz fertig", en: "Sentence done" },
  check: { de: "Geprüft", en: "Checked" },
  tts: { de: "Gesprochen", en: "Spoken" },
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

export function Masskette({ stages, lang, big = false }: { stages: Stage[]; lang: string; big?: boolean }) {
  const total = stages.reduce((a, s) => a + (s.ms ?? 0), 0) || 1;
  return (
    <ol className={`chain ${big ? "chain--big" : ""}`} aria-label="Pipeline">
      {stages.map((s, i) => (
        <li
          key={s.key}
          className={`chain__seg chain__seg--${s.key}`}
          style={{ flexGrow: Math.max(0.6, (s.ms ?? 0) / total) * 10, animationDelay: `${i * 70}ms` }}
        >
          <span className="chain__ms mono">{fmtMs(s.ms ?? 0)}</span>
          <span className="chain__line" aria-hidden="true" />
          <span className="chain__name">{pick(s.label, lang)}</span>
        </li>
      ))}
    </ol>
  );
}
