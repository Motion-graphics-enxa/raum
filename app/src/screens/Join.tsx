/** Join `/j/:room?role=a|b`: one scan, zero decisions (C-01, C-02, C-03).
 *
 *  Across the desk: the door plate names the room; two seats face each other across one desk, split by one
 *  centre line with the box sitting on it as its one key; your seat stands in the window's light with your
 *  language in its own script. Settled choices show as their value with a Change; one tap on the dado sits
 *  you down. */
import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, ApiError, listOf, useFeatures, type ModeConfig } from "../lib/api";
import type { ParticipantInfo, Role } from "../lib/events";
import { UI_LANG, langName, pick, t } from "../lib/i18n";
import {
  deviceAudioOut,
  deviceKind,
  deviceLang,
  deviceVoice,
  rememberAudioOut,
  rememberLang,
  rememberVoice,
  saveJoin,
} from "../lib/session";
import { AudioOutPicker, LanguagePicker, VoicePicker, type AudioOut, type Voice } from "../components/Pickers";
import { unlockSound } from "../lib/sound";
import { Hatch } from "../components/Drawing";
import { ArrowRight, Globe, Headphones, Pair, Speaker } from "../components/Icons";
import "./screens.css";
import "./join.css";

export async function resolveMode(room: string): Promise<ModeConfig | null> {
  return (await resolveRoom(room)).mode;
}

/** The room's mode, and who already sits in it when the box says so. */
async function resolveRoom(room: string): Promise<{ mode: ModeConfig | null; people: ParticipantInfo[] }> {
  try {
    const info = await api.room(room);
    if (info && typeof info.mode === "object") {
      return { mode: info.mode as ModeConfig, people: Array.isArray(info.participants) ? info.participants : [] };
    }
  } catch {
    /* fall through */
  }
  try {
    const modes = listOf<ModeConfig>(await api.modes(), "modes");
    const prefix = room.split("-")[0];
    return { mode: modes.find((m) => m.prefix === prefix) ?? null, people: [] };
  } catch {
    return { mode: null, people: [] };
  }
}

/** Sentence by sentence: the consent reads as the box's house rules, one line each. */
function sentences(text: string): string[] {
  return text
    .split(/(?<=[.!?])\s+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function Join() {
  const { room = "" } = useParams();
  const [qs] = useSearchParams();
  const role: Role = qs.get("role") === "a" ? "a" : "b";
  const otherRole: Role = role === "a" ? "b" : "a";
  const nav = useNavigate();
  const [mode, setMode] = useState<ModeConfig | null>(null);
  const [people, setPeople] = useState<ParticipantInfo[]>([]);
  const [lang, setLang] = useState(() => qs.get("lang") ?? deviceLang());
  const [langOpen, setLangOpen] = useState(false); // your seat's chooser: language and how you hear
  const [relit, setRelit] = useState(false); // a changed language: the light falls again at once, not after the entrance
  const [audioOut, setAudioOut] = useState<AudioOut>(deviceAudioOut);
  const [voice, setVoice] = useState<Voice>(deviceVoice);
  // C-13: the voice choice and its disclosure only when this box speaks in a copy of the speaker's voice
  const feats = useFeatures();
  const voiceClone = !!feats?.voice_clone;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const changeBtn = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let alive = true;
    resolveRoom(room).then(({ mode: m, people: p }) => {
      if (!alive) return;
      if (m) {
        setMode(m);
        setPeople(p);
      } else setLoadFailed(true);
    });
    return () => {
      alive = false;
    };
  }, [room]);

  const dir = "ltr"; // the chrome is English on every phone; each language name carries its own script and direction

  // The other seat: who is there and the language they speak. A person already in the room speaks for
  // themselves; otherwise the professional's preset language (as on Talk), never a guess for the other party.
  const other = people.find((p) => p.role === otherRole);
  const otherLang = other?.lang ?? (otherRole === "a" && mode ? mode.roles.a.default_lang : null);
  const otherKnown = !!otherLang && otherLang !== "auto";
  const sameLang = !!other && other.lang === lang;
  const otherLabel = mode ? pick(mode.roles[otherRole].label, UI_LANG) : t(lang, `role.${otherRole}`);
  const myLabel = mode ? pick(mode.roles[role].label, UI_LANG) : t(lang, `role.${role}`);

  // a choice applies at once and the panel closes: your seat shows it, the light falls on it again, and focus
  // goes back to the seat (the chips leave with the panel)
  const settle = () => {
    setRelit(true);
    setLangOpen(false);
    requestAnimationFrame(() => changeBtn.current?.focus());
  };
  const chooseLang = (l: string) => {
    setLang(l);
    settle();
  };
  const chooseAudioOut = (v: AudioOut) => {
    setAudioOut(v);
    settle();
  };

  const join = async () => {
    setBusy(true);
    setError(null);
    unlockSound();
    try {
      rememberLang(lang);
      rememberAudioOut(audioOut);
      if (voiceClone) rememberVoice(voice);
      // never a copy of a voice whose owner was not shown the choice: "standard" unless it was on screen
      const j = await api.join({
        room,
        role,
        lang,
        device: deviceKind(),
        consent: true,
        audio_out: audioOut,
        voice: voiceClone ? voice : "standard",
      });
      saveJoin(j);
      nav(`/r/${room}`);
    } catch (e) {
      setError(e instanceof ApiError ? `${t(lang, "join.error")} (${e.status})` : t(lang, "join.error"));
      setBusy(false);
    }
  };

  const promise = [
    ...sentences(t(lang, "join.consent.body")),
    ...(voiceClone ? [t(lang, "join.consent.voice")] : []),
    t(lang, "join.mic_note"),
  ];

  return (
    <div className={`screen screen--join room--${mode?.id ?? "x"}`} dir={dir} lang={UI_LANG}>
      <header className="jdoor">
        <p className="jdoor__house">
          <span className="join__word">RAUM</span>
          <span className="jdoor__slogan">{t(lang, "app.slogan")}</span>
        </p>
        <div className="jdoor__plate">
          {mode ? <Hatch mode={mode.id} size={34} className="jdoor__hatch" /> : null}
          <div className="jdoor__title">
            <span className="jdoor__name">{mode ? pick(mode.name, UI_LANG) : room.toUpperCase()}</span>
            {/* before the room is known the plate itself shows the id: never the same id twice */}
            <span className="jdoor__gloss" hidden={!mode}>
              {mode && mode.name.de ? (
                <>
                  <span className="jdoor__de">{mode.name.de}</span>
                  <span className="jdoor__sep" aria-hidden="true">
                    ·
                  </span>
                </>
              ) : null}
              <span className="mono jdoor__id">{room.toUpperCase()}</span>
            </span>
          </div>
        </div>
      </header>

      <main className="join">
        {loadFailed ? (
          // the plate above already shows the room id: the banner says what went wrong, once
          <p className="banner banner--bad join__banner" role="alert">
            {t(lang, "join.error")}
          </p>
        ) : null}

        {loadFailed ? null : (
          <h1 className="join__role" aria-live="polite">
            <span className="join__role-pre">{t(lang, "join.you_are")} </span>
            <span className="join__role-name">{myLabel}</span>
          </h1>
        )}

        {loadFailed ? null : (
        <div className="desk" data-busy={busy || undefined} data-again={relit || undefined}>
          <div className="desk__seats">
            <div className="seat seat--you">
              <span className="seat__light" key={`${lang}-${audioOut}`} aria-hidden="true" />
              <span className="seat__plate seat__plate--you">{t(lang, "talk.you_label")}</span>
              <button
                ref={changeBtn}
                type="button"
                className="seat__lang-btn"
                aria-expanded={langOpen}
                aria-controls="join-langs"
                onClick={() => setLangOpen((o) => !o)}
              >
                <span className="visually-hidden">{t(lang, "join.language")}: </span>
                <span className="seat__lang" key={lang} lang={lang} dir="auto">
                  {langName(lang)}
                </span>
                {lang !== UI_LANG ? <span className="seat__en">{langName(lang, UI_LANG)}</span> : null}
                <span className="visually-hidden">. {t(lang, "join.audio_out")} </span>
                <span className="seat__hear" key={audioOut}>
                  {audioOut === "headphones" ? <Headphones /> : <Speaker />}
                  {t(lang, `join.${audioOut}`)}
                </span>
                <span className="seat__change">
                  <Globe />
                  {langOpen ? t(lang, "talk.close") : t(lang, "join.change")}
                </span>
              </button>
            </div>
            <div className={`seat seat--them ${otherKnown ? "" : "is-empty"}`}>
              <span className="seat__plate seat__plate--them">{otherLabel}</span>
              {otherKnown ? (
                <span className="seat__said">
                  <span className="seat__lang" lang={otherLang} dir="auto">
                    {langName(otherLang)}
                  </span>
                  {otherLang !== UI_LANG ? <span className="seat__en">{langName(otherLang, UI_LANG)}</span> : null}
                </span>
              ) : (
                <span className="seat__wait">{t(lang, "talk.not_joined")}</span>
              )}
            </div>
            <span className="desk__rail" aria-hidden="true" />
          </div>

          <span className="desk__key" aria-hidden="true">
            <Pair />
          </span>

          {/* under the box, inside the desk: what needs a decision first, then the box's promise */}
          <div className="desk__foot">
            {sameLang ? (
              <p className="desk__note" role="status">
                {t(lang, "talk.same_lang").replace("{lang}", langName(lang, UI_LANG))}
              </p>
            ) : null}

            <div id="join-langs" className="desk__langs" hidden={!langOpen}>
              <label className="label" htmlFor="lang">
                {t(lang, "join.language")}
              </label>
              <LanguagePicker lang={lang} onChange={chooseLang} uiLang={UI_LANG} />
              <span className="label desk__langs-q" id="hear-label">
                {t(lang, "join.audio_out")}
              </span>
              <AudioOutPicker value={audioOut} onChange={chooseAudioOut} uiLang={UI_LANG} />
            </div>

            {/* C-13: always on screen when the box clones voices (the default is "own"): a copy of a voice is
                never chosen behind a fold, so it sits in the desk, above the promise and the dado */}
            {voiceClone ? (
              <section className="desk__langs desk__voice" aria-labelledby="voice-label">
                <span className="label" id="voice-label">
                  {t(lang, "join.voice")}
                </span>
                <VoicePicker value={voice} onChange={setVoice} uiLang={lang} />
              </section>
            ) : null}

            <section className="promise-block" aria-labelledby="consent-title">
              <h2 id="consent-title" className="visually-hidden">
                {t(lang, "join.consent.title")}
              </h2>
              <ul className="promise">
                {promise.map((x) => (
                  <li key={x}>{x}</li>
                ))}
              </ul>
              <p className="promise__law">{t(lang, "join.consent.ai_act")}</p>
            </section>
          </div>
        </div>
        )}

        <div className="join__go">
          {error ? (
            <p className="join__error" role="alert">
              {error}
            </p>
          ) : null}
          {/* EU AI Act Art. 50: said where the person acts, whatever the phone's height hides of the desk above */}
          <p className="join__disclose">{t(lang, "join.consent.title")}</p>
          <button type="button" className="btn btn--lg btn--block btn--wall" onClick={join} disabled={busy || !mode || !feats}>
            <span>{busy ? t(lang, "join.joining") : t(lang, "join.continue")}</span>
            {busy ? null : <ArrowRight />}
          </button>
          {inAndroidBrowser() ? (
            // F-09: on Android 12+ a QR code opens the browser; this hands the same join to the RAUM app
            <a className="join__app" href={`raum://j/${encodeURIComponent(room)}?role=${role}&lang=${encodeURIComponent(lang)}`}>
              {t(lang, "join.open_app")}
            </a>
          ) : null}
        </div>
      </main>
    </div>
  );
}

/** An Android phone in a browser (the RAUM app adds "RAUM-Android" to its user agent). */
function inAndroidBrowser(): boolean {
  const ua = typeof navigator === "undefined" ? "" : navigator.userAgent;
  return /Android/i.test(ua) && !/RAUM-Android/.test(ua);
}
