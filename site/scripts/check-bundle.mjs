/**
 * HARD GATE: landing-route JS must stay under 180 kB gzip (DESIGN.md §10).
 * Run after `next build`. Exits non-zero when over budget.
 */
import { readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";
import path from "node:path";

const LIMIT_KB = Number(process.env.LIMIT_KB ?? 180);
const manifest = JSON.parse(
  readFileSync(".next/app-build-manifest.json", "utf8")
);

// The landing route's own chunks plus the shared layout chunks it loads.
const files = new Set();
for (const key of ["/layout", "/page"]) {
  for (const f of manifest.pages[key] ?? []) files.add(f);
}

let total = 0;
const rows = [];
for (const f of files) {
  if (!f.endsWith(".js")) continue;
  const bytes = gzipSync(readFileSync(path.join(".next", f))).length;
  total += bytes;
  rows.push([f, bytes]);
}

rows.sort((a, b) => b[1] - a[1]);
for (const [f, b] of rows.slice(0, 12)) {
  console.log(`  ${(b / 1024).toFixed(1).padStart(7)} kB  ${f}`);
}

const kb = total / 1024;
console.log(
  `\nlanding JS total: ${kb.toFixed(1)} kB gzip  (limit ${LIMIT_KB} kB)`
);

if (kb > LIMIT_KB) {
  console.error(`FAIL: over budget by ${(kb - LIMIT_KB).toFixed(1)} kB`);
  process.exit(1);
}
console.log(`PASS: ${(LIMIT_KB - kb).toFixed(1)} kB of headroom`);
