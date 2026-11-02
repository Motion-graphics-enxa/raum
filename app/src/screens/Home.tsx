/** `/`: open a room on this box. (The Persuade landing page is a separate surface, Sprint 3.) */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, listOf, type ModeConfig } from "../lib/api";
import { pick, t } from "../lib/i18n";
import { deviceLang } from "../lib/session";
import { Hatch } from "../components/Drawing";
import { ROOM_ICONS } from "../components/Icons";
import { RoomCodes } from "./Admin";
import "./screens.css";
import "./admin.css";

export default function Home() {
  const lang = deviceLang() === "de" ? "de" : "en";
  const [modes, setModes] = useState<ModeConfig[]>([]);
  const [room, setRoom] = useState<{ id: string; mode: ModeConfig } | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    api
      .modes()
      .then((m) => setModes(listOf<ModeConfig>(m, "modes")))
      .catch(() => setErr(true));
  }, []);

  const open = async (m: ModeConfig) => {
    const r = await api.createRoom(m.id);
    setRoom({ id: r.room, mode: m });
  };

  return (
    <div className="screen screen--home" lang={lang}>
      <header className="tblock home__top">
        <div className="home__brand">
          <span className="home__word">RAUM</span>
          <span className="home__slogan">{t(lang, "app.slogan")}</span>
        </div>
        <p className="home__tag">{t(lang, "app.tagline")}</p>
      </header>
      <main className="home">
        {err ? <p className="banner banner--bad">raum-core is not reachable.</p> : null}
        <ul className="home__rooms">
          {modes.map((m) => {
            const Icon = ROOM_ICONS[m.icon];
            return (
              <li key={m.id}>
                <button type="button" className={`home__room home__room--${m.id}`} onClick={() => void open(m)}>
                  <Hatch mode={m.id} size={44} />
                  <span className="home__room-text">
                    <span className="home__room-name">
                      {Icon ? <Icon /> : null} {m.name.de}
                    </span>
                    <span className="home__room-sub">
                      {pick(m.roles.a.label, lang)} · {pick(m.roles.b.label, lang)} — {pick(m.tagline ?? {}, lang)}
                    </span>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
        {room ? (
          <section className="home__codes" aria-live="polite">
            <h2 className="admin__h">
              {room.mode.name.de} · <span className="mono">{room.id}</span>
            </h2>
            <RoomCodes room={room.id} base={location.origin} mode={room.mode} />
          </section>
        ) : null}
        <p className="home__links">
          <Link to="/admin">Admin</Link>
        </p>
      </main>
    </div>
  );
}
