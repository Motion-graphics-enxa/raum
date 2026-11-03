/** `/`: the RAUM website. It opens on a reel of three rooms; in each, one sentence is heard, translated, checked and forgotten. */
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Check, Handshake, Pause, Play, Scale, Stethoscope } from "../components/Icons";
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
    leave: "A take-home summary in the patient's language and a German file note.",
    Icon: Stethoscope,
  },
  {
    id: "kanzlei",
    plate: "Kanzlei",
    gloss: "Law office",
    people: "Lawyer and client",
    guard: "Deadlines and dates are checked; 'explain simply' only goes out once the lawyer approves it.",
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
    leave: "German case notes and the client's copy, both with every date checked.",
    Icon: Scale,
  },
  {
    id: "verhandlungsraum",
    plate: "Verhandlungsraum",
    gloss: "Negotiation room",
    people: "CEO and foreign partner",
    guard: "Every offer is tracked with its amount and terms; the deal glossary keeps names exact.",
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

const reduced = () => typeof matchMedia !== "undefined" && matchMedia("(prefers-reduced-motion: reduce)").matches;

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
        {/* ------------------------------------------------ the reel: three rooms, played like a title sequence */}
        <Reel lang={lang} onLang={setLang} />

        {/* ------------------------------------------------ today */}
        <section className="ltoday" aria-labelledby="ltoday-h">
          <h2 id="ltoday-h" className="lsec__h">
            Today the interpreter is a relative, a phone app, or nobody.
          </h2>
          <ul className="ltoday__facts">
            <li>
              Statutory health insurance pays <span className="ltoday__fig">nothing</span> for an interpreter.
            </li>
            <li>
              At weekends, only <span className="ltoday__fig">14 of 38</span> German university hospitals can get one.
            </li>
            <li>
              Yet about <span className="ltoday__fig">15&nbsp;%</span> of people in Germany mainly speak another language at home.
            </li>
          </ul>
          <p className="ltoday__law">
            And sending these conversations to a cloud is its own legal risk: §203 StGB for doctors, §43e BRAO for
            lawyers, GeschGehG §2 for trade secrets.
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
            Code compares every number, unit, date, name and medication between what was said and what was translated. A
            second model from a different family translates it back and scores the meaning. A human confirms what
            matters.
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
              Speech recognition, translation, a checker from a different model family, meaning search, voice: all on
              one ASUS Ascent GX10 in the room. Pull the internet cable and the conversation goes on.
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
          <p className="lclose__p">Scan the QR code on the box, choose your language, and talk. No account, no app, no cloud.</p>
          <Link className="btn btn--lg lclose__cta" to="/start">
            Open a room
          </Link>
        </section>
      </main>

      <footer className="lfoot">
        <span className="lfoot__word">RAUM</span>
        <span>A private AI interpreter that lives in the room. Macromedia Berlin Hackathon 2026.</span>
        <span className="lfoot__links">
          <Link to="/start">Rooms</Link>
          <Link to="/admin">Admin</Link>
        </span>
      </footer>
    </div>
  );
}

/** Two copies render (the bar's on wide screens, the hero's on narrow ones); CSS shows one at a time. */
function LangSwitch({ lang, onChange, className = "" }: { lang: Lang; onChange: (l: Lang) => void; className?: string }) {
  return (
    <div className={`lswitch ${className}`} role="radiogroup" aria-label="Example translations in">
      <span className="lswitch__label">Examples in</span>
      {(["en", "ru"] as const).map((l) => (
        <button key={l} type="button" role="radio" lang={l} aria-checked={lang === l} className={lang === l ? "is-on" : ""} onClick={() => onChange(l)}>
          {l === "en" ? "English" : "Русский"}
        </button>
      ))}
    </div>
  );
}

/* ---------------------------------------------------------------- the reel */

/** One scene per room: [brackets] mark the facts code compares between what was said and its translation. */
const REEL: Record<RoomId, { to: string; de: string; tr: Record<Lang, string> }> = {
  sprechzimmer: {
    to: "the patient",
    de: "Nehmen Sie [zweimal täglich] [500 mg] [Metformin] nach dem Essen.",
    tr: {
      en: "Take [500 mg] of [metformin] [twice a day], after meals.",
      ru: "Принимайте [500 мг] [метформина] [два раза в день] после еды.",
    },
  },
  kanzlei: {
    to: "the client",
    de: "Die Frist für Ihren [Widerspruch] endet am [14. März].",
    tr: {
      en: "The deadline for your [objection] ends on [14 March].",
      ru: "Срок подачи вашего [возражения] истекает [14 марта].",
    },
  },
  verhandlungsraum: {
    to: "the partner",
    de: "Unser letztes Angebot: [2,4 Millionen Euro], Lieferung im [Juni].",
    tr: {
      en: "Our final offer: [2.4 million euros], delivery in [June].",
      ru: "Наше последнее предложение: [2,4 миллиона евро], поставка в [июне].",
    },
  },
};
const SCENE_MS = 7600;
const LEAD_MS = 900; // the first scene waits for the line to draw and the paint to come in
const facts = (s: string) => (s.match(/\[/g) ?? []).length;

/** A sentence as words that arrive one by one; its facts are wrapped so the check can mark them. */
function Sentence({ text, lang, className }: { text: string; lang: string; className: string }) {
  let w = 0;
  let f = 0;
  const words = (t: string) =>
    t.split(/(\s+)/).map((x, i) =>
      !x ? null : /^\s+$/.test(x) ? x : (
        <span key={i} className="w" style={{ "--w": w++ } as React.CSSProperties}>
          {x}
        </span>
      ),
    );
  // a fact keeps the punctuation after it on its line, so no line ever starts with a comma
  return (
    <p className={className} lang={lang}>
      {text
        .split(/(\[[^\]]+\][,.;:!?]?)/)
        .filter(Boolean)
        .map((seg, i) => {
          const m = /^\[([^\]]+)\](.*)$/.exec(seg);
          if (!m) return <Fragment key={i}>{words(seg)}</Fragment>;
          return (
            <span key={i} className="fw">
              <span className="f" style={{ "--f": f++ } as React.CSSProperties}>
                {words(m[1])}
              </span>
              {m[2] ? words(m[2]) : null}
            </span>
          );
        })}
    </p>
  );
}

/** Pauses the reel while it is off screen or the tab is hidden. */
function useOnScreen<T extends Element>() {
  const ref = useRef<T>(null);
  const [on, setOn] = useState(true);
  useEffect(() => {
    const el = ref.current;
    let seen = true;
    let shown = document.visibilityState !== "hidden";
    const sync = () => setOn(seen && shown);
    const onVis = () => {
      shown = document.visibilityState !== "hidden";
      sync();
    };
    document.addEventListener("visibilitychange", onVis);
    let io: IntersectionObserver | undefined;
    if (el && typeof IntersectionObserver !== "undefined") {
      io = new IntersectionObserver(
        (es) => {
          for (const e of es) seen = e.isIntersecting;
          sync();
        },
        { threshold: 0.15 },
      );
      io.observe(el);
    }
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      io?.disconnect();
    };
  }, []);
  return [ref, on] as const;
}

function Reel({ lang, onLang }: { lang: Lang; onLang: (l: Lang) => void }) {
  const [ref, onScreen] = useOnScreen<HTMLElement>();
  const [scene, setScene] = useState(0);
  const [prev, setPrev] = useState(-1);
  const [phase, setPhase] = useState(0); // 0 waiting · 1 heard · 2 translated · 3 checked · 4 forgotten
  const [lit, setLit] = useState(false);
  const [playing, setPlaying] = useState(true);
  const [run, setRun] = useState(0);
  const booted = useRef(false);
  const still = reduced() || !playing;
  const room = ROOMS[scene];
  const n = facts(REEL[room.id].de);
  // the bar restarts with the scene timers; only the very first scene waits for the line and the paint
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const barLead = useMemo(() => (booted.current ? 0 : LEAD_MS), [scene, run, still, onScreen]);

  useEffect(() => {
    if (still) {
      booted.current = true;
      setLit(true);
      setPhase(3);
      return;
    }
    if (!onScreen) return;
    const ts: number[] = [];
    const at = (ms: number, fn: () => void) => ts.push(window.setTimeout(fn, ms));
    const lead = booted.current ? 0 : LEAD_MS;
    booted.current = true;
    setPhase(0);
    at(Math.max(0, lead - 600), () => setLit(true));
    at(lead + 350, () => setPhase(1));
    at(lead + 2000, () => setPhase(2));
    at(lead + 3300, () => setPhase(3));
    at(lead + SCENE_MS - 1150, () => setPhase(4));
    at(lead + SCENE_MS, () => {
      setPrev(scene);
      setScene((scene + 1) % ROOMS.length);
    });
    return () => ts.forEach(clearTimeout);
  }, [scene, run, still, onScreen]);

  const go = (i: number) => {
    if (i === scene) setRun((r) => r + 1);
    else {
      setPrev(scene);
      setScene(i);
    }
  };

  const cls = ["reel", lit ? "is-lit" : "", phase >= 1 ? "is-heard" : "", phase >= 2 ? "is-tr" : "", phase === 3 ? "is-ok" : "", phase === 4 ? "is-gone" : "", still ? "is-still" : ""]
    .filter(Boolean)
    .join(" ");
  const status =
    phase >= 4 ? "Session over. Key destroyed." : phase === 3 ? `${n} of ${n} facts match` : phase === 2 ? "Translating, checking" : "Listening";

  return (
    <section ref={ref} className={cls} aria-labelledby="reel-h">
      <div className="reel__wall">
        <span className="reel__beam" aria-hidden="true" />
        <div className="reel__head">
          <h1 id="reel-h" className="reel__h">
            <span className="reel__line">
              <span>What’s said in the room</span>
            </span>{" "}
            <span className="reel__line reel__line--2">
              <span>stays in the room.</span>
            </span>
          </h1>
          <div className="reel__offer">
            <p>
              RAUM is a private AI interpreter for the rooms where a misheard number is a legal problem. It translates both
              ways, checks every figure, and forgets. <strong>Nothing leaves the box.</strong>
            </p>
          </div>
        </div>
        <div className="reel__stack reel__stack--de">
          {ROOMS.map((r, i) => (
            <div key={r.id} className={`reel__scene ${i === scene ? "is-on" : ""}`} aria-hidden={i !== scene}>
              <span className="reel__who">
                <span className="reel__live" aria-hidden="true" />
                Example · {r.said}, speaking German
              </span>
              <Sentence className="reel__de" text={REEL[r.id].de} lang="de" />
            </div>
          ))}
        </div>
      </div>

      <div className="reel__dado">
        <span className="reel__strich" aria-hidden="true" />
        <span className="reel__paint" aria-hidden="true">
          {ROOMS.map((r, i) => (
            <i key={r.id} className={`paint--${r.id} ${lit && i === scene ? "is-on" : ""} ${lit && i === prev && i !== scene ? "is-prev" : ""}`} />
          ))}
        </span>
        <span className="reel__ghost" aria-hidden="true">
          {ROOMS.map((r, i) => (
            <span key={r.id} className={lit && i === scene ? "is-on" : ""} lang="de">
              {r.plate}
            </span>
          ))}
        </span>
        <span className="reel__sweep" aria-hidden="true" />

        <div className="reel__stack reel__stack--tr">
          {ROOMS.map((r, i) => (
            <div key={r.id} className={`reel__scene ${i === scene ? "is-on" : ""}`} aria-hidden={i !== scene}>
              <div className="reel__meta">
                <span className="reel__to">
                  For {REEL[r.id].to}, in {lang === "en" ? "English" : "Russian"}
                </span>
                <span className="reel__status">
                  {i === scene && phase === 3 ? <Check /> : <span className="reel__dot" aria-hidden="true" />}
                  {i === scene ? status : "Listening"}
                </span>
              </div>
              <Sentence className="reel__tr" text={REEL[r.id].tr[lang]} lang={lang} />
            </div>
          ))}
        </div>

        <div className="reel__foot">
          <ol className="reel__rooms" aria-label="The rooms">
            {ROOMS.map((r, i) => (
              <li key={r.id} className={i === scene ? "is-on" : ""}>
                <button type="button" aria-pressed={i === scene} onClick={() => go(i)}>
                  <span className="reel__bar" aria-hidden="true">
                    <i
                      key={i === scene ? `${scene}-${run}-${still}-${onScreen}` : "off"}
                      style={{ "--lead": `${barLead}ms` } as React.CSSProperties}
                    />
                  </span>
                  <span className="reel__plate" lang="de">
                    {r.plate}
                  </span>
                  <span className="reel__gloss">
                    {r.gloss} · {r.people}
                  </span>
                </button>
              </li>
            ))}
          </ol>
          <div className="reel__go">
            <Link className="btn reel__cta" to="/start">
              <span>
                Open the <span lang="de">{room.plate}</span>
              </span>
              <ArrowRight />
            </Link>
            <span className="reel__go-row">
              <a className="reel__more" href={`#room-${room.id}`}>
                How it works in this room
              </a>
              <button type="button" className="reel__pause" aria-pressed={!playing} onClick={() => setPlaying((p) => !p)}>
                {playing ? <Pause /> : <Play />}
                {playing ? "Pause" : "Play"}
              </button>
            </span>
          </div>
          <LangSwitch lang={lang} onChange={onLang} className="lswitch--hero" />
        </div>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- the check, played live */

const DEMO = {
  de: "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.",
  wrong: { en: "Come back on Thursday, 13 November, at 9.", ru: "Приходите в четверг, 13 ноября, в 9 часов." },
  right: { en: "Come back on Thursday, 12 November, at 9 a.m.", ru: "Приходите в четверг, 12 ноября, в 9 часов." },
  said: { en: "12 November", ru: "12 ноября" },
  got: { en: "13 November", ru: "13 ноября" },
};
const STAGES = ["Heard", "Cleaned", "Transcribed", "Translated", "Checked", "Corrected", "Spoken"];

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
    const at = (ms: number, fn: () => void) => ts.push(window.setTimeout(fn, ms));
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
          <li key={s} className={`${step > i ? "is-done" : ""} ${step === i + 1 ? "is-now" : ""} ${s === "Corrected" ? "is-fix" : ""}`}>
            <span className="demo__dot" aria-hidden="true" />
            {s}
          </li>
        ))}
      </ol>
      <div className={`demo__sheet ${fixed ? "is-final" : "is-draft"} ${caught ? "is-red" : ""}`}>
        <div className="demo__gutter">
          <span className="demo__plate">Doctor · German</span>
          <span className={`demo__mark ${fixed ? "is-ok" : caught ? "is-bad" : translated ? "is-pending" : "is-live"}`} role="status">
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
          {words < deWords.length ? <span className="demo__caret" aria-hidden="true" /> : null}
        </p>
        <p className={`demo__tr ${translated ? "is-on" : ""}`} lang={lang} aria-live="polite">
          {translated ? (fixed ? DEMO.right[lang] : DEMO.wrong[lang]) : " "}
        </p>
        <div className={`demo__facts ${translated ? "is-on" : ""}`} lang={lang}>
          <span className="demo__chip demo__chip--ok">
            <b>{lang === "en" ? "Thursday" : "четверг"}</b>
            <small>Day</small>
          </span>
          <span className={`demo__chip ${fixed ? "demo__chip--ok" : caught ? "demo__chip--bad" : "demo__chip--pending"}`}>
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
        <p className="demo__note">Example. A first translation wrote 13 instead of 12; code compared the dates, flagged it, and the fixed sentence went out.</p>
      </div>
      <button type="button" className="demo__replay" onClick={replay} disabled={step > 0 && step < 7}>
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
    <article id={`room-${room.id}`} ref={ref} className={`lroom room--${room.id} ${seen ? "is-seen" : ""}`} aria-labelledby={`lroom-${room.id}`}>
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
            <li key={f.kind} style={{ "--d": `${i * 90}ms` } as React.CSSProperties}>
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
    const id = window.setInterval(() => setKb((k) => Math.round((k + 0.3 + Math.random() * 0.6) * 10) / 10), 900);
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
        <path d={`M40 60 C ${120 + drag} ${pulled ? 110 : 60}, ${220 + drag} ${pulled ? 120 : 60}, ${300 + drag} 60`} />
      </svg>
      <button
        type="button"
        className="cable__plug"
        style={{ transform: `translateX(${pulled ? 150 : drag}px) ${pulled ? "rotate(18deg) translateY(40px)" : ""}` }}
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
          <dd className={pulled ? "is-off" : ""}>{pulled ? "Unplugged. Still translating." : "Connected, not needed"}</dd>
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
    <section ref={ref} className={`lforget ${burn ? "is-burned" : ""}`} aria-labelledby="lforget-h">
      <div className="lforget__text">
        <h2 id="lforget-h" className="lsec__h lsec__h--big">
          Then it forgets.
        </h2>
        <p className="lsec__lead">
          At the end both sides get the summary. Then the session key is overwritten and the files are deleted. Only the
          audit log remains, with no content: proof that it happened, not what was said.
        </p>
        <button type="button" className="btn btn--ghost" onClick={() => setBurn((b) => !b)}>
          {burn ? "Show the summary again" : "Destroy the session"}
        </button>
      </div>
      <article className="lforget__sheet" aria-label="Example summary">
        <h3 className="lforget__h">Summary · Sprechzimmer</h3>
        <div className="lforget__sec">
          <h4>Medication</h4>
          <p>Metformin 500 mg, twice a day after meals</p>
          <p className="lforget__tr" lang={lang}>
            {lang === "en" ? "Checked and read back to the patient" : "Метформин 500 мг, два раза в день после еды"}
          </p>
        </div>
        <div className="lforget__sec">
          <h4>Next appointment</h4>
          <p>Thursday, 12 November, 9 a.m.</p>
          <p className="lforget__tr" lang={lang}>
            {lang === "en" ? "Corrected from 13 November" : "Четверг, 12 ноября, 9 часов"}
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
