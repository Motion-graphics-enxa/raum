// Join a doctor (de) and a patient (ar) phone with silent fake mics + a table screen, run
// scripts/replay.py, screenshot every state. Usage: node replay-shots.mjs [base] [theme]
import puppeteer from "puppeteer-core";
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const HERE = path.dirname(new URL(import.meta.url).pathname);
const AUDIO = path.resolve(HERE, "../../recordings/e2e");
const REPO = path.resolve(HERE, "../..");
const base = process.argv[2] ?? "http://localhost:5180";
const scheme = process.argv[3] ?? "light";
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const out = path.join(REPO, "eval/out/e2e", scheme);
fs.mkdirSync(out, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const room = (await (await fetch(`${base}/api/rooms`, {
  method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ mode: "sprechzimmer" }),
})).json()).room;
console.log("room", room);

const browser = await puppeteer.launch({
  executablePath: CHROME, headless: "new",
  args: ["--disable-features=AudioServiceSandbox", "--no-sandbox", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
    `--use-file-for-fake-audio-capture=${path.join(AUDIO, "silence.wav")}`, "--no-first-run"],
});

async function phone(role, lang) {
  const ctx = await browser.createBrowserContext();
  const page = await ctx.newPage();
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
  await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  page.on("pageerror", (e) => console.log(`[${role}] pageerror ${e.message}`));
  await page.goto(`${base}/j/${room}?role=${role}&lang=${lang}`, { waitUntil: "networkidle2" });
  await page.waitForSelector("button.btn--lg:not([disabled])");
  await page.screenshot({ path: path.join(out, `join-${role}.png`) });
  await page.click("button.btn--lg");
  await page.waitForSelector(".tblock__status.is-live", { timeout: 20000 });
  return page;
}

const A = await phone("a", "de");
const B = await phone("b", "ru");
const tctx = await browser.createBrowserContext();
const T = await tctx.newPage();
await T.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
await T.setViewport({ width: 1440, height: 900 });
await T.goto(`${base}/t/${room}`, { waitUntil: "networkidle2" });
await sleep(1500);
await A.screenshot({ path: path.join(out, "talk-a-empty.png") });
await T.screenshot({ path: path.join(out, "table-empty.png") });

const rp = spawn("uv", ["run", "python", "../scripts/replay.py", "--room", room, "--speed", "1"], {
  cwd: path.join(REPO, "core"),
  stdio: "inherit",
});
rp.on("error", (e) => console.log("replay error", e));

// the patient's dose and allergy, green: noted on the doctor's phone, nobody asked
await A.waitForSelector(".fnote--noted", { timeout: 30000 });
await sleep(500);
await A.screenshot({ path: path.join(out, "talk-a-noted.png") });
// the checker unsure: read back to the patient (inline, never modal)
await B.waitForSelector(".rb:not(.is-done)", { timeout: 30000 });
await sleep(500);
await B.screenshot({ path: path.join(out, "talk-b-readback.png") });
await new Promise((r) => rp.on("exit", r));
await sleep(800);
await B.screenshot({ path: path.join(out, "talk-b-red.png") });
await A.screenshot({ path: path.join(out, "talk-a-end.png") });
await B.screenshot({ path: path.join(out, "talk-b-end.png") });
await T.screenshot({ path: path.join(out, "table.png") });
// open the proof on the doctor's newest "theirs" turn
const mark = await A.$$(".turn--theirs button.vmark");
if (mark.length) { await mark[mark.length - 1].click(); await sleep(300); await A.screenshot({ path: path.join(out, "talk-a-proof.png"), fullPage: false }); }

// End -> summary on both phones
await fetch(`${base}/api/rooms/${room}/end`, { method: "POST" });
await A.waitForSelector(".sheet", { timeout: 20000 });
await B.waitForSelector(".sheet", { timeout: 20000 });
await sleep(800);
await A.screenshot({ path: path.join(out, "summary-a.png"), fullPage: true });
await B.screenshot({ path: path.join(out, "summary-b.png"), fullPage: true });
// burn from the doctor's phone
const burnBtn = await A.$(".sum__actions .btn--bad");
if (burnBtn) {
  await burnBtn.click(); await sleep(200);
  const really = await A.$(".sum__burnask .btn--bad");
  if (really) await really.click();
}
await A.waitForSelector(".stamp", { timeout: 20000 });
await sleep(1200); // stamp frame draws
await A.screenshot({ path: path.join(out, "burn-a.png") });
await B.screenshot({ path: path.join(out, "burn-b.png") });
await T.screenshot({ path: path.join(out, "table-burn.png") });
const errs = [];
console.log("done", room, errs);
await browser.close();
