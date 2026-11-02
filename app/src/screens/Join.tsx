/** Join `/j/:room?role=a|b`: one scan, zero decisions (C-01, C-02, C-03). */
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, ApiError, listOf, type ModeConfig } from "../lib/api";
import type { Role } from "../lib/events";
import { LANGS, isRtl, langName, pick, t } from "../lib/i18n";
import { deviceKind, deviceLang, rememberLang, saveJoin } from "../lib/session";
import { unlockSound } from "../lib/sound";
import { Hatch } from "../components/Drawing";
import { Globe } from "../components/Icons";
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

  const options = useMemo(() => (LANGS.includes(lang) ? LANGS : [lang, ...LANGS]), [lang]);
  const dir = isRtl(lang) ? "rtl" : "ltr";

  const join = async () => {
    setBusy(true);
    setError(null);
    unlockSound();
    try {
      rememberLang(lang);
      const j = await api.join({ room, role, lang, device: deviceKind(), consent: true });
      saveJoin(j);
      nav(`/r/${room}`);
    } catch (e) {
      setError(e instanceof ApiError ? `${t(lang, "join.error")} (${e.status})` : t(lang, "join.error"));
      setBusy(false);
    }
  };

  return (
    <div className="screen screen--join" dir={dir} lang={lang}>
      <header className={`tblock tblock--${mode?.id ?? "x"}`}>
        <div className="tblock__row">
          {mode ? <Hatch mode={mode.id} size={22} className="tblock__hatch" /> : null}
          <div className="tblock__title">
            <span className="tblock__name">{mode ? mode.name.de : "RAUM"}</span>
            {mode && lang !== "de" ? <span className="tblock__gloss">{pick(mode.name, lang)}</span> : null}
          </div>
          <span className="tblock__status mono">{room.toUpperCase()}</span>
        </div>
        <div className="join__brand">
          <span className="join__word">RAUM</span>
          <span className="join__slogan">{t(lang, "app.slogan")}</span>
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
            {mode ? mode.roles[role].label[lang] ?? pick(mode.roles[role].label, lang) : t(lang, `role.${role}`)}
          </span>
        </h1>

        <section className="join__lang">
          <label className="label" htmlFor="lang">
            {t(lang, "join.language")}
          </label>
          <div className="field">
            <Globe />
            <select id="lang" value={lang} onChange={(e) => setLang(e.target.value)}>
              {options.map((l) => (
                <option key={l} value={l}>
                  {langName(l)}
                  {l !== lang ? ` · ${langName(l, lang)}` : ""}
                </option>
              ))}
            </select>
            <span className="field__hint mono" aria-hidden="true">
              {t(lang, "join.change")}
            </span>
          </div>
        </section>

        <section className="join__consent" aria-labelledby="consent-title">
          <h2 id="consent-title" className="join__consent-title">
            {t(lang, "join.consent.title")}
          </h2>
          <p className="join__consent-body">{t(lang, "join.consent.body")}</p>
          {lang !== "de" ? (
            <p className="join__consent-de" lang="de" dir="ltr">
              {t("de", "join.consent.title")} {t("de", "join.consent.body")}
            </p>
          ) : null}
          <p className="label join__aiact">{t(lang, "join.consent.ai_act")}</p>
        </section>

        <div className="join__go">
          {error ? (
            <p className="join__error" role="alert">
              {error}
            </p>
          ) : null}
          <button type="button" className="btn btn--lg btn--block" onClick={join} disabled={busy || !mode}>
            {busy ? t(lang, "join.joining") : t(lang, "join.continue")}
          </button>
          <p className="join__note">{t(lang, "join.mic_note")}</p>
        </div>
      </main>
    </div>
  );
}
