// E2E: two real Chrome instances with fake microphones fed from WAV files join one room.
// A (doctor, de) speaks a German sentence on a loop; B (patient, ar) is silent and must see
// partial captions, the streamed translation, fact-guard dimensions and the verify mark.
// Usage: node two-phones.mjs [base=http://localhost:5180] [seconds=30] [mode=sprechzimmer] [speaker=a]
import puppeteer from "puppeteer-core";
import fs from "node:fs";
import path from "node:path";

const HERE = path.dirname(new URL(import.meta.url).pathname);
const AUDIO = path.resolve(HERE, "../../recordings/e2e");
const REPO = path.resolve(HERE, "../..");
const base = process.argv[2] ?? "http://localhost:5180";
const secs = Number(process.argv[3] ?? 30);
const mode = process.argv[4] ?? "sprechzimmer";
const speaker = process.argv[5] ?? "a";
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const out = path.join(REPO, "eval/out/e2e");
fs.mkdirSync(out, { recursive: true });

const room = (await (await fetch(`${base}/api/rooms`, {
  method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ mode }),
})).json()).room;
console.log("room", room);

async function phone(role, lang, wav, w = 390, h = 844) {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: "new",
    args: [
      "--disable-features=AudioServiceSandbox",
      "--no-sandbox",
      "--use-fake-ui-for-media-stream",
      "--use-fake-device-for-media-stream",
      `--use-file-for-fake-audio-capture=${path.join(AUDIO, wav)}`,
      "--autoplay-policy=no-user-gesture-required",
      `--window-size=${w},${h}`,
      "--no-first-run",
    ],
    userDataDir: path.join(out, `profile-${role}`),
  });
  const page = await browser.newPage();
  await page.setViewport({ width: w, height: h, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  // the fake mic plays a WAV and never holds the talk button: hands-free (push-to-talk is the app's default)
  await page.evaluateOnNewDocument(() => { try { localStorage.setItem("raum:hands_free", "1"); } catch {} });
  page.on("console", (m) => {
    const t = m.text();
    if (/\[raum\]|error/i.test(t)) console.log(`[${role}] ${t.slice(0, 200)}`);
  });
  await page.goto(`${base}/j/${room}?role=${role}&lang=${lang}`, { waitUntil: "networkidle2" });
  await page.waitForSelector("button.btn--lg:not([disabled])", { timeout: 15000 });
  await page.click("button.btn--lg");
  await page.waitForSelector(".tblock__status.is-live", { timeout: 20000 });
  return { browser, page };
}

const t0 = Date.now();
const A = await phone("a", "de", speaker === "a" ? "de1_loop.wav" : "silence.wav");
const B = await phone("b", "ar", speaker === "b" ? "ar1_loop.wav" : "silence.wav");
console.log(`both joined in ${((Date.now() - t0) / 1000).toFixed(1)} s`);

const tableBrowser = await puppeteer.launch({ executablePath: CHROME, headless: "new", args: ["--no-first-run"] });
const table = await tableBrowser.newPage();
await table.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
await table.goto(`${base}/t/${room}`, { waitUntil: "networkidle2" });

let firstPartial = null, firstTranslation = null, firstMark = null;
const listener = speaker === "a" ? B.page : A.page;
for (let i = 0; i < secs * 4; i++) {
  await new Promise((r) => setTimeout(r, 250));
  const s = await listener.evaluate(() => ({
    turns: document.querySelectorAll(".turn--theirs").length,
    live: document.querySelectorAll(".turn.is-live").length,
    translated: [...document.querySelectorAll(".turn--theirs .turn__main:not(.is-source)")].filter((e) => e.textContent.trim()).length,
    marks: [...document.querySelectorAll(".turn--theirs .vmark")].map((e) => e.className.replace("vmark ", "")),
    dims: document.querySelectorAll(".turn--theirs .dim, .turn--theirs .cloud").length,
    confirm: !!document.querySelector(".confirm"),
  }));
  const el = (Date.now() - t0) / 1000;
  if (s.live && firstPartial == null) { firstPartial = el; console.log(`t+${el.toFixed(1)}s partial caption on listener`); }
  if (s.translated && firstTranslation == null) { firstTranslation = el; console.log(`t+${el.toFixed(1)}s translation on listener`); }
  if (s.marks.some((m) => /green|amber|red/.test(m)) && firstMark == null) {
    firstMark = el;
    console.log(`t+${el.toFixed(1)}s verify mark ${s.marks.join(",")} dims=${s.dims} confirm=${s.confirm}`);
  }
  if (firstMark != null && el > firstMark + 3) break;
}

await listener.screenshot({ path: path.join(out, `listener-${speaker === "a" ? "b" : "a"}.png`) });
await (speaker === "a" ? A.page : B.page).screenshot({ path: path.join(out, `speaker-${speaker}.png`) });
await table.screenshot({ path: path.join(out, "table.png") });
const text = await listener.evaluate(() => document.querySelector(".turns")?.innerText ?? document.body.innerText);
console.log("--- listener text ---\n" + text.slice(0, 1200));

if (process.env.KEEP) {
  console.log("KEEP set: leaving browsers open for", room);
} else {
  await A.browser.close();
  await B.browser.close();
  await tableBrowser.close();
}
console.log(JSON.stringify({ room, firstPartial, firstTranslation, firstMark }));
