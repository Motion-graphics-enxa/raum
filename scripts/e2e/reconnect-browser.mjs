// C-12 in a real browser: the patient's phone (Chrome, ar) loses the network for GAP seconds while
// the doctor's phone (Chrome, de, fake mic from a WAV) keeps talking. Needs the dev server
// (`npm run dev`: the `window.__raumLive` test hook exists only in dev builds) and `npm run audio`.
//
//   node reconnect-browser.mjs [base=http://localhost:5180] [gap=10] [scenario=leave|full]
//
//   leave  the box drops the phone (LiveKit 'server-leave') and the page is offline for the gap:
//          LiveKit gives up, live.ts rejoins with the same token once the network is back
//   full   the page is offline for the gap and LiveKit's own full reconnect runs
//
// Pass: every translated turn of the session is on the patient's screen (store), the RoomStore
// is the same object as before the drop (turns not reset), the mic is published again.
import puppeteer from "puppeteer-core";
import fs from "node:fs";
import path from "node:path";

const HERE = path.dirname(new URL(import.meta.url).pathname);
const AUDIO = path.resolve(HERE, "../../recordings/e2e");
const REPO = path.resolve(HERE, "../..");
const base = process.argv[2] ?? "http://localhost:5180";
const gap = Number(process.argv[3] ?? 10);
const scenario = process.argv[4] ?? "leave";
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const out = path.join(REPO, "eval/out/e2e");
fs.mkdirSync(out, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const t0 = Date.now();
const log = (...a) => console.log(`+${((Date.now() - t0) / 1000).toFixed(1)}s`, ...a);

const room = (await (await fetch(`${base}/api/rooms`, {
  method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ mode: "sprechzimmer" }),
})).json()).room;
log("room", room);

async function phone(role, lang, wav) {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: "new",
    args: [
      "--disable-features=AudioServiceSandbox", "--no-sandbox", "--use-fake-ui-for-media-stream",
      "--use-fake-device-for-media-stream", `--use-file-for-fake-audio-capture=${path.join(AUDIO, wav)}`,
      "--autoplay-policy=no-user-gesture-required", "--no-first-run",
    ],
    userDataDir: path.join(out, `c12-profile-${role}`),
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  page.on("console", (m) => {
    const t = m.text();
    if (/\[raum\]/.test(t)) log(`[${role}] ${t.slice(0, 160)}`);
  });
  await page.goto(`${base}/j/${room}?role=${role}&lang=${lang}`, { waitUntil: "networkidle2" });
  await page.waitForSelector("button.btn--lg:not([disabled])", { timeout: 15000 });
  await page.click("button.btn--lg");
  await page.waitForSelector(".tblock__status.is-live", { timeout: 20000 });
  return { browser, page };
}

const B = await phone("b", "ar", "silence.wav");
const A = await phone("a", "de", "de1_loop.wav");
log("both joined");
await sleep(14000);

const cdp = await B.page.createCDPSession();
const before = await B.page.evaluate(() => {
  const h = window.__raumLive;
  window.__storeBefore = h.store;
  return { turns: h.store.get().turns.length, state: h.room.state };
});
log("drop", scenario, before);
const tDrop = Date.now();
// Offline emulation stops the page's WebSockets and HTTP but not WebRTC, so the drop itself comes
// from LiveKit: the server removes the phone, or the client tears its connection down; the
// offline page then cannot come back before the gap is over.
if (scenario === "leave") {
  await B.page.evaluate(() => window.__raumLive.room.simulateScenario("server-leave"));
  await B.page.waitForFunction(() => window.__raumLive.room.state === "disconnected", { timeout: 10000, polling: 10 });
}
await cdp.send("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
if (scenario === "full") void B.page.evaluate(() => window.__raumLive.room.simulateScenario("full-reconnect").catch(() => undefined));
log("offline");
const states = [];
for (let i = 0; i < gap * 2; i++) {
  states.push(await B.page.evaluate(() => window.__raumLive.room.state));
  await sleep(500);
}
await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
log("back online; link states during the gap:", [...new Set(states)].join(" -> "));
const tBack = Date.now();
await B.page.waitForFunction(() => window.__raumLive.room.state === "connected", { timeout: 60000 });
log(`connected again after ${((Date.now() - tBack) / 1000).toFixed(1)} s`);
await sleep(14000);
await A.browser.close();
await sleep(4000);

const truth = await (await fetch(`${base}/api/rooms/${room}/events`)).json();
const arFinals = truth.filter((e) => e.type === "translation.final" && e.data.target_lang === "ar");
const finals = [...new Set(arFinals.map((e) => e.utt))];
const inGap = arFinals.filter((e) => e.ts >= tDrop && e.ts <= tBack).map((e) => e.utt);
const after = await B.page.evaluate(() => {
  const h = window.__raumLive;
  const m = h.store.get();
  const pub = h.room.localParticipant.getTrackPublication("microphone");
  return {
    sameStore: h.store === window.__storeBefore,
    turns: m.turns.length,
    ar: m.turns.filter((t) => t.translations.ar?.final).map((t) => t.utt),
    mic: pub?.track?.mediaStreamTrack?.readyState ?? "none",
    micMuted: pub?.isMuted ?? null,
  };
});
await B.page.screenshot({ path: path.join(out, `c12-${scenario}-b.png`) });
await B.browser.close();
const missing = finals.filter((u) => !after.ar.includes(u));
log("session ar turns", finals.length, finals.join(","), "| finished while the phone was away:", inGap.join(",") || "none");
log("on the phone   ", after.ar.length, after.ar.join(","));
log("missing", missing.length, "same RoomStore", after.sameStore, "turns before/after", before.turns, after.turns,
  "mic", after.mic, after.micMuted ? "(muted)" : "");
const ok = missing.length === 0 && after.sameStore && after.mic === "live" && !after.micMuted && after.turns >= before.turns;
log(ok ? "PASS" : "FAIL");
process.exit(ok ? 0 : 1);
