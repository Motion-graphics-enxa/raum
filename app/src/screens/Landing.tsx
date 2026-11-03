/** `/`: the RAUM website. A front page: the tagline over its German, the lede, and the index of three rooms where a conversation must not leave. */
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Hatch } from "../components/Drawing";
import {
  ArrowRight,
  Check,
  Handshake,
  Scale,
  Stethoscope,
} from "../components/Icons";
import "./landing.css";

type Lang = "en" | "ru";
type RoomId = "sprechzimmer" | "kanzlei" | "verhandlungsraum";

interface Fact {
  value: Record<Lang, string>;
  kind: string;
}
interface Room {
  id: RoomId;
  plate: string;
  gloss: string;
  people: string;
  guard: string;
  de: string;
  said: string;
  tr: Record<Lang, string>;
  facts: Fact[];
  /** the checked figure as it stands in the translation, underlined on the front page */
  hit: Record<Lang, string>;
  leave: string;
  Icon: typeof Stethoscope;
}

const ROOMS: Room[] = [
  {
    id: "sprechzimmer",
    plate: "Sprechzimmer",
    gloss: "Consultation room",
    people: "Doctor and patient",
    guard: "Doses and allergies are checked, and read back when it matters.",
    said: "Doctor",
    de: "Nehmen Sie zweimal täglich 500 mg Metformin nach dem Essen.",
    tr: {
      en: "Take 500 mg of metformin twice a day, after meals.",
      ru: "Принимайте 500 мг метформина два раза в день после еды.",
    },
    facts: [
      { value: { en: "500 mg", ru: "500 мг" }, kind: "Dose" },
      { value: { en: "twice a day", ru: "2 раза в день" }, kind: "Frequency" },
      { value: { en: "Metformin", ru: "Метформин" }, kind: "Medication" },
    ],
    hit: { en: "500 mg", ru: "500 мг" },
    leave:
      "A take-home summary in the patient's language and a German file note.",
    Icon: Stethoscope,
  },
  {
    id: "kanzlei",
    plate: "Kanzlei",
    gloss: "Law office",
    people: "Lawyer and client",
    guard:
      "Deadlines and dates are checked; 'explain simply' only goes out once the lawyer approves it.",
    said: "Lawyer",
    de: "Die Frist für Ihren Widerspruch endet am 14. März.",
    tr: {
      en: "The deadline for your objection ends on 14 March.",
      ru: "Срок подачи вашего возражения истекает 14 марта.",
    },
    facts: [
      { value: { en: "14 March", ru: "14 марта" }, kind: "Deadline" },
      { value: { en: "Objection", ru: "Возражение" }, kind: "Term" },
    ],
    hit: { en: "14 March", ru: "14 марта" },
    leave:
      "German case notes and the client's copy, both with every date checked.",
    Icon: Scale,
  },
  {
    id: "verhandlungsraum",
    plate: "Verhandlungsraum",
    gloss: "Negotiation room",
    people: "CEO and foreign partner",
    guard:
      "Every offer is tracked with its amount and terms; the deal glossary keeps names exact.",
    said: "CEO",
    de: "Unser letztes Angebot: 2,4 Millionen Euro, Lieferung im Juni.",
    tr: {
      en: "Our final offer: 2.4 million euros, delivery in June.",
      ru: "Наше последнее предложение: 2,4 миллиона евро, поставка в июне.",
    },
    facts: [
      { value: { en: "€2.4 m", ru: "2,4 млн €" }, kind: "Amount" },
      { value: { en: "June", ru: "июнь" }, kind: "Delivery" },
    ],
    hit: { en: "2.4 million euros", ru: "2,4 миллиона евро" },
    leave: "Minutes both sides approve before anyone leaves the table.",
    Icon: Handshake,
  },
];

/** Plays once each time it scrolls into view (and never under reduced motion). */
function useInView<T extends Element>(threshold = 0.4) {
  const ref = useRef<T>(null);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (typeof IntersectionObserver === "undefined") {
      setSeen(true);
      return;
    }
    const io = new IntersectionObserver(
      (es) => {
        for (const e of es) if (e.isIntersecting) setSeen(true);
      },
      { threshold },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [threshold]);
  return [ref, seen] as const;
}

const reduced = () =>
  typeof matchMedia !== "undefined" &&
  matchMedia("(prefers-reduced-motion: reduce)").matches;

export default function Landing() {
  const [lang, setLang] = useState<Lang>("en");

  useEffect(() => {
    document.title = "RAUM: a private interpreter that lives in the room";
  }, []);

  return (
    <div className="screen landing" lang="en">
      <header className="lnav">
        <a className="lnav__word" href="#top" aria-label="RAUM, to the top">
          RAUM
        </a>
        <nav className="lnav__links" aria-label="Sections">
          <a href="#check">How it checks</a>
          <a href="#rooms">The rooms</a>
          <a href="#privacy">Privacy</a>
        </nav>
        <LangSwitch lang={lang} onChange={setLang} className="lswitch--nav" />
        <Link className="btn lnav__cta" to="/start">
          Open a room
          <ArrowRight />
        </Link>
      </header>

      <main id="top">
        {/* ------------------------------------------------ the front page */}
        {/* Set like a front page: the tagline as the headline, the German it was said in under each line (the
            interpreter's own interlinear), one walnut rule, then the lede and the index of the three rooms. */}
        <section className="lede" aria-labelledby="lede-h">
          <div className="lede__top">
            <h1 id="lede-h" className="lede__h">
              <span className="lede__row">
                <span className="lede__en">
                  <span className="lede__w">What’s said</span>{" "}
                  <span className="lede__w lede__w--room">in the room</span>
                </span>
                <span className="lede__de" lang="de" aria-hidden="true">
                  Was im Raum gesagt wird,
                </span>
              </span>{" "}
              <span className="lede__row lede__row--2">
                <span className="lede__en">
                  <span className="lede__w">stays</span>{" "}
                  <span className="lede__w lede__w--room">
                    in the room<span className="lede__stop">.</span>
                  </span>
                </span>
                <span className="lede__de" lang="de" aria-hidden="true">
                  bleibt im Raum.
                </span>
              </span>
            </h1>
            <p className="lede__ok">
              <span className="lede__tick" aria-hidden="true">
                <Check />
              </span>
              <span>
                <b>Checked</b>
                <span>Said in German, translated into English</span>
              </span>
            </p>
          </div>

          <div className="lede__base">
            <div className="lede__intro">
              <p className="lede__p">
                <span className="lede__runin">RAUM</span> is a private AI interpreter for the rooms where a misheard
                number is a legal problem. It translates both ways, checks every
                figure, and forgets. <strong>Nothing leaves the box.</strong>
              </p>
              <div className="lede__act">
                <Link className="btn btn--lg lede__cta" to="/start">
                  Open a room
                  <ArrowRight />
                </Link>
                <LangSwitch
                  lang={lang}
                  onChange={setLang}
                  className="lswitch--hero"
                />
              </div>
            </div>

            <nav className="index" aria-labelledby="index-h">
              <h2 id="index-h" className="visually-hidden">
                The rooms
              </h2>
              <ul className="index__list">
                {ROOMS.map((r, i) => (
                  <li
                    key={r.id}
                    className={`index__row room--${r.id}`}
                    style={{ "--i": i } as React.CSSProperties}
                  >
                    <Link className="index__link" to="/start">
                      <span className="index__room">
                        <span className="index__name">
                          <Hatch mode={r.id} size={26} className="index__mark" />
                          <span lang="de">{r.plate}</span>
                        </span>
                        <span className="index__gloss">
                          {r.gloss} · {r.people}
                        </span>
                      </span>
                      <span className="index__line">
                        <span className="index__said" lang={lang}>
                          <Marked text={r.tr[lang]} hit={r.hit[lang]} />
                        </span>
                        <span className="index__fact">
                          <Check /> {r.facts[0].kind} checked
                        </span>
                      </span>
                      <span className="index__go" aria-hidden="true">
                        <ArrowRight />
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            </nav>
          </div>
        </section>

        {/* ------------------------------------------------ today */}
        <section className="ltoday" aria-labelledby="ltoday-h">
          <h2 id="ltoday-h" className="lsec__h">
            Today the interpreter is a relative, a phone app, or nobody.
          </h2>
          <ul className="ltoday__facts">
            <li>
              Statutory health insurance pays{" "}
              <span className="ltoday__fig">nothing</span> for an interpreter.
            </li>
            <li>
              At weekends, only <span className="ltoday__fig">14 of 38</span>{" "}
              German university hospitals can get one.
            </li>
            <li>
              Yet about <span className="ltoday__fig">15&nbsp;%</span> of people
              in Germany mainly speak another language at home.
            </li>
          </ul>
          <p className="ltoday__law">
            And sending these conversations to a cloud is its own legal risk:
            §203 StGB for doctors, §43e BRAO for lawyers, GeschGehG §2 for trade
            secrets.
          </p>
        </section>

        {/* ------------------------------------------------ the check */}
        <section id="check" className="lcheck" aria-labelledby="lcheck-h">
          <h2 id="lcheck-h" className="lsec__h lsec__h--big">
            Any AI can translate.
            <br />
            RAUM checks.
          </h2>
          <p className="lsec__lead">
            Code compares every number, unit, date, name and medication between
            what was said and what was translated. A second model from a
            different family translates it back and scores the meaning. A human
            confirms what matters.
          </p>
          <CheckDemo lang={lang} />
        </section>

        {/* ------------------------------------------------ the rooms */}
        <section id="rooms" className="lrooms" aria-label="The rooms">
          {ROOMS.map((r) => (
            <RoomStory key={r.id} room={r} lang={lang} />
          ))}
        </section>

        {/* ------------------------------------------------ privacy */}
        <section id="privacy" className="lpriv" aria-labelledby="lpriv-h">
          <div className="lpriv__text">
            <h2 id="lpriv-h" className="lsec__h lsec__h--big">
              Seven models.
              <br />
              One box.
            </h2>
            <p className="lsec__lead">
              Speech recognition, translation, a checker from a different model
              family, meaning search, voice: all on one ASUS Ascent GX10 in the
              room. Pull the internet cable and the conversation goes on.
            </p>
            <ul className="lpriv__models">
              <li>Whisper large-v3-turbo</li>
              <li>Gemma 4</li>
              <li>gpt-oss</li>
              <li>bge-m3</li>
              <li>Silero VAD</li>
              <li>DeepFilterNet</li>
              <li>Piper</li>
            </ul>
          </div>
          <Cable />
        </section>

        {/* ------------------------------------------------ forgets */}
        <Forget lang={lang} />

        {/* ------------------------------------------------ close */}
        <section className="lclose" aria-labelledby="lclose-h">
          <h2 id="lclose-h" className="lclose__h">
            Translates. Checks. Forgets.
          </h2>
          <p className="lclose__p">
            Scan the QR code on the box, choose your language, and talk. No
            account, no app, no cloud.
          </p>
          <Link className="btn btn--lg lclose__cta" to="/start">
            Open a room
          </Link>
        </section>
      </main>

      <footer className="lfoot">
        <span className="lfoot__word">RAUM</span>
        <span>
          A private AI interpreter that lives in the room. Macromedia Berlin
          Hackathon 2026.
        </span>
        <span className="lfoot__links">
          <Link to="/start">Rooms</Link>
          <Link to="/admin">Admin</Link>
        </span>
      </footer>
    </div>
  );
}

/** The sentence with its checked figure underlined in the checked pen (a solid line: the fact chip's "checked"). */
function Marked({ text, hit }: { text: string; hit: string }) {
  const at = text.indexOf(hit);
  if (at < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)}
      <span className="index__hit">{hit}</span>
      {text.slice(at + hit.length)}
    </>
  );
}

/** Two copies render (the bar's on wide screens, the hero's on narrow ones); CSS shows one at a time. */
function LangSwitch({
  lang,
  onChange,
  className = "",
}: {
  lang: Lang;
  onChange: (l: Lang) => void;
  className?: string;
}) {
  return (
    <div
      className={`lswitch ${className}`}
      role="radiogroup"
      aria-label="Example translations in"
    >
      <span className="lswitch__label">Examples in</span>
      {(["en", "ru"] as const).map((l) => (
        <button
          key={l}
          type="button"
          role="radio"
          aria-checked={lang === l}
          className={lang === l ? "is-on" : ""}
          onClick={() => onChange(l)}
        >
          {l === "en" ? "English" : "Русский"}
        </button>
      ))}
    </div>
  );
}

/* ---------------------------------------------------------------- the check, played live */

const DEMO = {
  de: "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.",
  wrong: {
    en: "Come back on Thursday, 13 November, at 9.",
    ru: "Приходите в четверг, 13 ноября, в 9 часов.",
  },
  right: {
    en: "Come back on Thursday, 12 November, at 9 a.m.",
    ru: "Приходите в четверг, 12 ноября, в 9 часов.",
  },
  said: { en: "12 November", ru: "12 ноября" },
  got: { en: "13 November", ru: "13 ноября" },
};
const STAGES = [
  "Heard",
  "Cleaned",
  "Transcribed",
  "Translated",
  "Checked",
  "Corrected",
  "Spoken",
];

function CheckDemo({ lang }: { lang: Lang }) {
  const [ref, seen] = useInView<HTMLDivElement>(0.45);
  const [step, setStep] = useState(0); // 0 idle … 7 spoken
  const [words, setWords] = useState(0);
  const deWords = DEMO.de.split(" ");

  useEffect(() => {
    if (!seen) return;
    if (reduced()) {
      setWords(deWords.length);
      setStep(7);
      return;
    }
    const ts: number[] = [];
    const at = (ms: number, fn: () => void) =>
      ts.push(window.setTimeout(fn, ms));
    setStep(1);
    setWords(0);
    deWords.forEach((_, i) => at(120 + i * 150, () => setWords(i + 1)));
    const t0 = 120 + deWords.length * 150;
    at(t0 + 100, () => setStep(2));
    at(t0 + 400, () => setStep(3));
    at(t0 + 800, () => setStep(4));
    at(t0 + 1700, () => setStep(5));
    at(t0 + 3000, () => setStep(6));
    at(t0 + 3800, () => setStep(7));
    return () => ts.forEach(clearTimeout);
  }, [seen]); // eslint-disable-line react-hooks/exhaustive-deps

  const translated = step >= 4;
  const caught = step === 5;
  const fixed = step >= 6;
  const replay = () => {
    setStep(0);
    setWords(0);
    window.setTimeout(() => {
      setWords(deWords.length);
      setStep(4);
      window.setTimeout(() => setStep(5), 700);
      window.setTimeout(() => setStep(6), 2000);
      window.setTimeout(() => setStep(7), 2700);
    }, 60);
  };

  return (
    <div className="demo" ref={ref}>
      <ol className="demo__rail" aria-label="Stages">
        {STAGES.map((s, i) => (
          <li
            key={s}
            className={`${step > i ? "is-done" : ""} ${step === i + 1 ? "is-now" : ""} ${s === "Corrected" ? "is-fix" : ""}`}
          >
            <span className="demo__dot" aria-hidden="true" />
            {s}
          </li>
        ))}
      </ol>
      <div
        className={`demo__sheet ${fixed ? "is-final" : "is-draft"} ${caught ? "is-red" : ""}`}
      >
        <div className="demo__gutter">
          <span className="demo__plate">Doctor · German</span>
          <span
            className={`demo__mark ${fixed ? "is-ok" : caught ? "is-bad" : translated ? "is-pending" : "is-live"}`}
            role="status"
          >
            {fixed ? (
              <>
                <Check /> Checked · 780 ms
              </>
            ) : caught ? (
              "Mismatch caught"
            ) : translated ? (
              "Checking…"
            ) : (
              "Listening"
            )}
          </span>
        </div>
        <p className="demo__de" lang="de">
          {deWords.slice(0, words).join(" ")}
          {words < deWords.length ? (
            <span className="demo__caret" aria-hidden="true" />
          ) : null}
        </p>
        <p
          className={`demo__tr ${translated ? "is-on" : ""}`}
          lang={lang}
          aria-live="polite"
        >
          {translated ? (fixed ? DEMO.right[lang] : DEMO.wrong[lang]) : " "}
        </p>
        <div className={`demo__facts ${translated ? "is-on" : ""}`} lang={lang}>
          <span className="demo__chip demo__chip--ok">
            <b>{lang === "en" ? "Thursday" : "четверг"}</b>
            <small>Day</small>
          </span>
          <span
            className={`demo__chip ${fixed ? "demo__chip--ok" : caught ? "demo__chip--bad" : "demo__chip--pending"}`}
          >
            <b>
              {DEMO.said[lang]}
              {caught ? (
                <>
                  <i aria-hidden="true"> ≠ </i>
                  <s>{DEMO.got[lang]}</s>
                </>
              ) : null}
            </b>
            <small>{caught ? "Said ≠ translated" : "Date"}</small>
          </span>
          <span className="demo__chip demo__chip--ok">
            <b>{lang === "en" ? "9 a.m." : "9 часов"}</b>
            <small>Time</small>
          </span>
        </div>
        <p className="demo__note">
          Example. A first translation wrote 13 instead of 12; code compared the
          dates, flagged it, and the fixed sentence went out.
        </p>
      </div>
      <button
        type="button"
        className="demo__replay"
        onClick={replay}
        disabled={step > 0 && step < 7}
      >
        Play it again
      </button>
    </div>
  );
}

/* ---------------------------------------------------------------- one room's story */

function RoomStory({ room, lang }: { room: Room; lang: Lang }) {
  const [ref, seen] = useInView<HTMLElement>(0.3);
  const { Icon } = room;
  return (
    <article
      id={`room-${room.id}`}
      ref={ref}
      className={`lroom room--${room.id} ${seen ? "is-seen" : ""}`}
      aria-labelledby={`lroom-${room.id}`}
    >
      <div className="lroom__wall">
        <span className="lroom__icon" aria-hidden="true">
          <Icon />
        </span>
        <h3 id={`lroom-${room.id}`} className="lroom__plate" lang="de">
          {room.plate}
        </h3>
        <p className="lroom__gloss">
          {room.gloss} · {room.people}
        </p>
        <p className="lroom__guard">{room.guard}</p>
      </div>
      <div className="lroom__sheet">
        <span className="lroom__said">{room.said} said, in German</span>
        <p className="lroom__de" lang="de">
          {room.de}
        </p>
        <p className="lroom__tr" lang={lang}>
          {room.tr[lang]}
        </p>
        <ul className="lroom__facts" aria-label="Checked facts" lang={lang}>
          {room.facts.map((f, i) => (
            <li
              key={f.kind}
              style={{ "--d": `${i * 90}ms` } as React.CSSProperties}
            >
              <b>{f.value[lang]}</b>
              <small>
                <Check /> {f.kind}
              </small>
            </li>
          ))}
        </ul>
        <p className="lroom__leave">
          <strong>You leave with:</strong> {room.leave}
        </p>
      </div>
    </article>
  );
}

/* ---------------------------------------------------------------- pull the cable */

function Cable() {
  const [pulled, setPulled] = useState(false);
  const [kb, setKb] = useState(18.4);
  const [drag, setDrag] = useState(0);
  const start = useRef<number | null>(null);
  const moved = useRef(false);
  useEffect(() => {
    const id = window.setInterval(
      () => setKb((k) => Math.round((k + 0.3 + Math.random() * 0.6) * 10) / 10),
      900,
    );
    return () => window.clearInterval(id);
  }, []);
  const onDown = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (pulled) return;
    start.current = e.clientX;
    moved.current = false;
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onMove = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (start.current == null || pulled) return;
    const d = Math.max(0, Math.min(140, e.clientX - start.current));
    if (d > 4) moved.current = true;
    setDrag(d);
    if (d > 110) {
      setPulled(true);
      start.current = null;
    }
  };
  const onUp = () => {
    start.current = null;
    if (!pulled) setDrag(0);
  };
  return (
    <div className={`cable ${pulled ? "is-pulled" : ""}`}>
      <div className="cable__box" aria-hidden="true">
        <span className="cable__label">ASUS Ascent GX10</span>
        <span className="cable__leds">
          <i className="is-on" />
          <i className="is-on" />
          <i className={pulled ? "" : "is-on"} />
        </span>
        <span className="cable__port" />
      </div>
      <svg className="cable__wire" viewBox="0 0 400 120" aria-hidden="true">
        <path
          d={`M40 60 C ${120 + drag} ${pulled ? 110 : 60}, ${220 + drag} ${pulled ? 120 : 60}, ${300 + drag} 60`}
        />
      </svg>
      <button
        type="button"
        className="cable__plug"
        style={{
          transform: `translateX(${pulled ? 150 : drag}px) ${pulled ? "rotate(18deg) translateY(40px)" : ""}`,
        }}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        onClick={() => {
          if (moved.current) {
            moved.current = false;
            return;
          }
          setDrag(0);
          setPulled((p) => !p);
        }}
        aria-pressed={pulled}
      >
        {pulled ? "Plug it back in" : "Pull me"}
      </button>
      <dl className="cable__meter">
        <div className="cable__zero">
          <dt>Bytes to third parties</dt>
          <dd>0</dd>
        </div>
        <div>
          <dt>Encrypted tunnel to your own devices</dt>
          <dd className="mono">{kb.toFixed(1)} KB</dd>
        </div>
        <div>
          <dt>Internet</dt>
          <dd className={pulled ? "is-off" : ""}>
            {pulled ? "Unplugged. Still translating." : "Connected, not needed"}
          </dd>
        </div>
      </dl>
    </div>
  );
}

/* ---------------------------------------------------------------- the summary that burns */

function Forget({ lang }: { lang: Lang }) {
  const [ref, seen] = useInView<HTMLElement>(0.55);
  const [burn, setBurn] = useState(false);
  useEffect(() => {
    if (!seen) return;
    const id = window.setTimeout(() => setBurn(true), reduced() ? 0 : 1600);
    return () => window.clearTimeout(id);
  }, [seen]);
  return (
    <section
      ref={ref}
      className={`lforget ${burn ? "is-burned" : ""}`}
      aria-labelledby="lforget-h"
    >
      <div className="lforget__text">
        <h2 id="lforget-h" className="lsec__h lsec__h--big">
          Then it forgets.
        </h2>
        <p className="lsec__lead">
          At the end both sides get the summary. Then the session key is
          overwritten and the files are deleted. Only the audit log remains,
          with no content: proof that it happened, not what was said.
        </p>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => setBurn((b) => !b)}
        >
          {burn ? "Show the summary again" : "Destroy the session"}
        </button>
      </div>
      <article className="lforget__sheet" aria-label="Example summary">
        <h3 className="lforget__h">Summary · Sprechzimmer</h3>
        <div className="lforget__sec">
          <h4>Medication</h4>
          <p>Metformin 500 mg, twice a day after meals</p>
          <p className="lforget__tr" lang={lang}>
            {lang === "en"
              ? "Checked and read back to the patient"
              : "Метформин 500 мг, два раза в день после еды"}
          </p>
        </div>
        <div className="lforget__sec">
          <h4>Next appointment</h4>
          <p>Thursday, 12 November, 9 a.m.</p>
          <p className="lforget__tr" lang={lang}>
            {lang === "en"
              ? "Corrected from 13 November"
              : "Четверг, 12 ноября, 9 часов"}
          </p>
        </div>
        <div className="lforget__sec">
          <h4>Checked facts</h4>
          <p>4 of 4 confirmed</p>
        </div>
        <footer className="lforget__tb">
          <span>
            <small>Room</small>SZ-7921
          </span>
          <span>
            <small>Bytes to third parties</small>0
          </span>
        </footer>
        <div className="lforget__stamp" aria-hidden={!burn}>
          <b>Key destroyed</b>
          <span className="mono">audit 8d5a92ef…</span>
        </div>
      </article>
    </section>
  );
}
