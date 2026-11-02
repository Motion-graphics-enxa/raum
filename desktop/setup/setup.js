// Box address page. Two commands, both in src-tauri/src/lib.rs:
//   setup_state() -> { address, pending }
//   connect({ address }) -> checks <address>/api/health, saves it, opens the box (this page goes away)
"use strict";

const TEXT = {
  en: {
    slogan: "Translates. Checks. Forgets.",
    tagline: "What's said in the room stays in the room.",
    label: "Box address",
    hint: "As on the box's room page or QR code. Only the start, without /j/…",
    connect: "Connect",
    checking: "Checking {address} …",
    opening: "Opening RAUM …",
    pending: "Room {room} opens once the box is connected.",
    address: "That is not a box address. Example: https://192.168.1.50:5180",
    insecure: "Use https://. The microphone only works over a secure connection.",
    tls: "This computer does not trust the box's certificate yet. Install the box's root certificate (raum-root.crt, see the desktop README), then connect again.",
    unreachable: "No answer from the box. Is it switched on, and is this computer on the same network (or the tunnel)?",
    not_raum: "Something answered at this address, but it is not a RAUM box.",
    internal: "Something went wrong in the app.",
  },
  de: {
    slogan: "Übersetzt. Prüft. Vergisst.",
    tagline: "Was im Raum gesagt wird, bleibt im Raum.",
    label: "Adresse der Box",
    hint: "Wie auf der Raumseite oder im QR-Code der Box. Nur der Anfang, ohne /j/…",
    connect: "Verbinden",
    checking: "Prüfe {address} …",
    opening: "RAUM wird geöffnet …",
    pending: "Raum {room} öffnet sich, sobald die Box verbunden ist.",
    address: "Das ist keine Adresse einer Box. Beispiel: https://192.168.1.50:5180",
    insecure: "Bitte https:// verwenden. Das Mikrofon funktioniert nur über eine sichere Verbindung.",
    tls: "Dieser Computer vertraut dem Zertifikat der Box noch nicht. Installieren Sie das Stammzertifikat der Box (raum-root.crt, siehe Desktop-README) und verbinden Sie dann erneut.",
    unreachable: "Keine Antwort von der Box. Ist sie eingeschaltet und ist dieser Computer im selben Netz (oder im Tunnel)?",
    not_raum: "Unter dieser Adresse antwortet etwas, aber es ist keine RAUM-Box.",
    internal: "In der App ist ein Fehler aufgetreten.",
  },
};

const lang = (navigator.language || "en").toLowerCase().startsWith("de") ? "de" : "en";
const t = (key, vars = {}) => TEXT[lang][key].replace(/\{(\w+)\}/g, (_, name) => vars[name] ?? "");

const $ = (id) => document.getElementById(id);
const form = $("form");
const input = $("address");
const button = $("go");
const status = $("status");

function show(text, detail = "", bad = false) {
  status.hidden = false;
  status.classList.toggle("status--bad", bad);
  $("status-text").textContent = text;
  $("status-detail").textContent = detail;
}

function busy(on) {
  button.disabled = on;
  input.readOnly = on;
}

async function connect(address) {
  busy(true);
  show(t("checking", { address }));
  try {
    await window.__TAURI__.core.invoke("connect", { address });
    show(t("opening"));
  } catch (err) {
    busy(false);
    const code = err && typeof err === "object" && err.code in TEXT.en ? err.code : "internal";
    const detail = err && typeof err === "object" ? err.detail || "" : String(err);
    show(t(code), detail, true);
    input.focus();
    input.select();
  }
}

async function main() {
  document.documentElement.lang = lang;
  for (const el of document.querySelectorAll("[data-t]")) el.textContent = t(el.dataset.t);

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!button.disabled) void connect(input.value);
  });

  const state = await window.__TAURI__.core.invoke("setup_state");
  if (state.address) input.value = state.address;
  const room = (state.pending || "").split("?")[0].split("/").pop();
  if (room) {
    $("pending").textContent = t("pending", { room });
    $("pending").hidden = false;
  }
  // A saved box reconnects on its own, unless the person came here to change it (menu: Box address…).
  const change = new URLSearchParams(location.search).has("change");
  if (state.address && !change) void connect(state.address);
  else input.focus();
}

void main();
