/**
 * Admin `/admin` (F-05). One drawing sheet per room: devices as connection plates (path line type,
 * RTT, how they play the voice), the agent, and the room's median turn drawn to scale with p50/p95
 * against the 1.0 s budget; burn one room, QR codes. Box-wide: privacy meter, seal, burn-all,
 * models and GPU, and the audit head with chain verification.
 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, listOf, type AdminLatency, type AuditVerify, type ModeConfig, type RoomLatency } from "../lib/api";
import type { ParticipantInfo } from "../lib/events";
import { pick } from "../lib/i18n";
import { Hatch, fmtMs } from "../components/Drawing";
import { Check, Cross, Key, Shield } from "../components/Icons";
import { GlossaryUpload } from "../components/GlossaryUpload";
import { QR } from "../components/QR";
import { Timeline, pathLabel } from "../components/Sheet";
import "./screens.css";
import "./table.css";
import "./admin.css";

interface Device extends ParticipantInfo {
  muted?: boolean;
}

interface RoomRow {
  room: string;
  mode: string;
  status: string;
  session_id?: string | null;
  started_at?: number | null;
  participants: Device[];
  agent_connected: boolean;
  agent_id?: string | null;
  agent_since?: number | null;
  off_record?: boolean;
  idle_s?: number | null;
  auto_burn_minutes?: number | null;
}

interface Status {
  profile: string;
  version?: string;
  uptime_s?: number;
  rooms: RoomRow[];
  models: Record<string, string>;
  gpu: { util_pct: number; models_loaded: string[]; mem_used_gb?: number | null; source?: string | null };
  meter: { third_party_bytes: number; tunnel_bytes: number; sealed: boolean; source?: string | null };
  sealed: boolean;
  seal?: { sealed: boolean; applied: boolean | null; method: string | null; error: string | null };
  audit?: { head_hash: string };
}

const UI = "en"; // English chrome, like every screen
const DEVICE: Record<string, string> = {
  phone: "Phone",
  laptop: "Laptop",
  tablet: "Tablet",
  speakerphone: "Speakerphone",
  table: "Table",
  bot: "Bot",
};
const AUDIO_OUT: Record<string, string> = {
  headphones: "Headphones",
  speaker: "Phone speaker",
  room: "Room speaker",
};
const STATUS: Record<string, string> = { live: "live", ended: "ended", burned: "destroyed", idle: "empty" };

function fmtSpan(s: number): string {
  if (s < 60) return `${Math.max(0, Math.round(s))} s`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

const clock = (ms: number) => new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });

export default function Admin() {
  const [st, setSt] = useState<Status | null>(null);
  const [lat, setLat] = useState<AdminLatency | null>(null);
  const [modes, setModes] = useState<ModeConfig[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);
  const [codes, setCodes] = useState<string | null>(null);
  const [gloss, setGloss] = useState<string | null>(null); // N-02: the deal glossary panel of one room
  const [busy, setBusy] = useState(false);
  const [verify, setVerify] = useState<AuditVerify | null>(null);
  const [verifying, setVerifying] = useState(false);

  const load = useCallback(async () => {
    try {
      setSt((await api.adminStatus()) as unknown as Status);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);
  const loadLatency = useCallback(async () => {
    try {
      setLat(await api.adminLatency());
    } catch {
      /* status shows the outage */
    }
  }, []);

  useEffect(() => {
    void load();
    void loadLatency();
    void api.modes().then((m) => setModes(listOf<ModeConfig>(m, "modes"))).catch(() => undefined);
    const iv = window.setInterval(load, 1000);
    const iv2 = window.setInterval(loadLatency, 2000);
    return () => {
      window.clearInterval(iv);
      window.clearInterval(iv2);
    };
  }, [load, loadLatency]);

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

  const burn = async (room: string) => {
    if (!window.confirm(`Destroy room ${room}? The key is deleted; the conversation can never be read again.`)) return;
    try {
      await api.burn(room);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
    await load();
  };

  const runVerify = async () => {
    setVerifying(true);
    try {
      setVerify(await api.auditVerify());
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setVerifying(false);
    }
  };

  const modeOf = (id: string) => modes.find((m) => m.id === id);
  const base = location.origin;
  const seal = st?.seal;

  return (
    <div className="screen screen--admin" lang="de">
      <header className="tblock">
        <div className="tblock__row">
          <span className="tblock__name">RAUM · Admin</span>
          <span className="tblock__gloss mono">
            {st ? `profile ${st.profile}` : ""}
            {st?.version ? ` · v${st.version}` : ""}
            {st?.uptime_s != null ? ` · up ${fmtSpan(st.uptime_s)}` : ""}
          </span>
        </div>
      </header>
      <main className="admin">
        {err ? (
          <p className="banner banner--bad" role="alert">
            raum-core unreachable: {err}
          </p>
        ) : null}

        <section className="admin__band" aria-label="Privacy and box">
          <div className="admin__meter">
            <span className="admin__meter-n mono">{(st?.meter.third_party_bytes ?? 0).toLocaleString("en-GB")}</span>
            <span className="label">Bytes to third parties{st?.meter.source === "stub" ? " · stub" : ""}</span>
          </div>
          <div className="admin__meter">
            <span className="admin__meter-n admin__meter-n--s mono">{((st?.meter.tunnel_bytes ?? 0) / 1024).toFixed(1)} KB</span>
            <span className="label">Tunnel to your own devices</span>
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
              <Shield /> {st?.sealed ? "Sealed" : "Seal the box"}
            </button>
            <button
              type="button"
              className="btn btn--bad"
              onClick={() => {
                if (window.confirm("Destroy all sessions?")) void api.burnAll().then(load);
              }}
            >
              <Key /> Destroy all
            </button>
            {st?.sealed ? (
              <p className="admin__sealnote mono" role="status">
                {seal?.method === "nft"
                  ? seal.applied
                    ? "outgoing traffic blocked (nft)"
                    : `nft rule not set: ${seal.error ?? "unknown"}`
                  : "status only: no nft on this machine"}
              </p>
            ) : null}
          </div>
        </section>

        <section className="admin__sec">
          <h2 className="admin__h">New room</h2>
          <div className="admin__new">
            {modes.map((m) => (
              <button key={m.id} type="button" className="admin__mode" onClick={() => void create(m.id)} disabled={busy}>
                <Hatch mode={m.id} size={28} />
                <span className="admin__mode-name">{m.name.de}</span>
                <span className="admin__mode-tag">{pick(m.tagline ?? {}, UI)}</span>
              </button>
            ))}
          </div>
          {created ? (
            <RoomCodes room={created} base={base} mode={modes.find((m) => created.startsWith(`${m.prefix}-`))} />
          ) : null}
        </section>

        <section className="admin__sec">
          <h2 className="admin__h">
            Rooms <span className="admin__count mono">{st ? st.rooms.length : ""}</span>
          </h2>
          {(st?.rooms ?? []).map((r) => (
            <RoomSheet
              key={r.room}
              r={r}
              mode={modeOf(r.mode)}
              lat={lat?.rooms[r.room]}
              codesOpen={codes === r.room}
              onCodes={() => setCodes(codes === r.room ? null : r.room)}
              glossOpen={gloss === r.room}
              onGloss={() => setGloss(gloss === r.room ? null : r.room)}
              onBurn={() => void burn(r.room)}
              base={base}
            />
          ))}
          {st && !st.rooms.length ? <p className="admin__empty">No rooms yet. Open one above.</p> : null}
        </section>

        <div className="admin__split">
          <section className="admin__sec">
            <h2 className="admin__h">Models</h2>
            <dl className="models models--admin">
              {Object.entries(st?.models ?? {}).map(([k, v]) => (
                <div key={k}>
                  <dt>{k}</dt>
                  <dd className="mono">{v}</dd>
                </div>
              ))}
            </dl>
          </section>
          <section className="admin__sec">
            <h2 className="admin__h">GPU</h2>
            <dl className="models models--admin">
              <div>
                <dt>Last</dt>
                <dd className="mono">{st ? `${Math.round(st.gpu.util_pct)} %` : "—"}</dd>
              </div>
              <div>
                <dt>Memory</dt>
                <dd className="mono">{st?.gpu.mem_used_gb != null ? `${st.gpu.mem_used_gb.toFixed(1)} GB` : "—"}</dd>
              </div>
              <div>
                <dt>Loaded</dt>
                <dd className="mono">{st?.gpu.models_loaded?.length ? st.gpu.models_loaded.join(" · ") : "—"}</dd>
              </div>
              <div>
                <dt>Source</dt>
                <dd className="mono">{st?.gpu.source ?? "—"}</dd>
              </div>
            </dl>
          </section>
        </div>

        <section className="admin__sec">
          <h2 className="admin__h">Audit log</h2>
          <div className="admin__audit">
            <dl className="models models--admin">
              <div>
                <dt>Head</dt>
                <dd className="mono admin__hash">{st?.audit?.head_hash ?? "—"}</dd>
              </div>
            </dl>
            <div className="admin__verify">
              <button type="button" className="btn btn--ghost" onClick={() => void runVerify()} disabled={verifying}>
                {verifying ? "Checking…" : "Verify chain"}
              </button>
              {verify ? <Verdict v={verify} /> : <span className="admin__hint">SHA-256 chain, no content</span>}
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}

function Verdict({ v }: { v: AuditVerify }) {
  const when = new Date(v.checked_at).toLocaleTimeString("en-GB");
  if (v.ok === null) return <p className="admin__verdict mono">No check possible · {when}</p>;
  return v.ok ? (
    <p className="admin__verdict is-ok mono" role="status">
      <Check /> Chain intact · {v.lines} lines · {when} ({fmtMs(v.ms)})
    </p>
  ) : (
    <p className="admin__verdict is-bad mono" role="alert">
      <Cross /> Chain broken at line {v.first_bad}: {v.reason} · {when}
    </p>
  );
}

function RoomSheet({
  r,
  mode,
  lat,
  codesOpen,
  onCodes,
  glossOpen,
  onGloss,
  onBurn,
  base,
}: {
  r: RoomRow;
  mode?: ModeConfig;
  lat?: RoomLatency;
  codesOpen: boolean;
  onCodes: () => void;
  glossOpen: boolean;
  onGloss: () => void;
  onBurn: () => void;
  base: string;
}) {
  const live = r.status === "live" && !r.off_record;
  const burnIn =
    r.status !== "burned" && r.auto_burn_minutes && r.idle_s != null ? r.auto_burn_minutes * 60 - r.idle_s : null;
  const roleLabel = (p: Device) =>
    mode && (p.role === "a" || p.role === "b") ? pick(mode.roles[p.role].label, UI) : p.role === "observer" ? "Observer" : p.role;
  return (
    <article className={`aroom ${r.status === "burned" ? "is-burned" : ""}`}>
      <header className="aroom__head">
        <span className="aroom__mode">
          <Hatch mode={r.mode} size={20} />
          <span className="aroom__id mono">{r.room}</span>
          <span className="aroom__name">{mode?.name.de ?? r.mode}</span>
        </span>
        <span className={`aroom__state mono ${live ? "is-live" : ""}`}>
          <span className="aroom__dot" aria-hidden="true" />
          {r.off_record ? "vertraulich" : (STATUS[r.status] ?? r.status)}
        </span>
        <span className="aroom__meta mono">
          {r.started_at ? `since ${clock(r.started_at)}` : ""}
          {burnIn != null ? ` · auto-destroy in ${fmtSpan(burnIn)}` : ""}
        </span>
        <span className="admin__links aroom__actions">
          <Link to={`/t/${r.room}`}>Table</Link>
          <button type="button" className="admin__link" onClick={onCodes} aria-expanded={codesOpen}>
            QR
          </button>
          {r.session_id && r.status !== "burned" ? (
            <button type="button" className="admin__link" onClick={onGloss} aria-expanded={glossOpen}>
              Glossary
            </button>
          ) : null}
          <button
            type="button"
            className="admin__link admin__link--bad"
            onClick={onBurn}
            disabled={r.status === "burned" || !r.session_id}
          >
            Destroy
          </button>
        </span>
      </header>
      <div className="aroom__cells">
        <section className="aroom__cell">
          <h3 className="aroom__h">Devices</h3>
          {r.participants.length ? (
            <ul className="plates">
              {r.participants.map((p) => (
                <li key={p.id} className={`plate plate--${p.path} aplate`}>
                  <span className="plate__who">{roleLabel(p)}</span>
                  <span className="plate__lang mono">{p.lang.toUpperCase()}</span>
                  <span className="plate__dev mono">{DEVICE[p.device] ?? p.device}</span>
                  <span className="plate__path mono">
                    {pathLabel(p.path, UI)}
                    {p.rtt_ms != null ? ` ${p.rtt_ms} ms` : ""}
                  </span>
                  <span className="aplate__out mono">
                    {AUDIO_OUT[p.audio_out ?? "speaker"] ?? p.audio_out}
                    {p.muted ? " · mic off" : ""}
                    <span className="aplate__id"> · {p.id}</span>
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="aroom__idle">No device connected.</p>
          )}
        </section>
        <section className="aroom__cell">
          <h3 className="aroom__h">Agent</h3>
          {r.agent_connected ? (
            <p className="aroom__agent">
              <span className="aroom__state mono is-live">
                <span className="aroom__dot" aria-hidden="true" />
                verbunden
              </span>
              <span className="aroom__meta mono">
                {r.agent_since ? `since ${clock(r.agent_since)}` : ""}
                {r.agent_id ? ` · ${r.agent_id.split("/").pop()}` : ""}
              </span>
            </p>
          ) : (
            <p className="aroom__idle">
              No agent. It joins as soon as a device is in the room; the session stays on the box.
            </p>
          )}
        </section>
        <section className="aroom__cell aroom__cell--lat">
          <h3 className="aroom__h">Latency</h3>
          <Latency lat={lat} />
        </section>
      </div>
      {codesOpen ? (
        <div className="aroom__codes">
          <RoomCodes room={r.room} base={base} mode={mode} />
        </div>
      ) : null}
      {glossOpen && r.session_id && r.status !== "burned" ? (
        <div className="aroom__gloss">
          <GlossaryUpload room={r.room} lang="de" role="admin" />
        </div>
      ) : null}
    </article>
  );
}

/** The room's median turn drawn to scale, with first-word p50/p95 against the 1.0 s budget. */
function Latency({ lat }: { lat?: RoomLatency }) {
  if (!lat || !lat.n) return <p className="aroom__idle">No measurement yet.</p>;
  const median = {
    vad_ms: lat.vad_ms.p50,
    asr_ms: lat.asr_ms.p50,
    mt_first_ms: lat.mt_first_ms.p50,
    mt_full_ms: lat.mt_full_ms.p50,
    tts_first_ms: lat.tts_first_ms.p50,
    check_ms: lat.check_ms.p50,
  };
  const f = (x: number | null) => (x != null ? fmtMs(x) : "—");
  return (
    <>
      <Timeline timing={median} lang={UI} />
      <p className="aroom__lat mono">
        <span>
          First words p50 {f(lat.mt_first_ms.p50)} · p95 {f(lat.mt_first_ms.p95)}
        </span>
        <span>
          Sentence p50 {f(lat.mt_full_ms.p50)} · p95 {f(lat.mt_full_ms.p95)}
        </span>
        <span>
          n {lat.n}
          {lat.over_budget ? ` · ${lat.over_budget} over 1.0 s` : ""}
        </span>
      </p>
    </>
  );
}

export function RoomCodes({ room, base, mode }: { room: string; base: string; mode?: ModeConfig }) {
  return (
    <div className="codes">
      {(["a", "b"] as const).map((r) => (
        <figure key={r} className="codes__item">
          <QR value={`${base}/j/${room}?role=${r}`} size={160} label={r} />
          <figcaption>
            <span className="codes__role">{mode ? pick(mode.roles[r].label, UI) : r}</span>
            <a className="mono codes__url" href={`/j/${room}?role=${r}`} target="_blank" rel="noreferrer">
              /j/{room}?role={r}
            </a>
          </figcaption>
        </figure>
      ))}
      <figure className="codes__item">
        <QR value={`${base}/t/${room}`} size={160} label="table" />
        <figcaption>
          <span className="codes__role">Table</span>
          <a className="mono codes__url" href={`/t/${room}`} target="_blank" rel="noreferrer">
            /t/{room}
          </a>
        </figcaption>
      </figure>
    </div>
  );
}
