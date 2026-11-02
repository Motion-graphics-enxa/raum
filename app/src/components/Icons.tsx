/** Drawn line icons, one stroke weight, in the plan's pen. 24×24 grid. */
import type { SVGProps } from "react";

type P = SVGProps<SVGSVGElement> & { title?: string };

function I({ title, children, ...rest }: P & { children: React.ReactNode }) {
  return (
    <svg viewBox="0 0 24 24" className="icon" aria-hidden={title ? undefined : true} role={title ? "img" : undefined} {...rest}>
      {title ? <title>{title}</title> : null}
      {children}
    </svg>
  );
}

export const Stethoscope = (p: P) => (
  <I {...p}>
    <path d="M6 3v6a4 4 0 0 0 8 0V3" />
    <path d="M10 13v2.5a4.5 4.5 0 0 0 9 0V13" />
    <circle cx="19" cy="11" r="2" />
    <path d="M5 3h2M13 3h2" />
  </I>
);

export const Scale = (p: P) => (
  <I {...p}>
    <path d="M12 3v18M7 21h10M5 7h14" />
    <path d="M5 7l-3 7a3.5 3.5 0 0 0 6 0L5 7zM19 7l-3 7a3.5 3.5 0 0 0 6 0l-3-7z" />
  </I>
);

export const Handshake = (p: P) => (
  <I {...p}>
    <path d="M2 11l4-4 4 2 3-2 3 1 6 3" />
    <path d="M6 7v6l5 5a1.5 1.5 0 0 0 2-2M10 14l3 3a1.5 1.5 0 0 0 2-2l-3-3M13 12l3 3a1.5 1.5 0 0 0 2-2l-4-4" />
    <path d="M18 9v4" />
  </I>
);

export const Mic = (p: P) => (
  <I {...p}>
    <rect x="9" y="2.5" width="6" height="12" rx="3" />
    <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7" />
  </I>
);

export const MicOff = (p: P) => (
  <I {...p}>
    <path d="M15 9.5V5.5a3 3 0 0 0-5.6-1.5M9 9v2.5a3 3 0 0 0 4.7 2.5" />
    <path d="M5.5 11a6.5 6.5 0 0 0 10.7 5M18.4 12.5a6.5 6.5 0 0 0 .1-1.5M12 17.5V21M8.5 21h7M3 3l18 18" />
  </I>
);

export const Pause = (p: P) => (
  <I {...p}>
    <rect x="3.5" y="3.5" width="17" height="17" rx="1" />
    <path d="M10 8.5v7M14 8.5v7" />
  </I>
);

export const Play = (p: P) => (
  <I {...p}>
    <rect x="3.5" y="3.5" width="17" height="17" rx="1" />
    <path d="M10 8.5l5.5 3.5-5.5 3.5z" />
  </I>
);

export const Stop = (p: P) => (
  <I {...p}>
    <path d="M14 4h5v16h-5M10 8l-4 4 4 4M6 12h9" />
  </I>
);

export const Check = (p: P) => (
  <I {...p}>
    <path d="M4.5 12.5l4.5 4.5 10.5-11" />
  </I>
);

export const Cross = (p: P) => (
  <I {...p}>
    <path d="M6 6l12 12M18 6L6 18" />
  </I>
);

/** The box's voice said it: a read-back that was spoken aloud. */
export const Speaker = (p: P) => (
  <I {...p}>
    <path d="M3.5 9.5h3.5l4.5-4v13l-4.5-4H3.5z" />
    <path d="M15 9.2a4 4 0 0 1 0 5.6M17.8 6.6a7.6 7.6 0 0 1 0 10.8" />
  </I>
);

export const RevisionMark = (p: P) => (
  <I {...p}>
    <path d="M12 3.5l9.5 16.5h-19z" />
    <path d="M12 10v4.5M12 17.2v.3" />
  </I>
);

export const Person = (p: P) => (
  <I {...p}>
    <circle cx="12" cy="7.5" r="3.5" />
    <path d="M5 20.5a7 7 0 0 1 14 0" />
    <path d="M17.5 3.5l2 2M20 8h2" />
  </I>
);

export const Repeat = (p: P) => (
  <I {...p}>
    <path d="M4 11V9a3 3 0 0 1 3-3h11M15 3l3 3-3 3M20 13v2a3 3 0 0 1-3 3H6M9 21l-3-3 3-3" />
  </I>
);

export const Simplify = (p: P) => (
  <I {...p}>
    <path d="M4 6h16M4 10h11M4 14h16M4 18h7" />
  </I>
);

export const Ledger = (p: P) => (
  <I {...p}>
    <path d="M4 4v16h16" />
    <path d="M7 15l4-4 3 3 5-6" />
    <path d="M17 8h2v2" />
  </I>
);

export const Menu = (p: P) => (
  <I {...p}>
    <path d="M4 7h16M4 12h16M4 17h16" />
  </I>
);

export const Printer = (p: P) => (
  <I {...p}>
    <path d="M7 9V3.5h10V9M7 17H4.5V9.5h15V17H17" />
    <rect x="7" y="14" width="10" height="6.5" />
  </I>
);

export const Key = (p: P) => (
  <I {...p}>
    <circle cx="7.5" cy="12" r="4" />
    <path d="M11.5 12H21M17.5 12v3.5M20.5 12v2.5" />
  </I>
);

export const Shield = (p: P) => (
  <I {...p}>
    <path d="M12 3l7.5 3v6c0 4.5-3.2 7.7-7.5 9-4.3-1.3-7.5-4.5-7.5-9V6z" />
    <path d="M8.5 12l2.5 2.5 4.5-5" />
  </I>
);

export const Globe = (p: P) => (
  <I {...p}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M3.5 12h17M12 3.5c2.5 2.6 3.5 5.4 3.5 8.5s-1 5.9-3.5 8.5c-2.5-2.6-3.5-5.4-3.5-8.5s1-5.9 3.5-8.5z" />
  </I>
);

export const Chevron = (p: P) => (
  <I {...p}>
    <path d="M9 5l7 7-7 7" />
  </I>
);

export const Plug = (p: P) => (
  <I {...p}>
    <path d="M9 3v5M15 3v5M6.5 8h11v3a5.5 5.5 0 0 1-11 0zM12 16.5V21" />
  </I>
);

/** Two speech bubbles, the second answering the first (Gespraech). */
export const Chat = (p: P) => (
  <I {...p}>
    <path d="M3.5 5.5h10v7h-5.5l-3 2.5v-2.5h-1.5z" />
    <path d="M16.5 9.5h4v7h-1.5v2.5l-3-2.5h-5.5v-2" />
  </I>
);

export const ROOM_ICONS: Record<string, (p: P) => React.ReactElement> = {
  chat: Chat,
  stethoscope: Stethoscope,
  scale: Scale,
  handshake: Handshake,
};
