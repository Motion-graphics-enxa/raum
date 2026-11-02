/** One turn of the conversation, as one device should read it. */
import { useState } from "react";
import type { Role } from "../lib/events";
import { fmtTime, isRtl, t } from "../lib/i18n";
import { checkState, factsFor, type Turn as TurnT } from "../lib/store";
import { FactDims, VerifyMark, fmtMs } from "./Drawing";
import { Timeline, firstWordsMs } from "./Sheet";
import "./turn.css";

export function TurnView({
  turn,
  myLang,
  otherLang,
  mine,
  ramp,
  speakerLabel,
  onExplain,
  onRelearn,
  checkerOff = false,
  onApproveExplain,
  myRole,
}: {
  turn: TurnT;
  myLang: string;
  otherLang: string | null;
  mine: boolean;
  ramp: 0 | 1 | 2 | 3;
  speakerLabel: string;
  myRole: Role | null;
  onExplain?: (utt: string) => void;
  onRelearn?: () => void; // F-10: learn this device's voice again
  checkerOff?: boolean; // the room runs no second check: show the mark only when the guard finds a mismatch
  onApproveExplain?: (utt: string, text: string) => void;
}) {
  const [open, setOpen] = useState(false);
  // What this device should read: the other person's words in my language, or my own words.
  const viewLang = mine ? otherLang ?? myLang : myLang;
  const tr = turn.translations[viewLang];
  const state = checkState(turn, viewLang);
  const facts = factsFor(turn, viewLang);
  const timing = turn.timing[viewLang] ?? turn.timing._;
  const firstMs = firstWordsMs(timing);
  const v = turn.verify[viewLang];
  const finalSrc = turn.text;
  const live = !finalSrc && !!turn.partial;

  let main: string;
  let mainLang: string;
  let sub: string | null = null;
  let subLang = turn.lang;
  if (mine) {
    main = finalSrc ?? turn.partial ?? "";
    mainLang = turn.lang;
    sub = tr?.text ?? null;
    subLang = viewLang;
  } else if (tr?.text) {
    main = tr.text;
    mainLang = myLang;
    sub = finalSrc ?? turn.partial ?? null;
  } else {
    // nothing translated yet: show what is being said, in the speaker's script, quietly
    main = finalSrc ?? turn.partial ?? "";
    mainLang = turn.lang;
  }
  const waitingForTranslation = !mine && !tr?.text;
  const sayAgain = turn.uncertain;
  const flagText = turn.flags.find((f) => f.kind === "uncertain");
  const corrected = turn.corrected?.[viewLang];

  return (
    <article
      className={`turn turn--r${ramp} ${mine ? "turn--mine" : "turn--theirs"} ${live ? "is-live" : ""} ${
        state === "red" ? "is-red" : ""
      }`}
      data-utt={turn.utt}
    >
      <header className="turn__gutter">
        <span className="turn__who">{speakerLabel}</span>
        <span className="turn__time mono">{fmtTime(turn.startedAt, myLang)}</span>
        <span className="turn__spacer" />
        {!sayAgain && !turn.ignored && (tr || mine) && (!checkerOff || state === "red") ? (
          <VerifyMark state={state} lang={myLang} ms={state === "translating" ? null : firstMs} onClick={() => setOpen((o) => !o)} />
        ) : null}
      </header>

      {turn.ignored ? (
        <div className="turn__ignored">
          <p dir="auto">{t(myLang, "talk.not_your_voice")}</p>
          {onRelearn ? (
            <button type="button" className="turn__action" onClick={onRelearn}>
              {t(myLang, "talk.relearn_voice")}
            </button>
          ) : null}
        </div>
      ) : sayAgain ? (
        <p className="turn__again" dir="auto">
          {flagText?.texts?.[myLang] ?? t(myLang, "talk.say_again")}
        </p>
      ) : (
        <>
          {corrected ? (
            <p className="turn__struck" lang={mainLang} dir={isRtl(mainLang) ? "rtl" : "ltr"}>
              <s>{corrected}</s>
            </p>
          ) : null}
          <p
            className={`turn__main ${waitingForTranslation ? "is-source" : ""}`}
            lang={mainLang}
            dir={isRtl(mainLang) ? "rtl" : "ltr"}
          >
            {main || " "}
            {(live || (tr && !tr.final)) && <span className="turn__caret" aria-hidden="true" />}
          </p>
          {sub ? (
            <p className="turn__sub" lang={subLang} dir={isRtl(subLang) ? "rtl" : "ltr"}>
              {mine ? <span className="turn__sublabel">{t(myLang, "talk.they_got")} · </span> : null}
              {sub}
            </p>
          ) : null}
          {facts.length ? (
            <div className="turn__facts" dir={isRtl(myLang) ? "rtl" : "ltr"}>
              <FactDims facts={facts} lang={myLang} state={state} />
            </div>
          ) : null}
        </>
      )}

      {turn.repeatBack ? <RepeatBack r={turn.repeatBack} lang={myLang} /> : null}

      {turn.explainDraft && myRole === "a" && !turn.explainSent ? (
        <div className="turn__explain">
          <span className="label">{t(myLang, "talk.explain_simply")}</span>
          <p dir="auto">{turn.explainDraft.text}</p>
          <button type="button" className="btn" onClick={() => onApproveExplain?.(turn.utt, turn.explainDraft!.text)}>
            {t(myLang, "summary.approve")}
          </button>
        </div>
      ) : null}
      {turn.explainSent && (myRole === "b" || turn.explainSent.target_lang === myLang) ? (
        <div className="turn__explain is-sent">
          <span className="label">{t(myLang, "talk.explain_simply")}</span>
          <p dir="auto" lang={turn.explainSent.target_lang}>
            {turn.explainSent.text}
          </p>
        </div>
      ) : null}
      {mine && myRole === "a" && onExplain && finalSrc && !turn.explainDraft ? (
        <button type="button" className="turn__action" onClick={() => onExplain(turn.utt)}>
          {t(myLang, "talk.explain_simply")}
        </button>
      ) : null}

      {open ? (
        <section className="proof" aria-label={t(myLang, "talk.proof")}>
          <h3 className="label">{t(myLang, "talk.proof")}</h3>
          {facts.length ? (
            <table className="proof__facts">
              <tbody>
                {facts.map((f, i) => (
                  <tr key={i} className={f.match ? "" : "is-bad"}>
                    <th scope="row">{t(myLang, `kind.${f.kind}`)}</th>
                    <td className="mono">{f.src ?? "—"}</td>
                    <td className="mono">{f.tgt ?? "—"}</td>
                    <td className="proof__ok">{f.match ? "=" : "≠"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
          {v ? (
            <dl className="proof__dl">
              <div>
                <dt>{t(myLang, "talk.similarity")}</dt>
                <dd className="mono">{v.similarity != null ? v.similarity.toFixed(2) : "—"}</dd>
              </div>
              {v.back_translation ? (
                <div>
                  <dt>{t(myLang, "talk.back_translation")}</dt>
                  <dd dir="auto">{v.back_translation}</dd>
                </div>
              ) : null}
              {v.reasons?.length ? (
                <div>
                  <dt>·</dt>
                  <dd>{v.reasons.join("; ")}</dd>
                </div>
              ) : null}
            </dl>
          ) : null}
          {timing ? (
            <div className="proof__chain">
              <Timeline timing={timing} lang={myLang} />
              {firstMs != null ? (
                <p className="proof__total mono">
                  {t(myLang, "talk.time")}: {fmtMs(firstMs)}
                </p>
              ) : null}
            </div>
          ) : null}
        </section>
      ) : null}
    </article>
  );
}

function RepeatBack({ r, lang }: { r: NonNullable<TurnT["repeatBack"]>; lang: string }) {
  const state = r.understood === "yes" ? "green" : r.understood === "partly" ? "amber" : "red";
  return (
    <div className={`turn__rb turn__rb--${state}`}>
      <span className="label">{t(lang, "talk.repeat_back")}</span>
      <VerifyMark state={state} lang={lang} />
      {r.missing.length ? <p dir="auto">− {r.missing.join(", ")}</p> : null}
      {r.wrong.length ? <p dir="auto">≠ {r.wrong.join(", ")}</p> : null}
    </div>
  );
}
