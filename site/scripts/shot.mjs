/**
 * Screenshots the site at the four spec widths and asserts, at each:
 *   - zero console errors / page errors
 *   - zero horizontal body scroll
 * Writes .shots/<width>.png. Exits non-zero on any failure.
 */
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const WIDTHS = [1440, 1024, 768, 390];
const URL = process.env.URL ?? "http://localhost:3000";
const REDUCED = process.env.REDUCED === "1";

mkdirSync(".shots", { recursive: true });

const browser = await chromium.launch();
let failed = false;

for (const width of WIDTHS) {
  const page = await browser.newPage({
    viewport: { width, height: 900 },
    deviceScaleFactor: 2,
    reducedMotion: REDUCED ? "reduce" : "no-preference",
  });

  const errors = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(String(e)));

  await page.goto(URL, { waitUntil: "networkidle" });
  await page.waitForTimeout(1400); // let reveals settle

  const suffix = REDUCED ? "-reduced" : "";
  await page.screenshot({
    path: `.shots/${width}${suffix}.png`,
    fullPage: true,
  });

  const { scrollW, clientW } = await page.evaluate(() => ({
    scrollW: document.documentElement.scrollWidth,
    clientW: document.documentElement.clientWidth,
  }));

  const problems = [];
  if (scrollW > clientW + 1) {
    problems.push(`horizontal scroll (${scrollW}px > ${clientW}px)`);
  }
  if (errors.length) {
    problems.push(`${errors.length} console error(s)`);
  }

  if (problems.length) {
    console.error(`FAIL ${width}px: ${problems.join("; ")}`);
    errors.forEach((e) => console.error(`      ${e}`));
    failed = true;
  } else {
    console.log(`PASS ${width}px`);
  }

  await page.close();
}

await browser.close();
process.exit(failed ? 1 : 0);
