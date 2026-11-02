/** N-01 offer tracker: every amount on record, as a dimension chain over time. */
import type { Offer } from "../lib/store";
import { fmtTime } from "../lib/i18n";
import "./offers.css";

export function fmtAmount(amount: number, currency: string, lang: string): string {
  try {
    return new Intl.NumberFormat(lang, {
      style: "currency",
      currency: currency.length === 3 ? currency : "EUR",
      notation: amount >= 1_000_000 ? "compact" : "standard",
      maximumFractionDigits: amount >= 1_000_000 ? 2 : 0,
    }).format(amount);
  } catch {
    return `${amount} ${currency}`;
  }
}

export function OfferStrip({ offers, lang, labelOf }: { offers: Offer[]; lang: string; labelOf: (r: string) => string }) {
  const shown = offers.slice(-4);
  return (
    <ol className="offers" aria-label="Offers">
      {shown.map((o, i) => (
        <li key={`${o.ts}-${i}`} className={`offers__item ${i === shown.length - 1 ? "is-latest" : ""}`}>
          <span className="offers__amount mono">{fmtAmount(o.amount, o.currency, lang)}</span>
          <span className="offers__line" aria-hidden="true" />
          <span className="offers__by">
            {labelOf(o.by)} · {fmtTime(o.ts, lang)}
          </span>
        </li>
      ))}
    </ol>
  );
}

export function OfferTimeline({ offers, lang, labelOf }: { offers: Offer[]; lang: string; labelOf: (r: string) => string }) {
  if (!offers.length) return null;
  const max = Math.max(...offers.map((o) => o.amount));
  return (
    <ol className="offer-tl">
      {offers.map((o, i) => (
        <li key={`${o.ts}-${i}`} className={`offer-tl__row offer-tl__row--${o.by}`}>
          <span className="offer-tl__who">{labelOf(o.by)}</span>
          <span className="offer-tl__bar" style={{ width: `${Math.max(8, (o.amount / max) * 100)}%` }} aria-hidden="true" />
          <span className="offer-tl__amount mono">{fmtAmount(o.amount, o.currency, lang)}</span>
        </li>
      ))}
    </ol>
  );
}
