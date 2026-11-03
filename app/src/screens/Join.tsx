/** Join `/j/:room?role=a|b`: one scan, zero decisions (C-01, C-02, C-03). */
import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, ApiError, listOf, useFeatures, type ModeConfig } from "../lib/api";
import type { Role } from "../lib/events";
import { UI_LANG, pick, t } from "../lib/i18n";
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
import "./screens.css";

export async function resolveMode(room: string): Promise<ModeConfig | null> {
  try {
    const info = await api.room(room);
    if (info && typeof info.mode === "object") return info.mode as ModeConfig;
  } catch {
    /* fall through */
  }
  try {
    const modes = listOf<ModeConfig>(await api.modes(), "modes");
    const prefix = room.split("-")[0];
    return modes.find((m) => m.prefix === prefix) ?? null;
  } catch {
    return null;
  }
}

export default function Join() {
  const { room = "" } = useParams();
  const [qs] = useSearchParams();
  const role: Role = qs.get("role") === "a" ? "a" : "b";
  const nav = useNavigate();
  const [mode, setMode] = useState<ModeConfig | null>(null);
  const [lang, setLang] = useState(() => qs.get("lang") ?? deviceLang());
  const [audioOut, setAudioOut] = useState<AudioOut>(deviceAudioOut);
  const [voice, setVoice] = useState<Voice>(deviceVoice);
  // C-13: the voice choice and its disclosure only when this box speaks in a copy of the speaker's voice
  const feats = useFeatures();
  const voiceClone = !!feats?.voice_clone;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    resolveMode(room).then((m) => {
      if (!alive) return;
      if (m) setMode(m);
      else setLoadFailed(true);
    });
    return () => {
      alive = false;
    };
  }, [room]);

  const dir = "ltr"; // the chrome is English on every phone; the language picker names each language in its own script

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

  return (
    <div className={`screen screen--join room--${mode?.id ?? "x"}`} dir={dir} lang={UI_LANG}>
      <header className={`tblock tblock--join tblock--${mode?.id ?? "x"}`}>
        <div className="join__brand">
          <span className="join__word">RAUM</span>
          <span className="join__slogan">{t(lang, "app.slogan")}</span>
        </div>
        <div className="tblock__row">
          {mode ? <Hatch mode={mode.id} size={30} className="tblock__hatch" /> : null}
          <div className="tblock__title">
            <span className="tblock__name">{mode ? pick(mode.name, UI_LANG) : "RAUM"}</span>
            {mode && mode.name.de ? <span className="tblock__gloss">{mode.name.de}</span> : null}
          </div>
          <span className="tblock__status mono">{room.toUpperCase()}</span>
        </div>
      </header>

      <main className="join">
        {loadFailed ? (
          <p className="join__error" role="alert">
            {t(lang, "join.error")} <span className="mono">{room}</span>
          </p>
        ) : null}

        <h1 className="join__role" aria-live="polite">
          <span className="join__role-pre">{t(lang, "join.you_are")} </span>
          <span className="join__role-name">
            {mode ? pick(mode.roles[role].label, UI_LANG) : t(lang, `role.${role}`)}
          </span>
        </h1>

        <section className="join__lang">
          <label className="label" htmlFor="lang">
            {t(lang, "join.language")}
          </label>
          <LanguagePicker lang={lang} onChange={setLang} uiLang={UI_LANG} />
        </section>

        <section className="join__lang">
          <span className="label">{t(lang, "join.audio_out")}</span>
          <AudioOutPicker value={audioOut} onChange={setAudioOut} uiLang={UI_LANG} />
        </section>

        {voiceClone ? (
          <section className="join__lang">
            <span className="label">{t(lang, "join.voice")}</span>
            <VoicePicker value={voice} onChange={setVoice} uiLang={lang} />
          </section>
        ) : null}

        <section className="join__consent" aria-labelledby="consent-title">
          <h2 id="consent-title" className="join__consent-title">
            {t(lang, "join.consent.title")}
          </h2>
          <p className="join__consent-body">{t(lang, "join.consent.body")}</p>
          {voiceClone ? <p className="join__consent-body">{t(lang, "join.consent.voice")}</p> : null}
          <p className="join__consent-body">{t(lang, "join.mic_note")}</p>
          <p className="label join__aiact">{t(lang, "join.consent.ai_act")}</p>
        </section>

        <div className="join__go">
          {error ? (
            <p className="join__error" role="alert">
              {error}
            </p>
          ) : null}
          {/* EU AI Act Art. 50: said where the person acts, whatever the phone's height hides of the sheet above */}
          <p className="join__disclose">{t(lang, "join.consent.title")}</p>
          <button type="button" className="btn btn--lg btn--block" onClick={join} disabled={busy || !mode || !feats}>
            {busy ? t(lang, "join.joining") : t(lang, "join.continue")}
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
