/**
 * The room drawn in plan view: walls (poché), door swing, desk, two seats.
 * A seat is solid when that person is in the room, dashed while it waits.
 */
export function RoomPlan({
  a,
  b,
  labelA,
  labelB,
  mode,
}: {
  a: boolean;
  b: boolean;
  labelA: string;
  labelB: string;
  mode: string;
}) {
  return (
    <svg className={`plan plan--${mode}`} viewBox="0 0 320 220" role="img" aria-label={`${labelA} · ${labelB}`}>
      <defs>
        <pattern id="plan-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <path d="M0 0v6" className="plan__hatchline" />
        </pattern>
      </defs>
      {/* walls with a door opening on the left of the bottom wall */}
      <path
        className="plan__wall"
        fill="url(#plan-hatch)"
        d="M14 14h292v192H118v-8h180V22H22v176h40v8H14z"
      />
      {/* door leaf and swing */}
      <path className="plan__door" d="M62 206V150" />
      <path className="plan__swing" d="M62 150a56 56 0 0 1 56 56" />
      {/* desk */}
      <rect className="plan__desk" x="128" y="84" width="64" height="52" />
      {/* seats */}
      <g className={`plan__seat ${a ? "is-in" : ""}`}>
        <rect x="142" y="48" width="36" height="26" rx="3" />
        <text x="160" y="40" textAnchor="middle">{labelA}</text>
      </g>
      <g className={`plan__seat ${b ? "is-in" : ""}`}>
        <rect x="142" y="146" width="36" height="26" rx="3" />
        <text x="160" y="190" textAnchor="middle">{labelB}</text>
      </g>
      {/* dimension along the desk's side */}
      <path className="plan__dim" d="M204 84v52M200 84h8M200 136h8M201 87l6-6M201 139l6-6" />
      <text className="plan__dimtext" x="216" y="113">0.80</text>
    </svg>
  );
}
