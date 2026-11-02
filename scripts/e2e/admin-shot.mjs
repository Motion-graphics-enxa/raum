// Screenshot of the admin console (F-05) on a running stack, light and dark, desktop and phone width.
// Usage: node admin-shot.mjs [base=http://localhost:5180]   -> eval/out/e2e/admin-*.png
import puppeteer from "puppeteer-core";
import fs from "node:fs";
import path from "node:path";

const HERE = path.dirname(new URL(import.meta.url).pathname);
const out = path.resolve(HERE, "../../eval/out/e2e");
fs.mkdirSync(out, { recursive: true });
const base = process.argv[2] ?? "http://localhost:5180";
const browser = await puppeteer.launch({
  executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: "new",
  args: ["--no-first-run"],
});
for (const [name, w, h, scheme] of [["desktop", 1280, 900, "light"], ["desktop-dark", 1280, 900, "dark"], ["phone", 390, 844, "light"]]) {
  const page = await browser.newPage();
  await page.setViewport({ width: w, height: h, deviceScaleFactor: 2 });
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
  await page.goto(`${base}/admin`, { waitUntil: "networkidle2" });
  await new Promise((r) => setTimeout(r, 2500));
  const verify = await page.$(".admin__verify button");
  if (verify) {
    await verify.click();
    await new Promise((r) => setTimeout(r, 600));
  }
  const file = path.join(out, `admin-${name}.png`);
  await page.screenshot({ path: file, fullPage: true });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  console.log(file, overflow > 0 ? `horizontal overflow ${overflow}px` : "no horizontal overflow");
  await page.close();
}
await browser.close();
