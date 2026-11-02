/** Admin `/admin` (F-05 skeleton): rooms, devices, models, GPU, privacy meter, seal, burn-all, new room. */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, listOf, type ModeConfig } from "../lib/api";
import type { ParticipantInfo } from "../lib/events";
import { pick } from "../lib/i18n";
import { Hatch } from "../components/Drawing";
import { Key, Shield } from "../components/Icons";
import { QR } from "../components/QR";
import { pathLabel } from "../components/Sheet";
import "./screens.css";
import "./admin.css";

interface RoomRow {
  room: string;
  mode: string;
  status: string;
  participants: ParticipantInfo[];
  agent_connected: boolean;
  off_record?: boolean;
}

interface Status {
  profile: string;
  version?: string;
  uptime_s?: number;
  rooms: RoomRow[];
  models: Record<string, string>;
  gpu: { util_pct: number; models_loaded: string[]; source?: string };
  meter: { third_party_bytes: number; tunnel_bytes: number; sealed: boolean; source?: string };
  sealed: boolean;
  audit?: { head_hash: string };
}

export default function Admin() {
  const [st, setSt] = useState<Status | null>(null);
  const [modes, setModes] = useState<ModeConfig[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setSt((await api.adminStatus()) as unknown as Status);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    void api.modes().then((m) => setModes(listOf<ModeConfig>(m, "modes"))).catch(() => undefined);
    const iv = window.setInterval(load, 1000);
    return () => window.clearInterval(iv);
  }, [load]);

  const create = async (mode: string) => {
    setBusy(true);
    try {
      const r = await api.createRoom(mode);
      setCreated(r.room);
      await load();
    } finally {
      setBusy(false);
    }
  };

  const modeOf = (id: string) => modes.find((m) => m.id === id);
  const base = location.origin;

  return (
    <div className="screen screen--admin" lang="de">
      <header className="tblock">
        <div className="tblock__row">
          <span className="tblock__name">RAUM · Verwaltung</span>
          <span className="tblock__gloss mono">
            {st ? `profile ${st.profile}` : ""}
            {st?.version ? ` · v${st.version}` : ""}
          </span>
        </div>
      </header>
      <main className="admin">
        {err ? (
          <p className="banner banner--bad" role="alert">
            raum-core nicht erreichbar: {err}
          </p>
        ) : null}

        <section className="admin__band">
          <div className="admin__meter">
            <span className="admin__meter-n mono">{(st?.meter.third_party_bytes ?? 0).toLocaleString("de-DE")}</span>
            <span className="label">Bytes an Dritte</span>
          </div>
          <div className="admin__meter">
            <span className="admin__meter-n admin__meter-n--s mono">{((st?.meter.tunnel_bytes ?? 0) / 1024).toFixed(1)} KB</span>
            <span className="label">Tunnel zu eigenen Geräten</span>
          </div>
          <div className="admin__meter">
            <span className="admin__meter-n admin__meter-n--s mono">{st ? `${Math.round(st.gpu.util_pct)} %` : "—"}</span>
            <span className="label">GPU{st?.gpu.source === "mock" ? " · mock" : ""}</span>
          </div>
          <div className="admin__controls">
            <button
              type="button"
              className={`btn ${st?.sealed ? "btn--ok" : "btn--ghost"}`}
              onClick={() => void api.seal(!st?.sealed).then(load)}
              aria-pressed={!!st?.sealed}
            >
              <Shield /> {st?.sealed ? "Versiegelt" : "Versiegeln"}
            </button>
            <button
              type="button"
              className="btn btn--bad"
              onClick={() => {
                if (window.confirm("Alle Sitzungen vernichten?")) void api.burnAll().then(load);
              }}
            >
              <Key /> Alle vernichten
            </button>
          </div>
        </section>

        <section className="admin__sec">
          <h2 className="admin__h">Neuer Raum</h2>
          <div className="admin__new">
            {modes.map((m) => (
              <button key={m.id} type="button" className="admin__mode" onClick={() => void create(m.id)} disabled={busy}>
                <Hatch mode={m.id} size={28} />
                <span className="admin__mode-name">{m.name.de}</span>
                <span className="admin__mode-tag">{pick(m.tagline ?? {}, "de")}</span>
              </button>
            ))}
          </div>
          {created ? (
            <RoomCodes room={created} base={base} mode={modes.find((m) => created.startsWith(`${m.prefix}-`))} />
          ) : null}
        </section>

        <section className="admin__sec">
          <h2 className="admin__h">Räume</h2>
          <table className="admin__table">
            <thead>
              <tr>
                <th>Raum</th>
                <th>Modus</th>
                <th>Status</th>
                <th>Geräte</th>
                <th>Agent</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(st?.rooms ?? []).map((r) => (
                <tr key={r.room}>
                  <td className="mono">{r.room}</td>
                  <td>
                    <span className="admin__modecell">
                      <Hatch mode={r.mode} size={16} /> {modeOf(r.mode)?.name.de ?? r.mode}
                    </span>
                  </td>
                  <td className="mono">{r.off_record ? "vertraulich" : r.status}</td>
                  <td>
                    {r.participants.length
                      ? r.participants.map((p) => (
                          <span key={p.id} className="admin__dev mono">
                            {p.role.toUpperCase()} {p.lang} · {pathLabel(p.path, "de")}
                            {p.rtt_ms != null ? ` ${p.rtt_ms} ms` : ""}
                          </span>
                        ))
                      : "—"}
                  </td>
                  <td className="mono">{r.agent_connected ? "verbunden" : "—"}</td>
                  <td>
                    <span className="admin__links">
                      <Link to={`/t/${r.room}`}>Tisch</Link>
                      <button type="button" className="admin__link" onClick={() => setCreated(r.room)}>
                        QR
                      </button>
                    </span>
                  </td>
                </tr>
              ))}
              {st && !st.rooms.length ? (
                <tr>
                  <td colSpan={6} className="admin__empty">
                    Noch keine Räume. Oben einen Raum öffnen.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </section>

        <section className="admin__sec">
          <h2 className="admin__h">Modelle</h2>
          <dl className="models models--admin">
            {Object.entries(st?.models ?? {}).map(([k, v]) => (
              <div key={k}>
                <dt>{k}</dt>
                <dd className="mono">{v}</dd>
              </div>
            ))}
          </dl>
          {st?.audit ? <p className="admin__audit mono">audit head {st.audit.head_hash.slice(0, 24)}…</p> : null}
        </section>
      </main>
    </div>
  );
}

export function RoomCodes({ room, base, mode }: { room: string; base: string; mode?: ModeConfig }) {
  return (
    <div className="codes">
      {(["a", "b"] as const).map((r) => (
        <figure key={r} className="codes__item">
          <QR value={`${base}/j/${room}?role=${r}`} size={160} label={r} />
          <figcaption>
            <span className="codes__role">{mode ? pick(mode.roles[r].label, "de") : r}</span>
            <a className="mono codes__url" href={`/j/${room}?role=${r}`} target="_blank" rel="noreferrer">
              /j/{room}?role={r}
            </a>
          </figcaption>
        </figure>
      ))}
      <figure className="codes__item">
        <QR value={`${base}/t/${room}`} size={160} label="table" />
        <figcaption>
          <span className="codes__role">Tisch</span>
          <a className="mono codes__url" href={`/t/${room}`} target="_blank" rel="noreferrer">
            /t/{room}
          </a>
        </figcaption>
      </figure>
    </div>
  );
}
