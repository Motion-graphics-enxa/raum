/**
 * `/start`: open a room on this box, then hand each person their code.
 * The picker is the room itself: the rooms' plates written large on the wall, the window's light standing on the
 * one you point at, the dado below the Begleitstrich repainted in that room's colour. Opening a room floods the
 * screen with its paint, which drains back down as the pass (`/start?room=<id>`) is issued: one coupon per person
 * with their join code and whether their phone is in yet, the table screen's code, and the table action.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type SVGProps } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, api, eventsSocketUrl, listOf, type ModeConfig } from "../lib/api";
import type { ParticipantInfo } from "../lib/events";
import { langName, pick } from "../lib/i18n";
import { useEventSocket } from "../lib/live";
import { RoomStore, useRoomModel } from "../lib/store";
import { QR } from "../components/QR";
import { ArrowRight, Check, Key, Pair, ROOM_ICONS } from "../components/Icons";
import "./screens.css";
import "./home.css";

type Role = "a" | "b";
type Show = Role | "t";

const still = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
const wait = (ms: number) => new Promise((done) => window.setTimeout(done, ms));
/** a room's paint token; a room added as a plugin file without its own paint wears the conversation's */
const paintOf = (id: string) => `var(--paint-${id}, var(--paint-gespraech))`;

export default function Home() {
  const [qs] = useSearchParams();
  const navigate = useNavigate();
  const room = qs.get("room");
  const [modes, setModes] = useState<ModeConfig[] | null>(null);
  const [down, setDown] = useState(false);
  const [opening, setOpening] = useState<string | null>(null);
  const [openErr, setOpenErr] = useState<string | null>(null);
  const [flood, setFlood] = useState<ModeConfig | null>(null);

  const load = useCallback(() => {
    api
      .modes()
      .then((m) => {
        setModes(listOf<ModeConfig>(m, "modes"));
        setDown(false);
      })
      .catch(() => setDown(true));
  }, []);
  useEffect(load, [load]);

  const open = async (m: ModeConfig) => {
    if (opening) return;
    const calm = still();
    setOpening(m.id);
    setOpenErr(null);
    // the room's paint rises from the dado while the box opens it; it never holds the room back more than a beat
    if (!calm) setFlood(m);
    try {
      const [r] = await Promise.all([api.createRoom(m.id), wait(calm ? 0 : 620)]);
      navigate(`/start?room=${encodeURIComponent(r.room)}`, { state: { arrive: m.id } });
    } catch (e) {
      const why = e instanceof ApiError ? ` (error ${e.status})` : "";
      setOpenErr(`The box did not open ${m.name.de}${why}. Try again.`);
    } finally {
      setOpening(null);
      setFlood(null);
    }
  };

  const mode = room && modes ? modes.find((m) => room.startsWith(`${m.prefix}-`)) : undefined;

  return (
    <div className={`screen screen--home ${mode ? `room--${mode.id}` : room ? "" : "screen--pick"}`} lang="en">
      <header className="home__bar">
        <Link to="/" className="home__mark" aria-label="RAUM home">
          <span className="home__word">RAUM</span>
        </Link>
        <span className="home__slogan">Translates. Checks. Forgets.</span>
        {room ? (
          <button type="button" className="home__end" onClick={() => navigate("/start")}>
            <ArrowRight className="icon home__back-icon" />
            All rooms
          </button>
        ) : (
          <Link className="home__end" to="/admin">
            Admin
          </Link>
        )}
      </header>

      {room ? (
        mode ? (
          <Pass key={room} room={room} mode={mode} />
        ) : (
          <main className="home">
            {modes || down ? (
              <div className="home__intro">
                <h1 className="home__h">This room is not on this box</h1>
                <p className="home__lead">
                  <span className="mono">{room}</span> does not match any room set up here. Open a new one instead.
                </p>
                <p>
                  <Link className="btn" to="/start">
                    All rooms
                  </Link>
                </p>
              </div>
            ) : null}
          </main>
        )
      ) : (
        <Picker modes={modes} down={down} reload={load} opening={opening} openErr={openErr} onOpen={open} flood={flood} />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ the picker: the rooms written on the wall */

function Picker({
  modes,
  down,
  reload,
  opening,
  openErr,
  onOpen,
  flood,
}: {
  modes: ModeConfig[] | null;
  down: boolean;
  reload: () => void;
  opening: string | null;
  openErr: string | null;
  onOpen: (m: ModeConfig) => void;
  flood: ModeConfig | null;
}) {
  const [at, setAt] = useState(0);
  const intent = useRef<number | undefined>(undefined);
  const list = useRef<HTMLUListElement>(null);
  useEffect(() => () => window.clearTimeout(intent.current), []);
  const rooms = modes ?? [];
  const here = rooms[Math.min(at, rooms.length - 1)];
  const burn = here?.retention?.auto_burn_minutes;

  // a pointer passing over the rows on its way somewhere does not repaint the room; one that rests does
  const rest = (i: number) => {
    window.clearTimeout(intent.current);
    intent.current = window.setTimeout(() => setAt(i), 90);
  };
  const leave = () => window.clearTimeout(intent.current);
  const step = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End") return;
    const rows = Array.from(list.current?.querySelectorAll<HTMLButtonElement>(".pick__row") ?? []);
    if (!rows.length) return;
    e.preventDefault();
    const now = rows.indexOf(document.activeElement as HTMLButtonElement);
    const next =
      e.key === "Home"
        ? 0
        : e.key === "End"
          ? rows.length - 1
          : (now + (e.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length;
    rows[next]?.focus();
  };

  return (
    <main
      className={`pick ${flood ? "is-flooding" : ""}`}
      style={{ "--stage": paintOf(here?.id ?? "gespraech") } as React.CSSProperties}
    >
      <div className="pick__wall">
        <div className="pick__intro">
          <h1 className="pick__h">Open a room</h1>
          <p className="pick__lead">Choose the room for this conversation.</p>
        </div>
        {down ? (
          <div className="banner banner--bad pick__note" role="alert">
            <span>The box is not answering, so no room can open. Check that RAUM is running on this box.</span>
            <button type="button" className="banner__btn" onClick={reload}>
              Try again
            </button>
          </div>
        ) : null}
        {openErr ? (
          <p className="banner banner--bad pick__note" role="alert">
            <span>{openErr}</span>
          </p>
        ) : null}
        {modes && modes.length === 0 ? (
          <p className="pick__lead">
            No rooms are set up on this box yet. Each room is one file in <span className="mono">modes/</span>.
          </p>
        ) : null}

        <div
          className="pick__rows"
          style={{ "--n": Math.max(rooms.length, 1), "--at": Math.min(at, Math.max(rooms.length - 1, 0)) } as React.CSSProperties}
        >
          {modes && modes.length ? <span className="pick__sun" aria-hidden="true" /> : null}
          <ul ref={list} className="pick__list" aria-busy={!modes && !down} aria-label="Rooms on this box" onKeyDown={step}>
            {modes
              ? modes.map((m, i) => {
                  const Icon = ROOM_ICONS[m.icon] ?? ROOM_ICONS.chat;
                  const busy = opening === m.id;
                  return (
                    <li key={m.id} className="pick__item" style={{ "--i": i, "--paint": paintOf(m.id) } as React.CSSProperties}>
                      <button
                        type="button"
                        className={`pick__row ${i === at ? "is-on" : ""} ${busy ? "is-busy" : ""}`}
                        onClick={() => onOpen(m)}
                        onFocus={() => setAt(i)}
                        onPointerEnter={() => rest(i)}
                        onPointerLeave={leave}
                        onPointerDown={() => setAt(i)}
                        disabled={!!opening && !busy}
                        aria-busy={busy}
                      >
                        <span className="pick__plate" lang="de">
                          {m.name.de}
                        </span>
                        <span className="pick__meta">
                          <span className="pick__name">
                            <span className="pick__swatch" aria-hidden="true" />
                            <Icon className="icon pick__icon" />
                            {pick(m.name)}
                          </span>
                          <span className="pick__roles">
                            {pick(m.roles.a.label)}
                            <Pair className="icon pick__pair" />
                            {pick(m.roles.b.label)}
                          </span>
                          {m.tagline ? <span className="pick__checks">{pick(m.tagline)}</span> : null}
                        </span>
                        <span className="pick__go">
                            {busy ? (
                              <>
                                <span className="pick__ring" aria-hidden="true" />
                                Opening…
                              </>
                            ) : (
                              <>
                                Open room
                                <ArrowRight className="icon pick__arrow" />
                              </>
                            )}
                        </span>
                        <ArrowRight className="icon pick__tap" />
                      </button>
                    </li>
                  );
                })
              : down
                ? null
                : [62, 44, 88, 48].map((w, i) => (
                    <li key={i} className="pick__item" aria-hidden="true" style={{ "--i": i } as React.CSSProperties}>
                      <span className="pick__ghost" style={{ "--w": `${w}%` } as React.CSSProperties} />
                    </li>
                  ))}
          </ul>
          {!modes && !down ? <p className="visually-hidden">Loading the rooms on this box…</p> : null}
        </div>
      </div>

      <footer className="pick__dado">
        <div className="pick__dado-in">
          <p className="pick__say">Each person then scans their own code. RAUM takes the language from their phone.</p>
          <p className="pick__burn">
            <Key className="icon" />
            {here && burn ? `${here.name.de} burns itself after ${burn} idle minutes. ` : ""}Nothing leaves this box.
          </p>
        </div>
      </footer>

      {flood ? (
        <div className="pick__flood" style={{ "--paint": paintOf(flood.id) } as React.CSSProperties} aria-hidden="true">
          <span className="pick__flood-plate" lang="de">
            {flood.name.de}
          </span>
          <span className="pick__flood-say">
            <span className="pick__ring" />
            Opening the room
          </span>
        </div>
      ) : null}
      <p className="visually-hidden" role="status">
        {opening ? "Opening the room…" : ""}
      </p>
    </main>
  );
}

/* ------------------------------------------------------------------ the pass: the opened room's codes */

function Pass({ room, mode }: { room: string; mode: ModeConfig }) {
  const store = useMemo(() => new RoomStore(room), [room]);
  const link = useEventSocket(room, store, eventsSocketUrl(room));
  const m = useRoomModel(store);
  const people = Object.values(m.participants);
  const inRoom: Record<Role, ParticipantInfo | undefined> = {
    a: people.find((p) => p.role === "a"),
    b: people.find((p) => p.role === "b"),
  };
  const count = (inRoom.a ? 1 : 0) + (inRoom.b ? 1 : 0);
  const label = (r: Role) => pick(mode.roles[r].label);
  const [show, setShow] = useState<Show>("a");

  // On a phone one code shows at a time: once its person is in, the other person's code comes up.
  const aIn = !!inRoom.a;
  const bIn = !!inRoom.b;
  useEffect(() => {
    const other: Role | null = show === "a" && aIn && !bIn ? "b" : show === "b" && bIn && !aIn ? "a" : null;
    if (!other) return;
    const id = window.setTimeout(() => setShow(other), 900);
    return () => window.clearTimeout(id);
  }, [show, aIn, bIn]);

  const burn = mode.retention?.auto_burn_minutes;
  // coming from the picker, the room's paint still fills the screen and drains down into the pass's dado
  const arrive = (useLocation().state as { arrive?: string } | null)?.arrive === mode.id;
  const status =
    link !== "connected" && count === 0
      ? "Connecting to the room…"
      : count === 2
        ? "Both are in the room"
        : count === 1
          ? `${label(inRoom.a ? "a" : "b")} is in · waiting for the ${say(label(inRoom.a ? "b" : "a"))}`
          : "Waiting for both phones";

  return (
    <main className="home home--pass">
      {arrive ? (
        <div className="pass-curtain" aria-hidden="true">
          <span className="pass-curtain__plate" lang="de">
            {mode.name.de}
          </span>
        </div>
      ) : null}
      <article className="pass" aria-labelledby="pass-title">
        <header className="pass__head">
          <div className="pass__title">
            <h1 id="pass-title" className="pass__plate" lang="de">
              {mode.name.de}
            </h1>
            <p className="pass__gloss">
              {pick(mode.name)}
              {mode.tagline ? <span className="pass__tag"> · {pick(mode.tagline)}</span> : null}
            </p>
          </div>
          <p className="pass__no">
            <span className="pass__no-l">Room</span>
            <span className="pass__id mono">{room}</span>
          </p>
          <p className={`pass__status ${count === 2 ? "is-in" : ""}`} role="status">
            {count === 2 ? <Check className="icon" /> : <span className="pass__dot" aria-hidden="true" />}
            {status}
          </p>
        </header>

        <div className="pass__switch" role="group" aria-label="Show the code for">
          {(["a", "b", "t"] as const).map((k) => (
            <button key={k} type="button" aria-pressed={show === k} onClick={() => setShow(k)}>
              {k === "t" ? "Table" : label(k)}
              {k !== "t" && inRoom[k] ? <Check className="icon" aria-label="joined" /> : null}
            </button>
          ))}
        </div>

        <div className="pass__codes" data-show={show}>
          {(["a", "b"] as const).map((r) => (
            <Coupon key={r} role={r} room={room} mode={mode} who={inRoom[r]} label={label(r)} />
          ))}
          <section className="coupon coupon--t" data-k="t" aria-labelledby="coupon-t">
            <div className="coupon__head">
              <h2 className="coupon__table" id="coupon-t">
                Table screen
              </h2>
            </div>
            <QR value={`${location.origin}/t/${room}`} size={168} label="Code for the table screen" mark={mode.id} />
            <p className="coupon__say">
              The conversation in both languages and every check, for the screen everyone can see.
            </p>
            <div className="coupon__acts">
              <CopyLink text={`${location.origin}/t/${room}`} />
            </div>
            <p className="coupon__url mono">
              {location.host}/t/{room}
            </p>
          </section>
        </div>

        <footer className="pass__dado">
          <a className="btn btn--wall pass__table" href={`/t/${room}`} target="_blank" rel="noreferrer">
            Open the table screen
            <OpenIcon />
          </a>
          <p className="pass__burn">
            {burn ? (
              <>
                <Key className="icon" /> Burns itself after {burn} idle minutes. Nothing leaves this box.
              </>
            ) : (
              "Nothing leaves this box."
            )}
          </p>
        </footer>
      </article>
    </main>
  );
}

function Coupon({
  role,
  room,
  mode,
  who,
  label,
}: {
  role: Role;
  room: string;
  mode: ModeConfig;
  who?: ParticipantInfo;
  label: string;
}) {
  const path = `/j/${room}?role=${role}`;
  const url = `${location.origin}${path}`;
  // the other person can also type the short address (`/:room` joins as role b)
  const typed = role === "b" ? `${location.host}/${room}` : `${location.host}${path}`;
  return (
    <section
      className={`coupon coupon--${role} ${who ? "is-in" : ""}`}
      data-k={role}
      aria-labelledby={`coupon-${role}`}
    >
      <div className="coupon__head">
        <h2 className="coupon__who" id={`coupon-${role}`}>
          {label}
        </h2>
        <span className="coupon__state" aria-live="polite">
          {who ? (
            <>
              <Check className="icon" />
              Joined · {langName(who.lang, "en")}
            </>
          ) : (
            <>
              <span className="coupon__ring" aria-hidden="true" />
              Waiting
            </>
          )}
        </span>
      </div>
      <QR value={url} size={248} label={`Join code for the ${say(label)}`} mark={mode.id} />
      <p className="coupon__say">Scan with the {say(label)}’s phone.</p>
      <div className="coupon__acts">
        <a className="coupon__act" href={path} target="_blank" rel="noreferrer">
          <OpenIcon />
          Join on this device
        </a>
        <CopyLink text={url} />
      </div>
      <p className="coupon__url mono">{typed}</p>
    </section>
  );
}

/** A role inside a sentence: "the doctor's phone", but "the CEO's phone". */
function say(label: string): string {
  return /^[A-Z0-9]{2,}$/.test(label) ? label : label.charAt(0).toLowerCase() + label.slice(1);
}

function CopyLink({ text }: { text: string }) {
  const [state, setState] = useState<"idle" | "done" | "fail">("idle");
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const copy = async () => {
    const ok = await copyText(text);
    setState(ok ? "done" : "fail");
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setState("idle"), 1800);
  };
  return (
    <button type="button" className={`coupon__act ${state === "done" ? "is-done" : ""}`} onClick={() => void copy()}>
      {state === "done" ? <Check className="icon" /> : <CopyIcon />}
      <span aria-live="polite">{state === "done" ? "Copied" : state === "fail" ? "Copy failed" : "Copy link"}</span>
    </button>
  );
}

/** The clipboard API needs a secure context; the box is often reached over plain http on the LAN. */
async function copyText(text: string): Promise<boolean> {
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the old way */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

/* drawn like the rest of Icons.tsx: 24 grid, one stroke */
const CopyIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg viewBox="0 0 24 24" className="icon" aria-hidden="true" {...p}>
    <rect x="8.5" y="8.5" width="11" height="11" rx="2" />
    <path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2" />
  </svg>
);
const OpenIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg viewBox="0 0 24 24" className="icon" aria-hidden="true" {...p}>
    <path d="M13.5 4.5h6v6M19.5 4.5l-8.5 8.5M17.5 14v4a1.5 1.5 0 0 1-1.5 1.5H6A1.5 1.5 0 0 1 4.5 18V8A1.5 1.5 0 0 1 6 6.5h4" />
  </svg>
);
