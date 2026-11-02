/** Summary `/r/:room/summary`: the sheet both sides keep (C-11, S-03, S-04, K-03, N-03), then the burn (T-02). */
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, eventsSocketUrl, type ModeConfig } from "../lib/api";
import type { EventData } from "../lib/events";
import { fmtTime, isRtl, pick, t } from "../lib/i18n";
import { useEventSocket } from "../lib/live";
import { deviceLang, loadJoin } from "../lib/session";
import { RoomStore, useRoomModel } from "../lib/store";
import { Hatch } from "../components/Drawing";
import { Key, Printer } from "../components/Icons";
import { QR } from "../components/QR";
import { resolveMode } from "./Join";
import "./screens.css";

type Summary = EventData["summary.ready"];

export default function SummaryScreen() {
  const { room = "" } = useParams();
  const join = useMemo(() => loadJoin(room), [room]);
  const lang = join?.lang ?? deviceLang();
  const role = join?.role ?? null;
  const store = useMemo(() => new RoomStore(room), [room]);
  useEventSocket(room, store, eventsSocketUrl(room));
  const m = useRoomModel(store);
  const [mode, setMode] = useState<ModeConfig | null>(join?.mode ?? null);
  const [fetched, setFetched] = useState<Summary | null>(null);
  const [approving, setApproving] = useState(false);
  const [burning, setBurning] = useState(false);
  const [askBurn, setAskBurn] = useState(false);

  useEffect(() => {
    if (!mode) void resolveMode(room).then(setMode);
  }, [room, mode]);

  // Poll until the summary exists (End may have been pressed on the other phone).
  useEffect(() => {
    let alive = true;
    let timer = 0;
    const load = async () => {
      try {
        const s = (await api.summary(room)) as Summary;
        if (alive && s?.sections) setFetched(s);
      } catch {
        if (alive) timer = window.setTimeout(load, 1200);
      }
    };
    void load();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [room]);

  const summary = m.summary ?? fetched;
  const burned = m.burn;
  const other = summary ? (lang === summary.lang_a ? summary.lang_b : summary.lang_a) : null;
  const dir = isRtl(lang) ? "rtl" : "ltr";
  const approvedBy = summary?.approved_by ?? [];
  const iApproved = role ? approvedBy.includes(role) : false;
  const takehomeFor = mode?.summary.takehome_for;

  const approve = async () => {
    if (!role) return;
    setApproving(true);
    try {
      const s = (await api.approve(room, role)) as Summary;
      if (s?.sections) setFetched(s);
    } finally {
      setApproving(false);
    }
  };

  const burn = async () => {
    setAskBurn(false);
    setBurning(true);
    try {
      await api.burn(room);
    } catch (e) {
      console.warn("[raum] burn", e);
      setBurning(false);
    }
  };

  return (
    <div className={`screen screen--summary ${burned ? "is-burned" : ""}`} dir={dir} lang={lang}>
      <header className={`tblock tblock--${mode?.id ?? "x"} no-print`}>
        <div className="tblock__row">
          {mode ? <Hatch mode={mode.id} size={22} className="tblock__hatch" /> : null}
          <div className="tblock__title">
            <span className="tblock__name">{t(lang, "summary.title")}</span>
            {mode ? <span className="tblock__gloss">{mode.name.de}</span> : null}
          </div>
          <span className="tblock__status mono">{room.toUpperCase()}</span>
        </div>
      </header>

      <main className={`sum ${burning || burned ? "is-burning" : ""}`}>
        {!summary && !burned ? (
          <p className="sum__waiting" role="status">
            {t(lang, "summary.waiting")}
          </p>
        ) : null}

        {summary ? (
          <article className="sheet" aria-label={t(lang, "summary.title")}>
            {summary.sections.map((sec) => (
              <section key={sec.key} className="sheet__sec">
                <h2 className="sheet__h">
                  <span>{pick(sec.title, lang)}</span>
                  {other && sec.title[other] && sec.title[other] !== pick(sec.title, lang) ? (
                    <span className="sheet__h2" lang={other} dir={isRtl(other) ? "rtl" : "ltr"}>
                      {sec.title[other]}
                    </span>
                  ) : null}
                </h2>
                {sec.items.length ? (
                  <ul className="sheet__items">
                    {sec.items.map((it, i) => (
                      <li key={i} className={it.uncertain ? "is-uncertain" : ""}>
                        <p className="sheet__main" lang={lang} dir="auto">
                          {it.text[lang] ?? pick(it.text, lang)}
                          {it.uncertain ? <span className="sheet__unc label"> · {t(lang, "summary.uncertain")}</span> : null}
                        </p>
                        {other && it.text[other] ? (
                          <p className="sheet__other" lang={other} dir={isRtl(other) ? "rtl" : "ltr"}>
                            {it.text[other]}
                          </p>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="sheet__none">—</p>
                )}
              </section>
            ))}

            {summary.file_note && (role === "a" || !role) ? (
              <section className="sheet__sec sheet__note">
                <h2 className="sheet__h">{t(lang, "summary.file_note")}</h2>
                {Object.entries(summary.file_note).map(([l, txt]) => (
                  <p key={l} lang={l} dir="auto" className="sheet__main">
                    {txt}
                  </p>
                ))}
              </section>
            ) : null}

            {/* Schriftfeld: the sheet's title block, bottom right as on every drawing */}
            <footer className="sheet__tb">
              <dl>
                <div>
                  <dt>{t(lang, "join.room")}</dt>
                  <dd className="mono">{room.toUpperCase()}</dd>
                </div>
                <div>
                  <dt>{t(lang, "join.language")}</dt>
                  <dd className="mono">
                    {summary.lang_a.toUpperCase()} ⇄ {summary.lang_b.toUpperCase()}
                  </dd>
                </div>
                <div>
                  <dt>gez.</dt>
                  <dd className="mono">RAUM {summary.generated_ms != null ? `· ${(summary.generated_ms / 1000).toFixed(1)} s` : ""}</dd>
                </div>
                <div>
                  <dt>gepr.</dt>
                  <dd className="mono">
                    {approvedBy.length
                      ? approvedBy.map((r) => (mode ? pick(mode.roles[r as "a" | "b"]?.label ?? {}, lang) : r)).join(" · ")
                      : "—"}
                  </dd>
                </div>
              </dl>
            </footer>
          </article>
        ) : null}

        {summary && !burned ? (
          <div className="sum__actions no-print">
            {role ? (
              <button type="button" className="btn btn--lg btn--block" onClick={approve} disabled={iApproved || approving}>
                {iApproved ? t(lang, "summary.approved") : t(lang, "summary.approve")}
              </button>
            ) : null}
            <button type="button" className="btn btn--ghost btn--block" onClick={() => window.print()}>
              <Printer /> {t(lang, "summary.print")}
            </button>
            {(role === takehomeFor || !role) && takehomeFor ? (
              <div className="sum__takehome">
                <QR value={location.href} size={136} label={t(lang, "summary.takehome")} />
                <div>
                  <h3 className="sum__th">{t(lang, "summary.takehome")}</h3>
                  <p className="sum__tb">{t(lang, "summary.takehome_body")}</p>
                </div>
              </div>
            ) : null}
            {role === "a" ? (
              askBurn ? (
                <div className="sum__burnask" role="alertdialog" aria-labelledby="burn-q">
                  <p id="burn-q">{t(lang, "summary.burn_confirm")}</p>
                  <div className="confirm__actions">
                    <button type="button" className="btn btn--bad btn--lg" onClick={burn}>
                      <Key /> {t(lang, "summary.burn")}
                    </button>
                    <button type="button" className="btn btn--ghost btn--lg" onClick={() => setAskBurn(false)}>
                      {t(lang, "talk.close")}
                    </button>
                  </div>
                </div>
              ) : (
                <button type="button" className="btn btn--bad btn--block" onClick={() => setAskBurn(true)} disabled={burning}>
                  <Key /> {t(lang, "summary.burn")}
                </button>
              )
            ) : null}
            {join ? (
              <Link className="sum__back" to={`/r/${room}`}>
                {t(lang, "summary.back")}
              </Link>
            ) : null}
          </div>
        ) : null}

        {burned ? <BurnStamp burn={burned} lang={lang} /> : null}
      </main>
    </div>
  );
}

export function BurnStamp({ burn, lang }: { burn: EventData["burn.done"]; lang: string }) {
  return (
    <section className="stamp" role="status" aria-live="assertive">
      <svg className="stamp__frame" viewBox="0 0 300 120" aria-hidden="true">
        <rect x="4" y="4" width="292" height="112" pathLength={1} />
        <path d="M18 104L282 16" pathLength={1} />
      </svg>
      <div className="stamp__body">
        <p className="stamp__title">{t(lang, "summary.burned")}</p>
        <p className="stamp__time mono">{fmtTime(burn.key_destroyed_at, lang)} · {new Date(burn.key_destroyed_at).toISOString().slice(11, 19)} UTC</p>
        <p className="stamp__hash mono" title={burn.audit_head_hash}>
          audit {burn.audit_head_hash.slice(0, 16)}…
        </p>
      </div>
      <p className="stamp__note">{t(lang, "summary.burned_body")}</p>
    </section>
  );
}
