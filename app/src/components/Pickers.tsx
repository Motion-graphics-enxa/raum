/** One-tap language, voice-output and voice choices (C-02, C-14, C-13). */
import { useRef } from "react";
import { LANGS, langName, t } from "../lib/i18n";
import { Globe } from "./Icons";
import "./pickers.css";

/** Languages the box has voices for: one tap each. Everything else via "More…". */
export const VOICE_LANGS = ["de", "en", "ru", "tr", "ar"];

export type AudioOut = "headphones" | "speaker";

/** C-13: whose voice speaks my words when the box clones voices. */
export type Voice = "own" | "standard";

export function LanguagePicker({
  lang,
  onChange,
  uiLang,
  id = "lang",
}: {
  lang: string;
  onChange: (lang: string) => void;
  uiLang: string;
  id?: string;
}) {
  const sel = useRef<HTMLSelectElement>(null);
  const options = LANGS.includes(lang) ? LANGS : [lang, ...LANGS];
  const more = () => {
    const el = sel.current as (HTMLSelectElement & { showPicker?: () => void }) | null;
    if (!el) return;
    try {
      if (el.showPicker) el.showPicker();
      else el.focus();
    } catch {
      el.focus();
    }
  };
  return (
    <div className="langpick">
      <div className="field field--select">
        <Globe />
        <select id={id} ref={sel} value={lang} onChange={(e) => onChange(e.target.value)}>
          {options.map((l) => (
            <option key={l} value={l}>
              {langName(l)}
              {l !== uiLang ? ` · ${langName(l, uiLang)}` : ""}
            </option>
          ))}
        </select>
        <span className="field__hint mono" aria-hidden="true">
          {t(uiLang, "join.change")}
        </span>
      </div>
      <div className="chips" role="group" aria-label={t(uiLang, "join.language")}>
        {VOICE_LANGS.map((l) => (
          <button
            key={l}
            type="button"
            className={`chip ${l === lang ? "is-on" : ""}`}
            aria-pressed={l === lang}
            lang={l}
            onClick={() => onChange(l)}
          >
            {langName(l)}
          </button>
        ))}
        <button type="button" className="chip chip--more" onClick={more}>
          {t(uiLang, "join.more")}
        </button>
      </div>
    </div>
  );
}

export function AudioOutPicker({
  value,
  onChange,
  uiLang,
}: {
  value: AudioOut;
  onChange: (v: AudioOut) => void;
  uiLang: string;
}) {
  return (
    <div className="seg" role="radiogroup" aria-label={t(uiLang, "join.audio_out")}>
      {(["headphones", "speaker"] as const).map((v) => (
        <button
          key={v}
          type="button"
          role="radio"
          aria-checked={value === v}
          className={`seg__opt ${value === v ? "is-on" : ""}`}
          onClick={() => onChange(v)}
        >
          <span className="seg__name">{t(uiLang, `join.${v}`)}</span>
          <span className="seg__hint">{t(uiLang, `join.${v}_hint`)}</span>
        </button>
      ))}
    </div>
  );
}

/** C-13: "My own voice" (an AI copy) or the standard voice. Shown only when the box clones voices. */
export function VoicePicker({
  value,
  onChange,
  uiLang,
}: {
  value: Voice;
  onChange: (v: Voice) => void;
  uiLang: string;
}) {
  return (
    <div className="seg" role="radiogroup" aria-label={t(uiLang, "join.voice")}>
      {(["own", "standard"] as const).map((v) => (
        <button
          key={v}
          type="button"
          role="radio"
          aria-checked={value === v}
          className={`seg__opt ${value === v ? "is-on" : ""}`}
          onClick={() => onChange(v)}
        >
          <span className="seg__name">{t(uiLang, `join.voice_${v}`)}</span>
          <span className="seg__hint">{t(uiLang, `join.voice_${v}_hint`)}</span>
        </button>
      ))}
    </div>
  );
}
