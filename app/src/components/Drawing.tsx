/**
 * Drawing primitives of the floor-plan world.
 *  - Dimension: a checked fact, dimensioned like a wall (value above, line with 45° ticks).
 *    State is line form first: solid = verified, dashed = checking, dotted = unsure.
 *  - RevisionCloud: a mismatch is marked the way drawings mark a change.
 *  - Hatch: each room's material hatch (Sprechzimmer diagonal, Kanzlei cross, Verhandlungsraum dots).
 *  - VerifyMark: the check state of one sentence, never colour alone.
 */
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import type { FactCheck } from "../lib/events";
import type { CheckState } from "../lib/store";
import { t } from "../lib/i18n";
import { Check, RevisionMark } from "./Icons";
import "./drawing.css";

export type DimState = "ok" | "pending" | "unsure" | "bad";

export function Dimension({
  value,
  kind,
  state,
  lang,
  delay = 0,
}: {
  value: ReactNode;
  kind?: string;
  state: DimState;
  lang: string;
  delay?: number;
}) {
  return (
    <span className={`dim dim--${state}`} style={{ "--dim-delay": `${delay}ms` } as React.CSSProperties}>
      <span className="dim__value mono" dir="auto">{value}</span>
      <span className="dim__line" aria-hidden="true">
        <span className="dim__end dim__end--s" />
        <span className="dim__end dim__end--e" />
      </span>
      {kind ? <span className="dim__kind">{t(lang, `kind.${kind}`)}</span> : null}
    </span>
  );
}

/** Scalloped outline drawn around its children, sized to them. */
export function RevisionCloud({ children, label }: { children: ReactNode; label?: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [box, setBox] = useState<{ w: number; h: number } | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      setBox({ w: Math.round(r.width), h: Math.round(r.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const pad = 7;
  return (
    <span className="cloud" ref={ref} role="group" aria-label={label}>
      {box ? (
        <svg
          className="cloud__svg"
          width={box.w + pad * 2}
          height={box.h + pad * 2}
          viewBox={`0 0 ${box.w + pad * 2} ${box.h + pad * 2}`}
          aria-hidden="true"
        >
          <path d={cloudPath(box.w + pad * 2, box.h + pad * 2, 5.5)} pathLength={1} />
        </svg>
      ) : null}
      <span className="cloud__body">{children}</span>
    </span>
  );
}

function cloudPath(w: number, h: number, r: number): string {
  const inset = r + 1;
  const x0 = inset, y0 = inset, x1 = w - inset, y1 = h - inset;
  const seg = (a: number, b: number) => Math.max(1, Math.round(Math.abs(b - a) / (r * 2)));
  const pts: [number, number][] = [];
  const nx = seg(x0, x1), ny = seg(y0, y1);
  for (let i = 0; i < nx; i++) pts.push([x0 + ((x1 - x0) * i) / nx, y0]);
  for (let i = 0; i < ny; i++) pts.push([x1, y0 + ((y1 - y0) * i) / ny]);
  for (let i = 0; i < nx; i++) pts.push([x1 - ((x1 - x0) * i) / nx, y1]);
  for (let i = 0; i < ny; i++) pts.push([x0, y1 - ((y1 - y0) * i) / ny]);
  let d = `M${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
  for (let i = 1; i <= pts.length; i++) {
    const [x, y] = pts[i % pts.length];
    const [px, py] = pts[i - 1];
    const rr = Math.max(2, Math.hypot(x - px, y - py) / 2);
    d += ` A${rr.toFixed(1)} ${rr.toFixed(1)} 0 0 1 ${x.toFixed(1)} ${y.toFixed(1)}`;
  }
  return d + "Z";
}

/**
 * The room mark: a swatch of the room's wall. Limewash above, the room's oil-paint dado below, the dark
 * Begleitstrich between them. Same name and props as the old material hatch, so every screen keeps its markup.
 */
export function Hatch({ mode, size = 20, className = "" }: { mode: string; size?: number; className?: string }) {
  const id = `mark-${mode}-${size}`;
  const s = 24;
  const line = 15; // the dado line sits at 5/8 of the height, on the module
  return (
    <svg
      className={`hatch hatch--${mode} ${className}`}
      width={size}
      height={size}
      viewBox={`0 0 ${s} ${s}`}
      aria-hidden="true"
    >
      <defs>
        <clipPath id={id}>
          <rect x="1" y="1" width={s - 2} height={s - 2} rx="2.5" />
        </clipPath>
      </defs>
      <g clipPath={`url(#${id})`}>
        <rect x="0" y="0" width={s} height={s} className="hatch__wall" />
        <rect x="0" y={line} width={s} height={s - line} className="hatch__paint" />
        <path d={`M0 ${line}H${s}`} className="hatch__strich" />
      </g>
      <rect x="1" y="1" width={s - 2} height={s - 2} rx="2.5" className="hatch__frame" />
    </svg>
  );
}

const MARK_LABEL: Record<CheckState, string> = {
  translating: "talk.translating",
  pending: "talk.checking",
  green: "talk.verified",
  amber: "talk.unsure",
  red: "talk.mismatch",
  uncertain: "talk.say_again",
};

export function VerifyMark({
  state,
  lang,
  onClick,
  ms,
}: {
  state: CheckState;
  lang: string;
  onClick?: () => void;
  ms?: number | null;
}) {
  const label = t(lang, MARK_LABEL[state]);
  const content = (
    <>
      <span className="vmark__glyph" aria-hidden="true">
        {state === "green" ? <Check /> : state === "red" ? <RevisionMark /> : <span className="vmark__ring" />}
      </span>
      <span className="vmark__text">{label}</span>
      {ms != null && state !== "translating" ? <span className="vmark__ms mono">{fmtMs(ms)}</span> : null}
    </>
  );
  return onClick ? (
    <button type="button" className={`vmark vmark--${state}`} onClick={onClick} aria-label={`${label}. ${t(lang, "talk.proof")}`}>
      {content}
    </button>
  ) : (
    <span className={`vmark vmark--${state}`}>{content}</span>
  );
}

export function fmtMs(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`;
}

/** One fact as the listener should read it. */
export function FactDims({
  facts,
  lang,
  state,
  max = 6,
}: {
  facts: FactCheck[];
  lang: string;
  state: CheckState;
  max?: number;
}) {
  if (!facts.length) return null;
  const shown = facts.slice(0, max);
  return (
    <span className="facts">
      {shown.map((f, i) =>
        f.match ? (
          <Dimension
            key={`${f.kind}-${i}`}
            value={f.tgt ?? f.src}
            kind={f.kind}
            lang={lang}
            delay={i * 60}
            state={state === "green" ? "ok" : state === "amber" ? "unsure" : "pending"}
          />
        ) : (
          <RevisionCloud key={`${f.kind}-${i}`} label={t(lang, "talk.mismatch")}>
            <span className="cloud__pair mono">
              <span className="cloud__said" dir="auto">{f.src ?? "—"}</span>
              <span className="cloud__ne" aria-hidden="true">≠</span>
              <span className="cloud__got" dir="auto">{f.tgt ?? "—"}</span>
            </span>
            <span className="dim__kind">{t(lang, `kind.${f.kind}`)}</span>
          </RevisionCloud>
        ),
      )}
    </span>
  );
}
