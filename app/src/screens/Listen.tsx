/** Listen-only `/l/:room` (jury QR, §9.2): no microphone, works over plain HTTP via the WS mirror. */
import { useEffect, useMemo, useRef } from "react";
import { useParams } from "react-router-dom";
import { eventsSocketUrl } from "../lib/api";
import { isRtl, t } from "../lib/i18n";
import { useEventSocket } from "../lib/live";
import { deviceLang } from "../lib/session";
import { RoomStore, checkState, useRoomModel } from "../lib/store";
import { VerifyMark } from "../components/Drawing";
import "./screens.css";

export default function Listen() {
  const { room = "" } = useParams();
  const lang = deviceLang();
  const store = useMemo(() => new RoomStore(room), [room]);
  const link = useEventSocket(room, store, eventsSocketUrl(room));
  const m = useRoomModel(store);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => end.current?.scrollIntoView({ behavior: "smooth", block: "end" }), [m.version]);

  const turns = m.turns.filter((tu) => tu.text || tu.partial);
  return (
    <div className="screen screen--listen" lang={lang} dir={isRtl(lang) ? "rtl" : "ltr"}>
      <header className="tblock">
        <div className="tblock__row">
          <span className="tblock__name">RAUM</span>
          <span className="tblock__gloss mono">{room.toUpperCase()}</span>
          <span className={`tblock__status ${link === "connected" ? "is-live" : ""}`}>
            <span className="tblock__dot" aria-hidden="true" />{" "}
            {link === "connected" ? "Live" : t(lang, link === "reconnecting" ? "talk.reconnecting" : "talk.connecting")}
          </span>
        </div>
      </header>
      <main className="listen">
        {turns.map((tu) => {
          const view = tu.translations[lang] ? lang : tu.lang === "de" ? Object.keys(tu.translations)[0] ?? tu.lang : "de";
          const text = view === tu.lang ? tu.text ?? tu.partial : tu.translations[view]?.text ?? tu.text ?? tu.partial;
          return (
            <article key={tu.utt} className="listen__turn">
              <p lang={view} dir={isRtl(view) ? "rtl" : "ltr"}>{text}</p>
              {view !== tu.lang && !tu.continuedIn ? <VerifyMark state={checkState(tu, view)} lang={lang} /> : null}
            </article>
          );
        })}
        {!turns.length ? <p className="listen__idle">{t(lang, "talk.waiting")}</p> : null}
        <div ref={end} />
      </main>
    </div>
  );
}
