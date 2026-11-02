/** Talk `/r/:room`: the other person's words in my language are the hero. */
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { api, eventsSocketUrl, useFeatures } from "../lib/api";
import { UI_LANG, langName, pick, t } from "../lib/i18n";
import { useEventSocket, useLiveRoom, type LiveSession } from "../lib/live";
import {
  clearJoin,
  deviceAudioOut,
  deviceVoice,
  loadJoin,
  rememberAudioOut,
  rememberLang,
  rememberVoice,
  saveJoin,
} from "../lib/session";
import { AudioOutPicker, LanguagePicker, VoicePicker, type AudioOut, type Voice } from "../components/Pickers";
import { tick, unlockSound } from "../lib/sound";
import {
  RoomStore,
  checkState,
  factChips,
  flagStatus,
  otherLangOf,
  readBackFor,
  spokeSince,
  useRoomModel,
  type Turn,
} from "../lib/store";
import { FactNotes, ReadBack, TalkControl, TitleBlock } from "../components/Sheet";
import { DocumentCamera, DocumentCard } from "../components/DocumentCamera";
import { GlossaryUpload } from "../components/GlossaryUpload";
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
  const [myLang, setMyLang] = useState(join.lang);
  const [audioOut, setAudioOut] = useState<AudioOut>(() => deviceAudioOut());
  const [voice, setVoice] = useState<Voice>(() => deviceVoice());
  const voiceClone = !!useFeatures()?.voice_clone; // C-13: the choice only where the box copies voices
  const live = useLiveRoom(session, store, myLang);
  // Second source: core's WS mirror (deduplicated). Keeps the sheet whole if the data channel hiccups.
  const events = useEventSocket(room, store, eventsSocketUrl(room));
  const m = useRoomModel(store);
  // The text link has been down a moment (not a blip): say so quietly while it comes back.
  const [linkLate, setLinkLate] = useState(false);
  useEffect(() => {
    if (events !== "reconnecting") return;
    const id = window.setTimeout(() => setLinkLate(true), 1500);
    return () => {
      window.clearTimeout(id);
      setLinkLate(false);
    };
  }, [events]);
  const textDown = events === "reconnecting" && linkLate;
  const mode = join.mode;
  const myRole = join.role;
  // The chrome is English, left to right; each sentence carries its own lang and dir (Turn, ReadBack).
  const dir = "ltr";
  const [menu, setMenu] = useState(false);
  const [answered, setAnswered] = useState<Map<string, boolean>>(() => new Map()); // read-backs I tapped
  const [ending, setEnding] = useState(false);
  const [repeatBackArmed, setRepeatBackArmed] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const autoMic = useRef(false);
  const ticked = useRef<Set<string>>(new Set());

  const participants = Object.values(m.participants).filter((p) => p.role === "a" || p.role === "b");
  const otherRole = myRole === "a" ? "b" : "a";
  // The participants' real languages (then what the turns show); the mode's default only as a last resort.
  const otherLang = otherLangOf(
    participants,
    join.identity,
    myRole,
    myLang,
    m.turns,
    otherRole === "a" ? mode.roles.a.default_lang : null,
  );
  const offRecord = !!m.session?.off_record;
  const sameLang =
    participants.length >= 2 && participants.every((p) => p.lang === participants[0].lang) ? participants[0].lang : null;
  const voicePaused = m.voicePaused.includes(join.identity);

  const changeLang = async (lang: string) => {
    setMyLang(lang);
    rememberLang(lang);
    saveJoin({ ...join, lang });
    await api.participant(room, join.identity, { lang }).catch((e) => console.warn("[raum] language", e));
  };
  const changeAudioOut = async (v: AudioOut) => {
    setAudioOut(v);
    rememberAudioOut(v);
    await api.participant(room, join.identity, { audio_out: v }).catch((e) => console.warn("[raum] audio", e));
  };
  const changeVoice = async (v: Voice) => {
    setVoice(v);
    rememberVoice(v);
    await api.participant(room, join.identity, { voice: v }).catch((e) => console.warn("[raum] voice", e));
  };
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

  const turns = m.turns.filter(
    (tu) =>
      (tu.text || tu.partial || Object.keys(tu.translations).length || tu.uncertain) &&
      (!tu.ignored || tu.speaker === join.identity), // F-10: someone else's voice: only its own phone notes it
  );

  // Recency ramp over the other person's turns.
  const theirIdx = turns.map((tu, i) => (tu.speaker !== join.identity ? i : -1)).filter((i) => i >= 0);
  const rampOf = (i: number): 0 | 1 | 2 | 3 => {
    const pos = theirIdx.length - 1 - theirIdx.indexOf(i);
    return (pos <= 0 ? 0 : pos >= 3 ? 3 : pos) as 0 | 1 | 2 | 3;
  };

  // Signature tick when the other person's sentence is verified in my language.
  useEffect(() => {
    for (const tu of turns) {
      if (tu.speaker === join.identity || tu.continuedIn || ticked.current.has(tu.utt)) continue;
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

  // A new read-back for me comes into view once (it is not a modal, so it must not land under the dock).
  const shownAsk = useRef<string | null>(null);
  const newestAsk = (() => {
    for (let i = turns.length - 1; i >= 0; i--) {
      const f = readBackFor(turns[i], myRole, myLang);
      if (f) return turns[i].confirms[f.id ?? ""] ? null : f.id ?? null;
    }
    return null;
  })();
  useEffect(() => {
    const el = scroller.current;
    if (!newestAsk || shownAsk.current === newestAsk || !el) return;
    shownAsk.current = newestAsk;
    if (el.scrollHeight - el.scrollTop - el.clientHeight > 900) return; // reading far back: leave them there
    const id = setTimeout(() => {
      document.getElementById(`rb-${newestAsk}`)?.closest(".rb")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }, 120);
    return () => clearTimeout(id);
  }, [newestAsk]);

  const control = (action: Parameters<typeof api.control>[1]["action"], extra: Record<string, unknown> = {}) =>
    api.control(room, { action, by: myRole, ...extra }).catch((e) => console.warn("[raum] control", e));

  // A read-back is answered aloud or with its small buttons; a tap shows as answered at once.
  const answer = (tu: Turn, flagId: string, accepted: boolean) => {
    setAnswered((s) => new Map(s).set(flagId, accepted));
    void control("confirm", { flag_id: flagId, accepted, utt: tu.utt });
  };

  /** What this device adds to a turn: the read-back asked of me, and (the professional only) the fact notes. */
  const notesFor = (tu: Turn) => {
    const ask = readBackFor(tu, myRole, myLang);
    const chips = myRole === "a" ? factChips(tu, myLang, ask) : [];
    if (!ask && !chips.length) return null;
    const id = ask?.id ?? "";
    const tapped = answered.get(id);
    const confirm = ask ? tu.confirms[id] : undefined;
    const status = ask
      ? confirm || tapped === undefined
        ? flagStatus(tu, ask)
        : tapped
          ? "confirmed"
          : "rejected"
      : null;
    return (
      <>
        {ask ? (
          <ReadBack
            flag={ask}
            lang={myLang}
            status={status}
            answer={confirm}
            canSpeak={!spokeSince(turns, join.identity, tu)}
            onAnswer={(accepted) => answer(tu, id, accepted)}
          />
        ) : null}
        <FactNotes chips={chips} lang={myLang} />
      </>
    );
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
    <div
      className={`screen screen--talk room--${mode.id} ${offRecord ? "is-off" : ""}`}
      dir={dir}
      lang={UI_LANG}
      onPointerDown={() => {
        unlockSound();
        live.startAudio(); // iOS: the box's voice may only start from a tap
      }}
    >
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
        roles={[t(myLang, "talk.you_speak"), `${roleLabel(otherRole) || t(myLang, "talk.other")} ${roleLabel(otherRole) ? t(myLang, "talk.speaks") : ""}`.trim()]}
        audioOut={audioOut}
      />

      {live.state === "reconnecting" ? <div className="banner banner--warn banner--reconnect" role="status">{t(myLang, "talk.reconnecting")}</div> : null}
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
      {textDown && live.state !== "reconnecting" && live.state !== "disconnected" ? (
        <div className="banner banner--warn banner--reconnect" role="status">{t(myLang, "talk.reconnecting")}</div>
      ) : null}
      {live.audioBlocked && live.state === "connected" ? (
        <div className="banner" role="status">
          <button type="button" className="banner__btn" onClick={() => live.startAudio()}>
            {t(myLang, "talk.tap_to_hear")}
          </button>
        </div>
      ) : null}
      {offRecord ? <div className="banner banner--off" role="status">{t(myLang, "talk.off_record_on")}</div> : null}
      {sameLang ? (
        <div className="banner banner--warn" role="alert">
          {t(myLang, "talk.same_lang").replace("{lang}", langName(sameLang, UI_LANG))}{" "}
          <button type="button" className="banner__btn" onClick={() => setMenu(true)}>
            {t(myLang, "talk.change_lang")}
          </button>
        </div>
      ) : null}
      {voicePaused ? <div className="banner" role="status">{t(myLang, "talk.voice_playing")}</div> : null}
      {m.session?.human_interpreter_requested ? (
        <div className="banner" role="status">{t(myLang, "talk.human_requested")}</div>
      ) : null}
      {repeatBackArmed && myRole === "a" ? (
        <div className="banner" role="status">{t(myLang, "talk.repeat_back_on")}</div>
      ) : null}
      {mode.id === "verhandlungsraum" && m.offers.length ? <OfferStrip offers={m.offers} lang={myLang} labelOf={roleLabel} /> : null}

      <div className="talk-scroll" ref={scroller}>
        {empty && !m.documents.length ? (
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
            {docsBefore(m.documents, turns, -1).map((d) => (
              <DocumentCard key={`doc-${d.ts}`} doc={d} lang={myLang} />
            ))}
            {turns.map((tu, i) => {
              const mine = tu.speaker === join.identity;
              return (
                <Fragment key={tu.utt}>
                <TurnView
                  turn={tu}
                  myLang={myLang}
                  otherLang={otherLang}
                  mine={mine}
                  ramp={mine ? 2 : rampOf(i)}
                  speakerLabel={mine ? t(myLang, "talk.you_said") : roleLabel(tu.role) || "·"}
                  myRole={myRole}
                  onExplain={mode.buttons.includes("explain_simply") ? (utt) => void control("explain_simply", { utt }) : undefined}
                  onApproveExplain={(utt, text) => void control("explain_approve", { utt, text })}
                  checkerOff={mode.checker === "off"}
                  onRelearn={mine ? () => void api.control(room, { action: "voice_lock_reset", by: join.identity }).catch((e) => console.warn("[raum] control", e)) : undefined}
                  extra={notesFor(tu)}
                />
                {docsBefore(m.documents, turns, i).map((d) => (
                  <DocumentCard key={`doc-${d.ts}`} doc={d} lang={myLang} />
                ))}
                </Fragment>
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
            <section className="menu__sec">
              <span className="label">{t(myLang, "talk.language")}</span>
              <LanguagePicker id="talk-lang" lang={myLang} onChange={(l) => void changeLang(l)} uiLang={myLang} />
            </section>
            <section className="menu__sec">
              <span className="label">{t(myLang, "join.audio_out")}</span>
              <AudioOutPicker value={audioOut} onChange={(v) => void changeAudioOut(v)} uiLang={myLang} />
            </section>
            {voiceClone ? (
              <section className="menu__sec">
                <span className="label">{t(myLang, "join.voice")}</span>
                <VoicePicker value={voice} onChange={(v) => void changeVoice(v)} uiLang={myLang} />
              </section>
            ) : null}
            {mode.id !== "verhandlungsraum" && (myRole === "a" || myRole === "b") ? (
              <section className="menu__sec">
                <DocumentCamera room={room} role={myRole} lang={myLang} onRead={() => setMenu(false)} />
              </section>
            ) : null}
            {mode.id === "verhandlungsraum" && (myRole === "a" || myRole === "b") ? (
              <section className="menu__sec">
                <GlossaryUpload room={room} lang={myLang} role={myRole} compact />
              </section>
            ) : null}
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
    </div>
  );
}

/** Documents (F-07) whose arrival falls after turn ``i`` and before turn ``i + 1`` (``i = -1``: before the first). */
function docsBefore<D extends { ts: number }>(docs: D[], turns: { startedAt: number }[], i: number): D[] {
  if (!docs.length) return docs;
  const lo = i < 0 ? -Infinity : turns[i].startedAt;
  const hi = i + 1 < turns.length ? turns[i + 1].startedAt : Infinity;
  return docs.filter((d) => d.ts >= lo && d.ts < hi);
}
