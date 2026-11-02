/** Talk `/r/:room`: the other person's words in my language are the hero. */
import { useEffect, useMemo, useRef, useState } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { api, eventsSocketUrl } from "../lib/api";
import { isRtl, pick, t } from "../lib/i18n";
import { useEventSocket, useLiveRoom, type LiveSession } from "../lib/live";
import { clearJoin, loadJoin } from "../lib/session";
import { tick, unlockSound } from "../lib/sound";
import { RoomStore, checkState, openFlag, useRoomModel, type Turn } from "../lib/store";
import { ConfirmSheet, TalkControl, TitleBlock } from "../components/Sheet";
import { TurnView } from "../components/Turn";
import { RoomPlan } from "../components/Plan";
import { Ledger, Pause, Person, Play, Repeat, Stop } from "../components/Icons";
import { OfferStrip } from "../components/Offers";
import "./screens.css";

export default function Talk() {
  const { room = "" } = useParams();
  const join = useMemo(() => loadJoin(room), [room]);
  if (!join) return <Navigate to={`/j/${room}`} replace />;
  return <TalkInner room={room} join={join} />;
}

function TalkInner({ room, join }: { room: string; join: NonNullable<ReturnType<typeof loadJoin>> }) {
  const nav = useNavigate();
  const store = useMemo(() => new RoomStore(room), [room]);
  const session: LiveSession = useMemo(
    () => ({ room, token: join.token, livekit_url: join.livekit_url, identity: join.identity, lang: join.lang }),
    [room, join],
  );
  const live = useLiveRoom(session, store);
  // Second source: core's WS mirror (deduplicated). Keeps the sheet whole if the data channel hiccups.
  useEventSocket(room, store, eventsSocketUrl(room));
  const m = useRoomModel(store);
  const mode = join.mode;
  const myLang = join.lang;
  const myRole = join.role;
  const dir = isRtl(myLang) ? "rtl" : "ltr";
  const [menu, setMenu] = useState(false);
  const [answered, setAnswered] = useState<Set<string>>(() => new Set());
  const [ending, setEnding] = useState(false);
  const [repeatBackArmed, setRepeatBackArmed] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const autoMic = useRef(false);
  const ticked = useRef<Set<string>>(new Set());

  const participants = Object.values(m.participants).filter((p) => p.role === "a" || p.role === "b");
  const others = participants.filter((p) => p.id !== join.identity);
  const other = others.find((p) => p.role !== myRole) ?? others[0];
  const otherRole = myRole === "a" ? "b" : "a";
  const otherLang = other?.lang ?? (otherRole === "a" ? mode.roles.a.default_lang : null);
  const offRecord = !!m.session?.off_record;
  const me = m.participants[join.identity];

  const roleLabel = (role: string | null) =>
    role === "a" || role === "b" ? pick(mode.roles[role].label, myLang) : "";

  // Auto-start the microphone once connected (the consent tap was the gesture).
  useEffect(() => {
    if (live.state === "connected" && !autoMic.current) {
      autoMic.current = true;
      void live.setMic(true);
    }
  }, [live.state, live]);

  // Every conversation ends on the summary.
  useEffect(() => {
    if (m.summary || m.burn || m.session?.status === "ended" || m.session?.status === "burned") {
      nav(`/r/${room}/summary`);
    }
  }, [m.summary, m.burn, m.session?.status, nav, room]);

  const turns = m.turns.filter((tu) => tu.text || tu.partial || Object.keys(tu.translations).length || tu.uncertain);

  // Recency ramp over the other person's turns.
  const theirIdx = turns.map((tu, i) => (tu.speaker !== join.identity ? i : -1)).filter((i) => i >= 0);
  const rampOf = (i: number): 0 | 1 | 2 | 3 => {
    const pos = theirIdx.length - 1 - theirIdx.indexOf(i);
    return (pos <= 0 ? 0 : pos >= 3 ? 3 : pos) as 0 | 1 | 2 | 3;
  };

  // Signature tick when the other person's sentence is verified in my language.
  useEffect(() => {
    for (const tu of turns) {
      if (tu.speaker === join.identity || ticked.current.has(tu.utt)) continue;
      if (checkState(tu, myLang) === "green") {
        ticked.current.add(tu.utt);
        if (Date.now() - (tu.endedAt ?? tu.startedAt) < 15000) tick();
      }
    }
  }, [m.version]); // eslint-disable-line react-hooks/exhaustive-deps

  // Keep the newest sentence in view.
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 240;
    if (near) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [m.turns]);

  // The one interruption: an open confirm flag for my role.
  let pending: { turn: Turn; flag: NonNullable<ReturnType<typeof openFlag>> } | null = null;
  for (let i = turns.length - 1; i >= 0 && !pending; i--) {
    const f = openFlag(turns[i], myRole);
    if (f && !answered.has(f.id ?? "")) pending = { turn: turns[i], flag: f };
  }

  const control = (action: Parameters<typeof api.control>[1]["action"], extra: Record<string, unknown> = {}) =>
    api.control(room, { action, by: myRole, ...extra }).catch((e) => console.warn("[raum] control", e));

  const answer = (accepted: boolean) => {
    if (!pending) return;
    const id = pending.flag.id ?? "";
    setAnswered((s) => new Set(s).add(id));
    void control("confirm", { flag_id: id, accepted, utt: pending.turn.utt });
  };

  const end = async () => {
    setEnding(true);
    setMenu(false);
    try {
      await api.end(room);
    } catch (e) {
      console.warn("[raum] end", e);
    }
    nav(`/r/${room}/summary`);
  };

  const speakingOther = Object.keys(m.speaking).some((id) => id !== join.identity);
  const empty = turns.length === 0;

  return (
    <div className={`screen screen--talk ${offRecord ? "is-off" : ""}`} dir={dir} lang={myLang} onPointerDown={unlockSound}>
      <TitleBlock
        mode={mode}
        room={room}
        lang={myLang}
        pair={[myLang, otherLang]}
        path={me?.path ?? "direct"}
        rtt={live.rttMs}
        link={live.state}
        offRecord={offRecord}
        onMenu={() => setMenu(true)}
      />

      {live.state === "reconnecting" ? <div className="banner banner--warn" role="status">{t(myLang, "talk.reconnecting")}</div> : null}
      {live.state === "disconnected" ? (
        <div className="banner banner--bad" role="alert">
          {t(myLang, "talk.disconnected")}{" "}
          <button
            type="button"
            className="banner__btn"
            onClick={() => {
              clearJoin(room);
              nav(`/j/${room}?role=${myRole}&lang=${myLang}`);
            }}
          >
            {t(myLang, "talk.rejoin")}
          </button>
        </div>
      ) : null}
      {offRecord ? <div className="banner banner--off" role="status">{t(myLang, "talk.off_record_on")}</div> : null}
      {m.session?.human_interpreter_requested ? (
        <div className="banner" role="status">{t(myLang, "talk.human_requested")}</div>
      ) : null}
      {repeatBackArmed && myRole === "a" ? (
        <div className="banner" role="status">{t(myLang, "talk.repeat_back_on")}</div>
      ) : null}
      {mode.id === "verhandlungsraum" && m.offers.length ? <OfferStrip offers={m.offers} lang={myLang} labelOf={roleLabel} /> : null}

      <div className="talk-scroll" ref={scroller}>
        {empty ? (
          <div className="talk-empty">
            <RoomPlan
              mode={mode.id}
              a={participants.some((p) => p.role === "a")}
              b={participants.some((p) => p.role === "b")}
              labelA={pick(mode.roles.a.label, myLang)}
              labelB={pick(mode.roles.b.label, myLang)}
            />
            <h2 className="talk-empty__title">{speakingOther ? `${roleLabel(otherRole)} ${t(myLang, "talk.speaking")}` : t(myLang, "talk.waiting")}</h2>
            <p className="talk-empty__body">{t(myLang, "talk.waiting_body")}</p>
          </div>
        ) : (
          <div className="turns">
            {turns.map((tu, i) => {
              const mine = tu.speaker === join.identity;
              return (
                <TurnView
                  key={tu.utt}
                  turn={tu}
                  myLang={myLang}
                  otherLang={otherLang}
                  mine={mine}
                  ramp={mine ? 2 : rampOf(i)}
                  speakerLabel={mine ? t(myLang, "talk.you_said") : roleLabel(tu.role) || "·"}
                  myRole={myRole}
                  onExplain={mode.buttons.includes("explain_simply") ? (utt) => void control("explain_simply", { utt }) : undefined}
                  onApproveExplain={(utt, text) => void control("explain_approve", { utt, text })}
                />
              );
            })}
          </div>
        )}
      </div>

      <nav className="dock" aria-label={t(myLang, "talk.menu")}>
        <button
          type="button"
          className={`dock__side ${offRecord ? "is-active" : ""}`}
          onClick={() => void control("off_record", { value: !offRecord })}
          aria-pressed={offRecord}
        >
          {offRecord ? <Play /> : <Pause />}
          <span>{offRecord ? t(myLang, "talk.resume") : t(myLang, "talk.off_record")}</span>
        </button>
        <TalkControl
          on={live.micOn && !offRecord}
          level={offRecord ? 0 : live.level}
          disabled={offRecord || live.state !== "connected"}
          lang={myLang}
          error={live.micError}
          onToggle={() => {
            unlockSound();
            void live.setMic(!live.micOn);
          }}
        />
        {myRole === "a" && mode.buttons.includes("repeat_back") ? (
          <button
            type="button"
            className={`dock__side ${repeatBackArmed ? "is-active" : ""}`}
            aria-pressed={repeatBackArmed}
            onClick={() => {
              setRepeatBackArmed(true);
              void control("repeat_back");
            }}
          >
            <Repeat />
            <span>{t(myLang, "talk.repeat_back")}</span>
          </button>
        ) : mode.buttons.includes("human_interpreter") ? (
          <button
            type="button"
            className={`dock__side ${m.session?.human_interpreter_requested ? "is-active" : ""}`}
            onClick={() => void control("human_interpreter")}
          >
            <Person />
            <span>{t(myLang, "talk.human_interpreter")}</span>
          </button>
        ) : (
          <span className="dock__side dock__side--empty" />
        )}
      </nav>

      {menu ? (
        <div className="menu" role="dialog" aria-modal="true" aria-label={t(myLang, "talk.menu")} onClick={() => setMenu(false)}>
          <div className="menu__sheet" onClick={(e) => e.stopPropagation()}>
            <ul className="menu__list">
              {myRole === "a" && mode.buttons.includes("repeat_back") ? (
                <li>
                  <button
                    type="button"
                    onClick={() => {
                      setRepeatBackArmed(true);
                      setMenu(false);
                      void control("repeat_back");
                    }}
                  >
                    <Repeat /> {t(myLang, "talk.repeat_back")}
                  </button>
                </li>
              ) : null}
              {mode.buttons.includes("human_interpreter") ? (
                <li>
                  <button
                    type="button"
                    onClick={() => {
                      setMenu(false);
                      void control("human_interpreter");
                    }}
                  >
                    <Person /> {t(myLang, "talk.human_interpreter")}
                  </button>
                </li>
              ) : null}
              {mode.buttons.includes("offer_tracker") ? (
                <li>
                  <a href={`/t/${room}`} target="_blank" rel="noreferrer">
                    <Ledger /> {t(myLang, "talk.offers")}
                  </a>
                </li>
              ) : null}
              <li>
                <button type="button" onClick={() => void end()} disabled={ending}>
                  <Stop /> {t(myLang, "talk.end")}
                </button>
              </li>
            </ul>
            <button type="button" className="btn btn--ghost btn--block" onClick={() => setMenu(false)}>
              {t(myLang, "talk.close")}
            </button>
          </div>
        </div>
      ) : null}

      {pending ? (
        <ConfirmSheet
          flag={pending.flag}
          lang={myLang}
          onAnswer={answer}
          mismatches={(pending.turn.facts[myLang] ?? Object.values(pending.turn.facts).flat()).filter((f) => !f.match)}
        />
      ) : null}
    </div>
  );
}
