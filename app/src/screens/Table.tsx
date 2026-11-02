/** Table screen `/t/:room`: bilingual conversation left, glass box right (C-09, G-01..G-04, T-06). */
import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { eventsSocketUrl, type ModeConfig } from "../lib/api";
import type { ParticipantInfo } from "../lib/events";
import { isRtl, langName, pick, t } from "../lib/i18n";
import { useEventSocket } from "../lib/live";
import { RoomStore, checkState, percentile, useRoomModel, type Turn } from "../lib/store";
import { shownTranslation } from "../lib/draft";
import { DraftText } from "../components/DraftText";
import { FactDims, Hatch, VerifyMark } from "../components/Drawing";
import { Timeline, firstWordsMs, pathLabel } from "../components/Sheet";
import { OfferTimeline } from "../components/Offers";
import { QR } from "../components/QR";
import { BurnStamp } from "./Summary";
import { resolveMode } from "./Join";
import "./screens.css";
import "./table.css";

const UI = "en"; // the table screen chrome is English like every screen; conversation text is bilingual

export default function Table() {
  const { room = "" } = useParams();
  const [qs] = useSearchParams();
  const store = useMemo(() => new RoomStore(room), [room]);
  const link = useEventSocket(room, store, eventsSocketUrl(room));
  const m = useRoomModel(store);
  const [mode, setMode] = useState<ModeConfig | null>(null);
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void resolveMode(room).then(setMode);
    const theme = qs.get("theme");
    if (theme === "dark" || theme === "light") document.documentElement.dataset.theme = theme;
    return () => {
      delete document.documentElement.dataset.theme;
    };
  }, [room, qs]);

  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [m.version]);

  const people = Object.values(m.participants).filter((p) => p.role === "a" || p.role === "b");
  // The participants' real languages; two people who both joined as "b" still show as a pair.
  const pa = people.find((p) => p.role === "a") ?? (people.length >= 2 ? people[0] : undefined);
  const pb = people.find((p) => p.role === "b" && p !== pa);
  const heard = m.turns.find((tu) => tu.lang && tu.lang !== pb?.lang)?.lang;
  const langA = pa?.lang ?? heard ?? mode?.roles.a.default_lang ?? "de";
  const langB = pb?.lang ?? null;
  const turns = m.turns.filter((tu) => tu.text || tu.partial || tu.uncertain);
  const last = [...turns].reverse().find((tu) => Object.keys(tu.timing).length) ?? null;
  const lastLang = last ? Object.keys(last.timing)[0] : null;
  const firsts = turns.map((tu) => Object.values(tu.timing).map(firstWordsMs)).flat().filter((x): x is number => x != null);
  const fulls = turns
    .map((tu) => Object.values(tu.timing).map((tm) => tm.mt_full_ms ?? null))
    .flat()
    .filter((x): x is number => x != null);
  const p50 = percentile(firsts, 50);
  const p95 = percentile(firsts, 95);
  const full50 = percentile(fulls, 50);
  const models = last && lastLang ? last.timing[lastLang]?.models : null;
  const base = `${location.origin}/j/${room}`;
  const label = (r: string | null) => (mode && (r === "a" || r === "b") ? pick(mode.roles[r].label, UI) : "");

  return (
    <div className="screen screen--table">
      <header className={`tblock tblock--${mode?.id ?? "x"} ttop`}>
        <div className="tblock__row">
          {mode ? <Hatch mode={mode.id} size={30} className="tblock__hatch" /> : null}
          <div className="tblock__title">
            <span className="tblock__name ttop__name">{mode ? pick(mode.name, UI) : "RAUM"}</span>
            <span className="tblock__gloss">{mode ? pick(mode.tagline ?? {}, UI) : ""}</span>
          </div>
          <span className="ttop__room mono">{room.toUpperCase()}</span>
          <span className="ttop__langs mono">
            {langA.toUpperCase()} ⇄ {(langB ?? "··").toUpperCase()}
          </span>
          <span className={`tblock__status ${link === "connected" ? "is-live" : ""}`}>
            <span className="tblock__dot" aria-hidden="true" />
            {m.session?.off_record ? t(UI, "talk.off_record") : link === "connected" ? "Live" : t(UI, "talk.reconnecting")}
          </span>
        </div>
      </header>

      <div className="tgrid">
        <section className="tconv" aria-label="Conversation">
          <div className="tconv__head">
            <span className="label">{langName(langA, UI)}</span>
            <span className="label">{langB ? langName(langB, UI) : "—"}</span>
          </div>
          <div className="tconv__scroll" ref={scroller}>
            {turns.length === 0 ? (
              <div className="tjoin">
                <p className="tjoin__lead">Scan with a phone to join the room.</p>
                <div className="tjoin__codes">
                  {(["a", "b"] as const).map((r) => (
                    <figure key={r} className="tjoin__code">
                      <QR value={`${base}?role=${r}`} size={188} label={label(r)} />
                      <figcaption>
                        <span className="tjoin__role">{label(r)}</span>
                        <span className="mono tjoin__url">/j/{room}?role={r}</span>
                      </figcaption>
                    </figure>
                  ))}
                </div>
              </div>
            ) : (
              turns.map((tu, i) => (
                <TableRow key={tu.utt} turn={tu} langA={langA} langB={langB} hero={i === turns.length - 1} label={label(tu.role)} />
              ))
            )}
          </div>
        </section>

        <aside className="glass" aria-label="Glass box">
          <section className="glass__block">
            <h2 className="glass__h">Last sentence</h2>
            {last && lastLang ? (
              <>
                <Timeline timing={last.timing[lastLang]} lang={UI} big p50={p50} p95={p95} full50={full50} />
              </>
            ) : (
              <p className="glass__idle">Waiting for the first sentence.</p>
            )}
          </section>

          <section className="glass__block glass__privacy">
            <div className="glass__meter">
              <span className="glass__meter-n mono">{(m.privacy?.third_party_bytes ?? 0).toLocaleString("en-GB")}</span>
              <span className="glass__meter-l">Bytes to third parties</span>
            </div>
            <div className="glass__meter glass__meter--small">
              <span className="mono">{fmtKB(m.privacy?.tunnel_bytes ?? 0)}</span>
              <span className="glass__meter-l">Encrypted tunnel to your own devices</span>
            </div>
            <p className={`glass__seal ${m.privacy?.sealed || m.session?.sealed ? "is-sealed" : ""}`}>
              {m.privacy?.sealed || m.session?.sealed ? "Sealed: outgoing traffic blocked" : "Not sealed"}
              {m.privacy?.source === "stub" ? <span className="mono glass__src"> · stub</span> : null}
            </p>
          </section>

          <section className="glass__block">
            <h2 className="glass__h">Devices</h2>
            <ul className="plates">
              {people.length ? (
                people.map((p) => <Plate key={p.id} p={p} label={label(p.role)} />)
              ) : (
                <li className="glass__idle">Nobody in the room yet.</li>
              )}
            </ul>
          </section>

          <section className="glass__block">
            <h2 className="glass__h">Models</h2>
            <dl className="models">
              {Object.entries(models ?? {}).map(([k, v]) => (
                <div key={k}>
                  <dt>{k}</dt>
                  <dd className="mono">{v}</dd>
                </div>
              ))}
              <div>
                <dt>GPU</dt>
                <dd className="mono">
                  {m.gpu ? `${Math.round(m.gpu.util_pct)} %` : "—"}
                  {m.gpu?.source === "mock" ? " · mock" : ""}
                </dd>
              </div>
            </dl>
            {!models && m.gpu?.models_loaded?.length ? (
              <p className="glass__models mono">{m.gpu.models_loaded.join(" · ")}</p>
            ) : null}
          </section>

          {mode?.id === "verhandlungsraum" && m.offers.length ? (
            <section className="glass__block">
              <h2 className="glass__h">Offers</h2>
              <OfferTimeline offers={m.offers} lang={UI} labelOf={(r) => label(r)} />
            </section>
          ) : null}

          {m.burn ? <BurnStamp burn={m.burn} lang={UI} /> : null}
        </aside>
      </div>
    </div>
  );
}

function TableRow({
  turn,
  langA,
  langB,
  hero,
  label,
}: {
  turn: Turn;
  langA: string;
  langB: string | null;
  hero: boolean;
  label: string;
}) {
  const cell = (lang: string | null) => {
    if (!lang) return <span className="trow__empty">—</span>;
    const isSrc = lang === turn.lang;
    const shown = isSrc ? null : shownTranslation(turn, lang); // the translation + its live draft (draft.ts)
    const text = isSrc ? turn.text ?? turn.partial : shown?.text;
    return (
      <p className={`trow__text ${isSrc ? "is-src" : ""}`} lang={lang} dir={isRtl(lang) ? "rtl" : "ltr"}>
        {text || (shown?.draft ? null : " ")}
        {shown?.draft ? <DraftText text={shown.draft} ui={UI} lead={!!text} mark /> : null}
      </p>
    );
  };
  const target = turn.lang === langA ? langB : langA;
  const state = target ? checkState(turn, target) : "translating";
  return (
    <article className={`trow ${hero ? "is-hero" : ""} ${state === "red" && !turn.continuedIn ? "is-red" : ""}`}>
      <header className="trow__gutter">
        <span className="trow__who">{label}</span>
        {turn.continuedIn ? null : turn.uncertain ? <VerifyMark state="uncertain" lang={UI} /> : target ? <VerifyMark state={state} lang={UI} ms={firstWordsMs(turn.timing[target])} /> : null}
      </header>
      <div className="trow__cols">
        {cell(langA)}
        {cell(langB)}
      </div>
      {target && !turn.continuedIn && turn.facts[target]?.length ? (
        <div className="trow__facts">
          <FactDims facts={turn.facts[target]} lang={UI} state={state} />
        </div>
      ) : null}
    </article>
  );
}

function Plate({ p, label }: { p: ParticipantInfo; label: string }) {
  return (
    <li className={`plate plate--${p.path}`}>
      <span className="plate__who">{label}</span>
      <span className="plate__lang mono">{p.lang.toUpperCase()}</span>
      <span className="plate__dev mono">{p.device}</span>
      <span className="plate__path mono">
        {pathLabel(p.path, UI)}
        {p.rtt_ms != null ? ` ${p.rtt_ms} ms` : ""}
      </span>
    </li>
  );
}

function fmtKB(bytes: number): string {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toLocaleString("en-GB", { maximumFractionDigits: 1 })} KB`;
}
