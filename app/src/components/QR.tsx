import { useId, useMemo } from "react";
import QRCode, { type QRCode as QRCodeData } from "qrcode";
import "./qr.css";

/*
 * A join code, drawn as inline SVG. A code is a printed object: walnut modules on paper in both themes (an
 * inverted code in the evening theme is one many phone scanners refuse). Finder squares get the system's gently
 * squared corners; with `mark` (a room id) the room's swatch sits in the middle of the code, which is then built
 * at error correction Q so the covered modules are recovered.
 */
const INK = "#211b17";
const PAPER = "#fcfcfa";
const WALL = "#f4f4f1";
const QUIET = 3; // modules of paper around the code

type Props = { value: string; size?: number; label?: string; mark?: string; className?: string };

export function QR({ value, size = 168, label, mark, className = "" }: Props) {
  const uid = useId();
  const code = useMemo(() => draw(value, mark), [value, mark]);
  if (!code) return <span className={`qrcode qrcode--empty ${className}`} style={{ width: size, height: size }} />;
  const { n, modules, finders, hole } = code;
  const full = n + QUIET * 2;
  return (
    <svg
      className={`qrcode ${className}`}
      viewBox={`${-QUIET} ${-QUIET} ${full} ${full}`}
      width={size}
      height={size}
      role="img"
      aria-label={label ?? value}
    >
      <rect x={-QUIET} y={-QUIET} width={full} height={full} rx={1.2} fill={PAPER} />
      <g className="qrcode__ink">
        <path d={modules} fill={INK} shapeRendering="crispEdges" />
        <path d={finders} fill={INK} fillRule="evenodd" />
      </g>
      {hole && mark ? <Swatch x={hole.x} y={hole.y} s={hole.s} mode={mark} id={`qr${uid.replace(/[^a-zA-Z0-9_-]/g, "")}`} /> : null}
    </svg>
  );
}

/** The room mark in module units: limewash above, the room's paint below at 5/8, the Begleitstrich between. */
function Swatch({ x, y, s, mode, id }: { x: number; y: number; s: number; mode: string; id: string }) {
  const pad = 0.55;
  const w = s - pad * 2;
  const line = y + pad + w * 0.625;
  return (
    <g className="qrcode__mark" aria-hidden="true">
      <defs>
        <clipPath id={id}>
          <rect x={x + pad} y={y + pad} width={w} height={w} rx={0.9} />
        </clipPath>
      </defs>
      <g clipPath={`url(#${id})`}>
        <rect x={x + pad} y={y + pad} width={w} height={w} fill={WALL} />
        <rect x={x + pad} y={line} width={w} height={y + pad + w - line} style={{ fill: `var(--paint-${mode}, var(--paint))` }} />
        <path d={`M${x} ${line}H${x + s}`} stroke={INK} strokeWidth={0.42} />
      </g>
      <rect x={x + pad} y={y + pad} width={w} height={w} rx={0.9} fill="none" stroke={INK} strokeWidth={0.34} />
    </g>
  );
}

type Drawn = { n: number; modules: string; finders: string; hole: { x: number; y: number; s: number } | null };

function draw(value: string, mark?: string): Drawn | null {
  let qr: QRCodeData;
  try {
    qr = QRCode.create(value, { errorCorrectionLevel: mark ? "Q" : "M" });
  } catch {
    return null;
  }
  const n = qr.modules.size;
  const dark = (r: number, c: number) => qr.modules.get(r, c) === 1;
  const inFinder = (r: number, c: number) => (r < 7 && c < 7) || (r < 7 && c >= n - 7) || (r >= n - 7 && c < 7);
  // A centre mark only up to version 6: from version 7 an alignment pattern sits in the middle of the code.
  const h = n <= 29 ? 3 : 4;
  const mid = (n - 1) / 2;
  const hole = mark && qr.version <= 6 ? { x: mid - h, y: mid - h, s: h * 2 + 1 } : null;
  const inHole = (r: number, c: number) =>
    !!hole && r >= hole.y && r < hole.y + hole.s && c >= hole.x && c < hole.x + hole.s;

  // one rect per horizontal run of dark modules
  let modules = "";
  for (let r = 0; r < n; r++) {
    let c = 0;
    while (c < n) {
      if (!dark(r, c) || inFinder(r, c) || inHole(r, c)) {
        c++;
        continue;
      }
      const start = c;
      while (c < n && dark(r, c) && !inFinder(r, c) && !inHole(r, c)) c++;
      modules += `M${start} ${r}h${c - start}v1h${start - c}z`;
    }
  }
  const finders = [
    [0, 0],
    [n - 7, 0],
    [0, n - 7],
  ]
    .map(([x, y]) => rrect(x, y, 7, 1.7) + rrect(x + 1, y + 1, 5, 1.05) + rrect(x + 2, y + 2, 3, 0.75))
    .join("");
  return { n, modules, finders, hole };
}

/** A rounded square as a path (so three of them can share one even-odd fill). */
function rrect(x: number, y: number, s: number, r: number): string {
  return (
    `M${x + r} ${y}H${x + s - r}A${r} ${r} 0 0 1 ${x + s} ${y + r}V${y + s - r}` +
    `A${r} ${r} 0 0 1 ${x + s - r} ${y + s}H${x + r}A${r} ${r} 0 0 1 ${x} ${y + s - r}V${y + r}` +
    `A${r} ${r} 0 0 1 ${x + r} ${y}Z`
  );
}
