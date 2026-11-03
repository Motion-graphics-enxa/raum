/** Call mode in the Talk menu: is the other person here or on a call, and how loud their own voice plays under the
 * translation (src/lib/originalVoice.ts). Two segmented tiles like the audio route; the slider only on a call. */
import { useId } from "react";
import { t } from "../lib/i18n";
import type { UseOriginalVoice } from "../lib/originalVoice";
import "./originalVoice.css";

export function OriginalVoiceControl({ ov, uiLang }: { ov: UseOriginalVoice; uiLang: string }) {
  const id = useId();
  const pct = Math.round(ov.prefs.volume * 100);
  return (
    <div className="ovoice">
      <span className="label" id={`${id}-where`}>
        {t(uiLang, "call.where")}
      </span>
      <div className="seg" role="radiogroup" aria-labelledby={`${id}-where`}>
        {([
          ["off", "call.here", !ov.call],
          ["on", "call.away", ov.call],
        ] as const).map(([value, key, on]) => (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={on}
            className={`seg__opt ${on ? "is-on" : ""}`}
            onClick={() => ov.setCall(value)}
          >
            <span className="seg__name">{t(uiLang, key)}</span>
            <span className="seg__hint">{t(uiLang, `${key}_hint`)}</span>
          </button>
        ))}
      </div>
      {ov.call ? (
        <div className="ovoice__level">
          <label className="ovoice__name" htmlFor={`${id}-vol`}>
            {t(uiLang, "call.original")}
          </label>
          <output className="ovoice__pct" htmlFor={`${id}-vol`}>
            {pct} %
          </output>
          <input
            id={`${id}-vol`}
            className="ovoice__range"
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={ov.prefs.volume}
            aria-valuetext={`${pct} %`}
            aria-describedby={`${id}-hint`}
            style={{ "--fill": `${pct}%` } as React.CSSProperties}
            onChange={(e) => ov.setVolume(Number(e.currentTarget.value))}
          />
          <p className="ovoice__hint" id={`${id}-hint`}>
            {t(uiLang, "call.original_hint")}
          </p>
        </div>
      ) : null}
    </div>
  );
}
