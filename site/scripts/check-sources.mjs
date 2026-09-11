/**
 * Content-law guard (MASTER_BUILD_PROMPT §5.1):
 * every number on the site must be traceable to a repo file.
 *
 * The real failure mode is NOT "a content record forgot its source field" —
 * it is "a number got hardcoded into a component, where no citation exists and
 * no reviewer can trace it." So this script guards two things:
 *
 *   1. The content layer cites real repo files, and its numbers are the only
 *      sanctioned source of figures.
 *   2. No component/route under src/ (outside src/lib/content) hardcodes a
 *      product claim — a metric-looking number in JSX is a defect.
 *
 * Deliberate exceptions (layout, not claims) are listed in ALLOW below.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

const CONTENT_DIR = path.join("src", "lib", "content");
const SCAN_DIRS = [path.join("src", "components"), path.join("src", "app")];

// Citations must name a real repo artefact.
const CITATION = /\.(?:md|toml|py)\b/;

// A claim-shaped number in JSX: e.g. "2.59×", "45 concurrent", "$0.0528",
// "3,600 tasks", "24/24". Percentages/sizes in CSS are excluded by requiring
// the number to sit in rendered text, not in a style/class attribute.
const CLAIMISH = /(?:\$[\d,]+(?:\.\d+)?|\b\d[\d,]*(?:\.\d+)?\s?(?:×|x\b)|(?:\b\d[\d,]*\/\d+)|(?:\b\d[\d,]{2,}\b))/;

// Numbers that are layout, timing, or index — not product claims.
const ALLOW = [
  /\b(?:width|height|top|left|right|bottom|maxWidth|minWidth|maxHeight|minHeight)\s*[:=]/,
  /\b\d{1,3}(?:px|rem|em|vh|vw|ms|s|ch|%)\b/,
  /\bduration[-\s]?\d|\bdelay\b|\bstagger\b/i,
  /^\s*(?:\/\/|\*|\/\*)/,          // comments
  /z-\[\d+\]|z-\d+/,
  /\b(?:sm|md|lg|xl|xs)\b/,
  /\d+\s*[,)]\s*$/,                 // array indices, tuple positions
  /\bv?\d+\.\d+\.\d+\b/,            // version strings
  /#[0-9a-fA-F]{3,8}\b/,            // hex colour literals, not claims
  /rgba?\([^)]*\)/,                 // rgb()/rgba() colours
  /\b(?:key|index|i|n)\b\s*[=:]/,

  // --- shader + animation internals -------------------------------------
  // GLSL is dense with numeric literals that are MATH, not claims: noise
  // seeds, colour vec3s, smoothstep thresholds, octave counts. Flagging them
  // produced 13 false positives against 1 real catch, and a guard that cries
  // wolf trains you to ignore it - the worst outcome for a correctness check.
  /\bvec[234]\s*\(/,
  /\bfract\(|\bsmoothstep\(|\bmix\(|\bclamp\(|\bmat2\(|\bfbm\(/,
  /gl_FragColor|uniforms\.|\.value\s*=/,
  /@keyframes|\bsetTimeout\(|\bsetInterval\(|_MS\b|_DELAY\b|_TIME\b/,
  /encodeURIComponent|%23|baseFrequency|numOctaves/,

  // Step counters in illustrative UI copy ("step 1", "3 steps") describe the
  // SHAPE of a run, not a measured result. Real measured figures - costs,
  // token counts, success rates - never take this form, and those still fail.
  /\bstep \d\b|\b\d+ steps\b/i,
];

const problems = [];
let contentNumbers = 0;

/* ---- 1. content layer must cite real repo files ---- */
for (const file of readdirSync(CONTENT_DIR).filter((f) => f.endsWith(".ts"))) {
  const text = readFileSync(path.join(CONTENT_DIR, file), "utf8");
  if (!CITATION.test(text)) {
    problems.push(`${CONTENT_DIR}/${file}: cites no repo file anywhere`);
  }
  contentNumbers += (text.match(/:\s*-?\d[\d_]*/g) ?? []).length;
}

/* ---- 2. no component may hardcode a claim ---- */
function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (/\.(tsx?|css)$/.test(entry)) out.push(full);
  }
  return out;
}

for (const dir of SCAN_DIRS) {
  let files;
  try { files = walk(dir); } catch { continue; }
  for (const file of files) {
    if (file.endsWith(".css")) continue;
    const lines = readFileSync(file, "utf8").split("\n");
    lines.forEach((line, i) => {
      if (!CLAIMISH.test(line)) return;
      if (ALLOW.some((re) => re.test(line))) return;
      problems.push(`${file}:${i + 1}: claim-shaped number in a component -> ${line.trim().slice(0, 80)}`);
    });
  }
}

if (problems.length) {
  console.error(`FAIL: ${problems.length} untraceable number(s)\n`);
  for (const p of problems) console.error("  " + p);
  console.error("\nFigures belong in src/lib/content/, each with its source.");
  process.exit(1);
}
console.log(`PASS: content layer cites repo files (${contentNumbers} entries); no component hardcodes a claim`);
