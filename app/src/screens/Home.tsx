/**
 * `/start`: open a room on this box, then hand each person their code.
 * The rooms are set as an index: each German plate large on the wall, what it is for beside it; pointing at a
 * line raises the room's paint behind it. Picking a line opens the room and issues its pass (`/start?room=<id>`):
 * one coupon per person with their join code and whether their phone is in yet, the table screen's code, and the
 * table action on the room's paint.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type SVGProps } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, api, eventsSocketUrl, listOf, type ModeConfig } from "../lib/api";
import type { ParticipantInfo } from "../lib/events";
import { langName, pick } from "../lib/i18n";
import { useEventSocket } from "../lib/live";
import { RoomStore, useRoomModel } from "../lib/store";
import { QR } from "../components/QR";
import { ArrowRight, Check, Key, Pair } from "../components/Icons";
import { Hatch } from "../components/Drawing";
import "./screens.css";
import "./home.css";

type Role = "a" | "b";
type Show = Role | "t";

export default function Home() {
  const [qs] = useSearchParams();
  const navigate = useNavigate();
  const room = qs.get("room");
  const [modes, setModes] = useState<ModeConfig[] | null>(null);
  const [down, setDown] = useState(false);
  const [opening, setOpening] = useState<string | null>(null);
  const [openErr, setOpenErr] = useState<string | null>(null);

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
    setOpening(m.id);
    setOpenErr(null);
    try {
      const r = await api.createRoom(m.id);
      navigate(`/start?room=${encodeURIComponent(r.room)}`);
    } catch (e) {
      const why = e instanceof ApiError ? ` (error ${e.status})` : "";
      setOpenErr(`The box did not open ${m.name.de}${why}. Try again.`);
    } finally {
      setOpening(null);
    }
  };

  const mode = room && modes ? modes.find((m) => room.startsWith(`${m.prefix}-`)) : undefined;

  return (
    <div className={`screen screen--home ${mode ? `room--${mode.id}` : ""}`} lang="en">
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
        <main className="home">
          <div className="home__intro home__intro--index">
            <h1 className="home__h">Open a room</h1>
            <p className="home__lead">
              Choose the room for this conversation. Each person then scans their own code; RAUM takes the language
              from their phone.
            </p>
          </div>
          {down ? (
            <div className="banner banner--bad home__note" role="alert">
              <span>The box is not answering, so no room can open. Check that RAUM is running on this box.</span>
              <button type="button" className="banner__btn" onClick={load}>
                Try again
              </button>
            </div>
          ) : null}
          {openErr ? (
            <p className="banner banner--bad home__note" role="alert">
              <span>{openErr}</span>
            </p>
          ) : null}
          <ul className="rooms" aria-busy={!modes && !down} aria-label="Rooms on this box">
            {modes
              ? modes.map((m, i) => (
                  <li key={m.id} style={{ "--i": i } as React.CSSProperties}>
                    <RoomLine mode={m} busy={opening === m.id} locked={!!opening} onOpen={() => void open(m)} />
                  </li>
                ))
              : down
                ? null
                : [62, 38, 88, 46].map((w, i) => (
                    <li key={i} aria-hidden="true" style={{ "--i": i } as React.CSSProperties}>
                      <span className="line line--ghost">
                        <span className="line__layer">
                          <span className="line__plate" style={{ "--w": `${w}%` } as React.CSSProperties} />
                          <span className="line__meta">
                            <span />
                            <span />
                          </span>
                        </span>
                      </span>
                    </li>
                  ))}
          </ul>
          {modes && modes.length > 0 ? (
            <p className="rooms__foot">
              Nothing leaves this box. Each room is one file in <span className="mono">modes/</span>.
            </p>
          ) : null}
          {!modes && !down ? <p className="visually-hidden">Loading the rooms on this box…</p> : null}
          {modes && modes.length === 0 ? (
            <p className="home__lead">
              No rooms are set up on this box yet. Each room is one file in <span className="mono">modes/</span>.
            </p>
          ) : null}
        </main>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ the picker: the index of rooms */

/**
 * One line of the index. The German plate is set large on the wall; what the room is for stands in the column
 * beside it. Pointing at a line (or focusing it, or opening it) draws the Begleitstrich along its foot, then the
 * room's paint rises behind the line with the strich riding on top, and the capitals turn white as it passes.
 * The painted copy is a second, identical layer clipped from below, so the type never moves.
 */
function RoomLine({
  mode,
  busy,
  locked,
  onOpen,
}: {
  mode: ModeConfig;
  busy: boolean;
  locked: boolean;
  onOpen: () => void;
}) {
  const burn = mode.retention?.auto_burn_minutes;
  const layer = (painted: boolean) => (
    <span className={`line__layer ${painted ? "line__paint" : "line__face"}`} aria-hidden={painted || undefined}>
      <span className="line__plate" lang="de">
        {mode.name.de}
      </span>
      <span className="line__meta">
        <span className="line__name">
          <Hatch mode={mode.id} size={18} className="line__mark" />
          {pick(mode.name)}
        </span>
        <span className="line__roles">
          {pick(mode.roles.a.label)}
          <Pair className="icon line__pair" />
          {pick(mode.roles.b.label)}
        </span>
        {mode.tagline ? <span className="line__checks">{pick(mode.tagline)}</span> : null}
        {burn ? (
          <span className="line__burn">
            <Key className="icon" /> Burns after {burn} idle minutes
          </span>
        ) : null}
      </span>
      <span className="line__go">
        {busy ? (
          <>
            <span className="line__ring" aria-hidden="true" />
            <span className="line__go-l">Opening…</span>
          </>
        ) : (
          <>
            <span className="line__go-l">Open room</span>
            <ArrowRight className="icon line__arrow" />
          </>
        )}
      </span>
    </span>
  );
  return (
    <button
      type="button"
      className={`line line--${mode.id} ${busy ? "is-busy" : ""}`}
      onClick={onOpen}
      disabled={locked}
      aria-busy={busy}
    >
      {layer(false)}
      {layer(true)}
      <span className="line__strich" aria-hidden="true" />
    </button>
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
