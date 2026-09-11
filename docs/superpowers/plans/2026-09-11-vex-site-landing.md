# Vex Website — Landing Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Execution mode note:** `MASTER_BUILD_PROMPT.md` §0.4 reports intermittent API 400/402 errors on long-running subagents in this repo and instructs preferring the main session. Inline execution is therefore recommended here, overriding subagent-driven-development's usual default.

**Goal:** Ship the Vex landing page (`/`) as an award-caliber, fully verified Next 15 route that holds a hard 180 kB gzip JS budget and contains zero unsourced claims.

**Architecture:** A fresh Next 15 App Router project in `site/`, styled entirely through Tailwind v4 CSS-first `@theme` tokens transcribed from `DESIGN.md` (no `tailwind.config.js`). All verified product numbers live in one typed content module so no figure is ever hardcoded in JSX. The hero background is a bespoke domain-warped fBm GLSL shader on a single `ogl` fullscreen triangle, dynamically imported and wrapped in a six-rung degradation ladder. Animation is tiered by cost: CSS + IntersectionObserver for reveals, `motion` only where component state animates, GSAP ScrollTrigger lazily loaded for the single scroll-linked gate moment in §5.

**Tech Stack:** next@15 (App Router) · react@19 · TypeScript (strict) · tailwindcss@4 · ogl · gsap + ScrollTrigger (lazy) · motion · lenis · lucide-react · next/font (Fraunces / Archivo / JetBrains Mono) · Playwright (verification)

**Spec:** `MASTER_BUILD_PROMPT.md` (build brief) + `DESIGN.md` (design language, authoritative). Ground truth for all figures: `README.md`, `RESULTS.md`, `CHANGELOG.md`, `INTERFACES.md`, `pyproject.toml`.

---

## Global Constraints

Every task's requirements implicitly include this section. Values copied verbatim from the spec.

**Budget & performance**
- Landing JS **< 180 kB gzip**, including the shader. Hard gate enforced by `scripts/check-bundle.mjs`; a task that breaks it is not done.
- LCP < 2.0s · CLS < 0.05 · INP < 200ms.
- Shader: 1 draw call, **DPR ≤ 1.5**, half-resolution on mobile (DPR cap 1.0), RAF cancelled when offscreen or tab hidden.

**Responsive**
- Fully responsive **380 → 1920**. **No horizontal body scroll at any width.** Wide tables/mockups scroll inside their own `overflow-x:auto` container.
- Breakpoints: `1280 / 1024 / 768 / 520 / 380`.

**Accessibility**
- Focus: `2px solid var(--color-ember)` + `2px` offset, visible on every interactive element. Never `outline:none` without a replacement.
- Touch targets ≥ `44×44px`.
- One `<h1>`; correct heading order; `<nav>`/`<main>`/`<footer>`; skip-link first in tab order.
- Keyboard-complete: tabs (arrows/Home/End), mobile nav (Esc closes, focus returns, focus trapped).
- Colour is never the sole signal — pair with icon, shape, or text.
- `prefers-reduced-motion`: every animation resolves to its final state; **the shader freezes on a composed still frame rather than disappearing.**

**Colour law (DESIGN.md §1.7 contrast ledger)**
- Copper CTAs take **`--color-ink` text, not white** (`--quench` on `--copper` ≈3.9:1, fails AA).
- `--color-copper` (≈4.6:1) is **not** for small body text. `--color-smoke` (≈2.9:1) is large/decorative only, ≥14px.
- `--color-patina*` appears **only on genuinely verified things**. Never decorative.
- Copper at rest targets **≤12% of viewport pixels**.

**Ban list (DESIGN.md §5) — each entry is a build defect**
- No violet/purple anywhere. No purple→blue gradients.
- No gradient text on headings. No glassmorphism except the nav bar, subtly. No glowing orbs or blurred blobs. No neon cyan-on-black terminal.
- **No `box-shadow` for elevation** — depth is hairlines plus the `inset 0 1px 0 var(--color-etch)` bevel only.
- No bento grid of equal feature cards. No three-column icon-above-heading row. No dot-grid/graph-paper background. No "✨ Introducing…" pill above the hero.
- No Inter for everything. **No emoji as UI icons — SVG only (Lucide).**
- No spotlight-follows-cursor. No scale-transform hover that shifts layout. No typewriter effect on the headline. No floating 3D shapes.
- **More than one scroll-linked hero moment is a defect.**
- No `three.js` / `@react-three/fiber` in the bundle.
- No fake logos, testimonials, quotes, star counts, user numbers, or adoption claims.

**Motion law (DESIGN.md §4)**
- Animate `transform` and `opacity` only. Never `width`/`height`/`top`/`left`/`filter` on scroll.
- Never `ease-in-out` on entrances. Entrances use `--ease-forge`.
- Micro 120–180ms · standard 320–420ms · deliberate 600–900ms · ambient 40–60s loop.
- Stagger between siblings **40–70ms**. Reveal distance **12–20px**, not 60px.
- Reveals fire **once** (`unobserve` after).

**Typography law (DESIGN.md §2.2)**
- Fraunces is for **statements only** — `h1`, section `h2`, pull-quotes, the big number in a stat. Never body, nav, buttons, labels, or UI. **≤2 Fraunces elements visible at any one scroll position.**
- Archivo carries everything else. JetBrains Mono for commands, code, paths, labels, table numerals, badges.
- `font-variant-numeric: tabular-nums` in every table and stat.
- Body measure **62–72ch**; lead paragraphs **50–58ch**. Never full-bleed text.

**Content law (MASTER_BUILD_PROMPT §5.1)**
- Every number must be traceable to `README.md`, `RESULTS.md`, `CHANGELOG.md`, `INTERFACES.md`, or a log under `logs/`. If it cannot be verified, cut it.
- The honesty note (§5.4) ships **verbatim, with the numbers**, as a designed component — never fine print.
- Voice: declarative, not salesy. Lowercase `vex` wordmark. Sentence case headings, no Title Case. Every number carries its `n`.

---

## Resolved Decisions

Verified against the repo or decided with the user during brainstorming. Do not re-litigate.

| # | Decision | Evidence / rationale |
|---|---|---|
| D1 | **CLI entry point is `vex`**, with `harness` as a still-working legacy alias. Both map to `cli.main:main`. | `pyproject.toml:20-23`. State it as fact and name the alias plainly. |
| D2 | **No PyPI package exists.** The only honest install is `git clone` + `pip install -e .`. `python -m cli` also works. | No publish config in `pyproject.toml`; `README.md:161` shows `pip install -e .`. **Never write `pip install vex`.** |
| D3 | **Hero eyebrow is a lowercase hairline-separated mono spec plate** — `cli-first` `verifier-gated` `open source` as three mono items divided by 1px `--color-rule` hairlines. No ALL-CAPS, no middle-dot string. | Overrides MASTER_BUILD_PROMPT §4 §2. `frontend-design` names all-caps labels, mono data labels, middle-dot meta strings, and labels-above-content as the four commonest tells of a generated page. User chose this. |
| D4 | **The h1's copper word is `real`, not `Verified`.** "Fix **real** bugs. Verified, not vibed." | `DESIGN.md §1.3` assigns copper to *active/working*; `§1.4` reserves patina for *verified/proven*. Colouring "Verified" copper contradicts the palette's central idea. Patina stays reserved for the §5 gate. |
| D5 | **180 kB is a hard gate.** Budget wins ties against the stack table. GSAP+ScrollTrigger lazy and §5-only; `cmdk` deferred entirely to the docs plan; `ogl` dynamically imported; simple reveals use IntersectionObserver + CSS, not `motion`. | User decision. React 19 + Next hydration is ~90 kB before any feature. |
| D6 | **`ogl` fullscreen triangle, not a quad.** `new Triangle(gl)` — position `-1..3`, uv `0..2`, no camera, no scene graph. | context7 `/oframe/ogl`: the documented full-screen-shader path. |
| D7 | **bottle / click / parse must NOT be cited as successes.** Honest in-flight failures, no numbers. | `README.md:112-115` — "runs in flight", "first attempts showed honest failures", "Numbers land when the endpoint stabilizes". |
| D8 | **`harness/state_machine.py` is a designed contract, not shipped runtime.** If phases are mentioned, say so explicitly. | `grep` confirms zero runtime imports — only `tests/test_state_machine.py`. |
| D9 | Citeable OSS validation: **jaraco/path** (1 attempt, 6 calls, $0.053) · the **Round-6 five** (more-itertools, arrow, inflect, boltons, python-semver → adaptive **3/5** vs always-expensive **2/5**) · **python-semver** module DoD (15/15 checks). | `README.md:93-110`, `RESULTS.md:74,84`, `CHANGELOG.md:60-66`. |
| D10 | Next **16** exists; the brief locks **next@15**. Honour the brief. | User's stack table is explicit. |
| D11 | **`@theme` must be `@theme static`.** Tailwind v4 tree-shakes theme variables: it emits a custom property only when it detects a matching *utility class* in source. Tokens used solely as `var(--color-x)` (the `etch` bevel, `--color-rule-soft/-hot`, `--color-patina-dim`, every shader/scrim colour) are silently dropped and resolve to nothing. | Found live in Task 1 Step 4: patina, warn, fail, flare and the near-black steps all rendered black. `static` emits all 33 tokens. |
| D12 | **`scripts/check-sources.mjs` guards the right failure mode:** not "a content record forgot its `source` field" but "a number got hardcoded into a component." It scans `src/components/` and `src/app/` for claim-shaped numbers with an allow-list for layout/timing/hex-colour values, and asserts the content layer cites repo files. | The first two versions passed while detecting almost nothing. The red test (hardcoding `$0.0581` into `page.tsx`) is what proved it works. |

---

## File Structure

```
site/
  package.json
  next.config.ts                 bundle-analyzer wiring
  tsconfig.json                  strict: true
  postcss.config.mjs             @tailwindcss/postcss
  README.md                      run + deploy (Task 14)
  public/
    hero-fallback.webp           pre-rendered shader still (no-WebGL rung)
    favicon.svg
    og.png
  scripts/
    check-bundle.mjs             HARD GATE: landing JS <= 180 kB gzip
    shot.mjs                     Playwright: 4 widths, console, h-scroll assert
  src/
    app/
      layout.tsx                 fonts, <html> vars, metadata, viewport, SkipLink
      page.tsx                   landing: composes src/components/sections/*
      globals.css                @import "tailwindcss" + @theme + base layer
      sitemap.ts
      robots.ts
    lib/
      site.ts                    name, url, description, nav links — one source
      cn.ts                      className joiner
      content/
        ablation.ts              the 6 verified router rows + honesty note
        stats.ts                 stat-band figures, each with its source
        repos.ts                 D9 repo results
        sandbox.ts               sandbox flag list
        mcp.ts                   the exactly-5 MCP tools
    components/
      primitives/                Button Badge Panel Card Hairline SpecPlate
                                 SectionHeader Prose Link Icon VisuallyHidden SkipLink
      motion/
        Reveal.tsx               IntersectionObserver + CSS, fires once
        StaggerGroup.tsx
        SplitText.tsx            char/word split for the h1
        CountUp.tsx
        GrainOverlay.tsx
        ShaderHero.tsx           'use client' host + fallback ladder
        shader/
          forge.glsl.ts          vertex + fragment source
          mountForge.ts          ogl lifecycle, DPR, RAF gating
      product/                   Terminal CommandLine InstallTabs DiffBlock
                                 RationaleCard RouterTable StatTile SessionBoard
                                 LoopDiagram LayerStack SandboxFlags MCPToolList
                                 RepoRow HonestyNote
      chrome/                    Nav MobileNav Footer
      sections/                  Hero StatBand Thesis HowItWorks LayerHarness
                                 LayerExecution LayerRuntime Reliability Benchmarks
                                 LayerMemory MultiRepo GetStarted HonestByDesign Closing
```

**Responsibility boundaries:** `lib/content/*` holds data only — every exported figure carries a `source` string naming the repo file it came from. `components/sections/*` compose `product/` + `primitives/` and own layout rhythm only; they never hold figures inline. `components/motion/shader/*` is the only place WebGL is touched.

---

## Task 1: Scaffold + the Forge token system

**Files:**
- Create: `site/package.json`, `site/tsconfig.json`, `site/next.config.ts`, `site/postcss.config.mjs`
- Create: `site/src/app/globals.css`, `site/src/app/layout.tsx`, `site/src/app/page.tsx`
- Create: `site/src/lib/site.ts`, `site/src/lib/cn.ts`

**Interfaces:**
- Produces: CSS custom properties for every `DESIGN.md §1` token, consumable as both Tailwind utilities (`bg-ink`, `text-copper`, `font-display`, `ease-forge`, `rounded-lg`) and raw vars (`var(--color-etch)`).
- Produces: `cn(...classes: (string | false | null | undefined)[]) => string`
- Produces: `site` object — `{ name, url, description, repo, navLinks }`

- [ ] **Step 1: Scaffold the project**

```bash
cd site && npm init -y
npm i next@15 react@19 react-dom@19
npm i -D typescript @types/react @types/node @tailwindcss/postcss tailwindcss@4
npm i ogl lucide-react
```

`site/postcss.config.mjs`:
```js
export default { plugins: { "@tailwindcss/postcss": {} } };
```

`site/tsconfig.json` must set `"strict": true`, `"jsx": "preserve"`, `"moduleResolution": "bundler"`, and `"paths": { "@/*": ["./src/*"] }`.

- [ ] **Step 2: Write `globals.css` with the complete token table**

Transcribed from `DESIGN.md §1`, `§2.3`, `§4.1`, `§6`. Tailwind v4 emits both utilities and CSS vars from `@theme`.

```css
@import "tailwindcss";

@theme {
  /* Ground & surfaces — DESIGN.md §1.1 */
  --color-ink:    #080706;
  --color-basalt: #0F0D0B;
  --color-slab:   #14110F;
  --color-char:   #201A16;
  --color-forge:  #2B221C;

  /* Hairlines — §1.2 */
  --color-rule:      #2E2621;
  --color-rule-soft: rgba(197,106,62,0.10);
  --color-rule-hot:  rgba(197,106,62,0.34);
  --color-etch:      rgba(245,240,234,0.05);

  /* Heat — §1.3 */
  --color-copper: #C56A3E;
  --color-ember:  #E8945C;
  --color-flare:  #F4B183;
  --color-scorch: #7A3F24;

  /* Patina — §1.4 */
  --color-patina:        #4FA88B;
  --color-patina-bright: #6FD4AE;
  --color-patina-dim:    rgba(79,168,139,0.14);

  /* Type — §1.5 */
  --color-quench: #F5F0EA;
  --color-ash:    #A29488;
  --color-smoke:  #6E645C;
  --color-soot:   #473F39;

  /* Signal — §1.6 */
  --color-warn: #D9A441;
  --color-fail: #D4614E;

  /* Families — §2.1 (vars injected by next/font in layout.tsx) */
  --font-display: var(--font-fraunces), "Instrument Serif", Georgia, serif;
  --font-sans:    var(--font-archivo), Inter, system-ui, sans-serif;
  --font-mono:    var(--font-jetbrains), ui-monospace, SFMono-Regular, monospace;

  /* Scale — §2.3 */
  --text-display: clamp(3.25rem, 7.5vw, 7rem);
  --text-display--line-height: 0.94;
  --text-display--letter-spacing: -0.035em;
  --text-h1: clamp(2.75rem, 5.5vw, 4.75rem);
  --text-h1--line-height: 1.0;
  --text-h1--letter-spacing: -0.03em;
  --text-h2: clamp(2rem, 3.6vw, 3.25rem);
  --text-h2--line-height: 1.06;
  --text-h2--letter-spacing: -0.025em;
  --text-h3: clamp(1.35rem, 1.9vw, 1.75rem);
  --text-h3--line-height: 1.2;
  --text-h3--letter-spacing: -0.015em;
  --text-lead: clamp(1.125rem, 1.5vw, 1.375rem);
  --text-lead--line-height: 1.55;
  --text-lead--letter-spacing: -0.008em;
  --text-body: 1rem;
  --text-body--line-height: 1.65;
  --text-body--letter-spacing: -0.005em;
  --text-small: 0.875rem;
  --text-small--line-height: 1.55;
  --text-monolg: 0.9375rem;
  --text-monolg--line-height: 1.7;
  --text-monolg--letter-spacing: -0.01em;
  --text-mono: 0.8125rem;
  --text-mono--line-height: 1.75;
  --text-eyebrow: 0.75rem;
  --text-eyebrow--line-height: 1;

  /* Easing — §4.1 */
  --ease-forge:  cubic-bezier(0.22, 1, 0.36, 1);
  --ease-strike: cubic-bezier(0.65, 0, 0.35, 1);
  --ease-draw:   cubic-bezier(0.16, 1, 0.3, 1);
  --ease-quench: cubic-bezier(0.34, 1.28, 0.64, 1);

  /* Radii — §6 */
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 14px;
  --radius-xl: 18px;

  /* Breakpoints — §6 */
  --breakpoint-xs: 380px;
  --breakpoint-sm: 520px;
  --breakpoint-md: 768px;
  --breakpoint-lg: 1024px;
  --breakpoint-xl: 1280px;
}

@layer base {
  html {
    background: var(--color-ink);
    color-scheme: dark;
    -webkit-text-size-adjust: 100%;
  }
  body {
    background: var(--color-ink);
    color: var(--color-ash);
    font-family: var(--font-sans);
    font-size: var(--text-body);
    line-height: var(--text-body--line-height);
    /* Global guard against the "no horizontal body scroll" constraint */
    overflow-x: clip;
  }
  h1, h2 { font-family: var(--font-display); color: var(--color-quench); }
  h3, h4 { font-family: var(--font-sans); color: var(--color-quench); }
  code, kbd, pre, samp { font-family: var(--font-mono); }

  /* Focus — DESIGN.md §9, applies to EVERY interactive element */
  :where(a, button, input, select, textarea, summary, [tabindex]):focus-visible {
    outline: 2px solid var(--color-ember);
    outline-offset: 2px;
    border-radius: var(--radius-sm);
  }

  /* Tabular numerals everywhere numbers are compared */
  table, .tnum { font-variant-numeric: tabular-nums; }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: 0.01ms !important;
      animation-iteration-count: 1 !important;
      transition-duration: 0.01ms !important;
      scroll-behavior: auto !important;
    }
  }
}

/* The machined-metal bevel. The ONLY sanctioned box-shadow (DESIGN.md §1.2). */
@utility etch {
  box-shadow: inset 0 1px 0 var(--color-etch);
}
```

- [ ] **Step 3: Wire the three fonts in `layout.tsx`**

Fraunces carries `opsz`/`SOFT`/`WONK`; `wght` is included by default and must not be listed in `axes`.

```tsx
import { Fraunces, Archivo, JetBrains_Mono } from "next/font/google";

const fraunces = Fraunces({
  subsets: ["latin"],
  display: "swap",
  axes: ["SOFT", "WONK", "opsz"],
  variable: "--font-fraunces",
});
const archivo = Archivo({
  subsets: ["latin"], display: "swap", variable: "--font-archivo",
});
const jetbrains = JetBrains_Mono({
  subsets: ["latin"], display: "swap", variable: "--font-jetbrains",
});
```

Apply to `<html className={`${fraunces.variable} ${archivo.variable} ${jetbrains.variable}`}>`.

Export metadata and viewport (Next 15 per-route mechanism, confirmed via context7):
```tsx
export const viewport = {
  width: "device-width",
  initialScale: 1,
  colorScheme: "dark",
  themeColor: "#080706",
};
```

- [ ] **Step 4: Verify the build and the tokens**

Run: `cd site && npx next build`
Expected: build completes with **zero** errors and zero warnings about unknown CSS.

Temporarily render in `page.tsx` one swatch per colour token plus one line in each of the three families, then:

Run: `npx next dev` and open `http://localhost:3000`
Expected: 18 swatches render the exact hexes from `DESIGN.md §1`; the display line is a serif with visibly chiselled letterforms (Fraunces `WONK` active), the body line is Archivo, the mono line is JetBrains Mono.

- [ ] **Step 5: Commit**

```bash
git add site/
git commit -m "feat(site): scaffold Next 15 + Tailwind v4 Forge token system"
```

---

## Task 2: The verification harness — build this before any UI

This task exists **second on purpose**. Every later task's "verify" step calls these two scripts. Without them the 180 kB gate and the no-horizontal-scroll constraint are unenforceable claims.

**Files:**
- Create: `site/scripts/check-bundle.mjs`
- Create: `site/scripts/shot.mjs`
- Modify: `site/package.json` (scripts block)
- Modify: `site/next.config.ts` (bundle analyzer)

**Interfaces:**
- Produces: `npm run gate` — exits non-zero if landing JS exceeds 180 kB gzip.
- Produces: `npm run shots` — writes `site/.shots/<width>.png`, exits non-zero on any console error or horizontal overflow.

- [ ] **Step 1: Write the bundle gate**

`site/scripts/check-bundle.mjs`:
```js
import { readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";
import path from "node:path";

const LIMIT_KB = 180;
const manifest = JSON.parse(
  readFileSync(".next/app-build-manifest.json", "utf8")
);

// The landing route's own chunks plus the shared layout chunks it loads.
const keys = ["/page", "/layout"];
const files = new Set();
for (const k of keys) for (const f of manifest.pages[k] ?? []) files.add(f);

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
console.log(`\nlanding JS total: ${kb.toFixed(1)} kB gzip  (limit ${LIMIT_KB})`);
if (kb > LIMIT_KB) {
  console.error(`FAIL: over budget by ${(kb - LIMIT_KB).toFixed(1)} kB`);
  process.exit(1);
}
console.log("PASS");
```

- [ ] **Step 2: Write the screenshot + console + overflow checker**

`site/scripts/shot.mjs`:
```js
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const WIDTHS = [1440, 1024, 768, 390];
const URL = process.env.URL ?? "http://localhost:3000";
mkdirSync(".shots", { recursive: true });

const browser = await chromium.launch();
let failed = false;

for (const width of WIDTHS) {
  const page = await browser.newPage({
    viewport: { width, height: 900 },
    deviceScaleFactor: 2,
  });
  const errors = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(String(e)));

  await page.goto(URL, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200); // let reveals settle
  await page.screenshot({ path: `.shots/${width}.png`, fullPage: true });

  const overflow = await page.evaluate(() => {
    const de = document.documentElement;
    return { scrollW: de.scrollWidth, clientW: de.clientWidth };
  });

  if (overflow.scrollW > overflow.clientW + 1) {
    console.error(
      `FAIL ${width}px: horizontal scroll (${overflow.scrollW} > ${overflow.clientW})`
    );
    failed = true;
  }
  if (errors.length) {
    console.error(`FAIL ${width}px: ${errors.length} console error(s)`);
    errors.forEach((e) => console.error(`    ${e}`));
    failed = true;
  }
  if (!failed) console.log(`PASS ${width}px`);
  await page.close();
}

await browser.close();
process.exit(failed ? 1 : 0);
```

- [ ] **Step 3: Wire scripts and the analyzer**

```bash
npm i -D playwright @next/bundle-analyzer && npx playwright install chromium
```

`package.json` scripts:
```json
{
  "dev": "next dev",
  "build": "next build",
  "start": "next start",
  "gate": "next build && node scripts/check-bundle.mjs",
  "shots": "node scripts/shot.mjs",
  "analyze": "ANALYZE=true next build"
}
```

`next.config.ts`:
```ts
import type { NextConfig } from "next";
import withBundleAnalyzer from "@next/bundle-analyzer";

const config: NextConfig = { reactStrictMode: true };
export default withBundleAnalyzer({ enabled: process.env.ANALYZE === "true" })(config);
```

- [ ] **Step 4: Prove both scripts actually fail when they should**

A gate that has never gone red is not a gate.

Run: `npm run gate`
Expected: PASS with a small number (the token page is nearly empty).

Temporarily set `LIMIT_KB = 1` and re-run.
Expected: **FAIL**, non-zero exit, "over budget by …". Restore `LIMIT_KB = 180`.

Temporarily add `<div style={{width:"3000px"}} />` to `page.tsx`, run `npm run dev` in another shell, then `npm run shots`.
Expected: **FAIL 390px: horizontal scroll**. Remove the div and re-run; expected all four PASS.

- [ ] **Step 5: Commit**

```bash
git add site/scripts site/package.json site/next.config.ts
git commit -m "feat(site): bundle gate + Playwright screenshot/console/overflow harness"
```

---

## Task 3: Content layer — every verified number, with its source

No figure may be written inline in a component. This module is the single audit surface for MASTER_BUILD_PROMPT §5.1.

**Files:**
- Create: `site/src/lib/content/ablation.ts`, `stats.ts`, `repos.ts`, `sandbox.ts`, `mcp.ts`

**Interfaces:**
- Produces: `ABLATION_ROWS: AblationRow[]`, `HONESTY_NOTE: string`, `STATS: Stat[]`, `REPOS: RepoResult[]`, `SANDBOX_FLAGS: Flag[]`, `MCP_TOOLS: McpTool[]`
- Every exported record carries `source: string` naming the repo file and line range it was verified against.

- [ ] **Step 1: Write the ablation data**

All six rows verified against `README.md:58-63`, cross-checked `RESULTS.md:78-84`.

```ts
export type AblationRow = {
  set: string; n: number; arm: "always-expensive" | "adaptive";
  success: string; calls: number; tokens: number; costUsd: number; wallS: number;
};

export const ABLATION_SOURCE = "README.md:58-63; cross-checked RESULTS.md:78-84";

export const ABLATION_ROWS: AblationRow[] = [
  { set: "5 fixture bugs", n: 5,  arm: "always-expensive", success: "5/5",   calls: 17, tokens: 38_680,  costUsd: 0.0528, wallS: 575  },
  { set: "5 fixture bugs", n: 5,  arm: "adaptive",         success: "5/5",   calls: 31, tokens: 69_615,  costUsd: 0.0237, wallS: 300  },
  { set: "16-task set",    n: 16, arm: "always-expensive", success: "16/16", calls: 81, tokens: 138_526, costUsd: 0.1505, wallS: 2717 },
  { set: "16-task set",    n: 16, arm: "adaptive",         success: "16/16", calls: 71, tokens: 136_436, costUsd: 0.0581, wallS: 812  },
  { set: "5 real OSS repos", n: 5, arm: "always-expensive", success: "2/5",  calls: 71, tokens: 329_438, costUsd: 0.3059, wallS: 2992 },
  { set: "5 real OSS repos", n: 5, arm: "adaptive",         success: "3/5",  calls: 75, tokens: 302_801, costUsd: 0.0730, wallS: 581  },
];

/**
 * VERBATIM per MASTER_BUILD_PROMPT §5.4. Ships WITH the numbers, never as
 * fine print. Do not paraphrase, shorten, or move to a footer.
 */
export const HONESTY_NOTE =
  "Costs use proxy price rates for comparable model classes on free-tier BYO " +
  "endpoints (both report $0) — the delta is a price-model delta, not a bill. " +
  "n=5 and n=16 runs are directional, not benchmark-grade. SWE-bench numbers " +
  "are deferred to Phase 6.";
```

- [ ] **Step 2: Write the stat band figures**

Each is independently verified; do not invent a fourth if only three survive.

```ts
export type Stat = {
  value: number; suffix?: string; prefix?: string;
  label: string; sub: string; source: string; asterisk?: boolean;
};

export const STATS: Stat[] = [
  { value: 2.59, suffix: "x", label: "cheaper on the 16-task set",
    sub: "n=16, 100% success in both arms",
    source: "RESULTS.md:83 (v4: OFF $0.1505 vs ON $0.0581)", asterisk: true },
  { value: 45, label: "concurrent tasks, 8 mid-run hard kills",
    sub: "45/45 success, 8/8 genuine resumes, zero leaked containers",
    source: "README.md:124-127; CHANGELOG.md:29-32" },
  { value: 3600, label: "tasks in the soak run",
    sub: "120 kills, 15/15 checks, flat artifact counts",
    source: "RESULTS.md:139-148" },
  { value: 24, suffix: "/24", label: "sandbox escape attacks held",
    sub: "sequential + 78 concurrent hostile runs, 0 findings",
    source: "INTERFACES.md:508-527; CHANGELOG.md:69-71" },
];
```

- [ ] **Step 3: Write repos, sandbox flags, and MCP tools**

`repos.ts` — only D9 entries. **bottle/click/parse are excluded by D7.** Include the honest first failure on jaraco/path.

```ts
export const REPOS = [
  { name: "jaraco/path", result: "verified", attempts: 1, calls: 6, costUsd: 0.053,
    note: "First attempt failed honestly and exposed a real harness bug (binary-artifact diff crash), which was fixed.",
    source: "README.md:102-106; CHANGELOG.md:60-64" },
  { name: "python-semver", result: "verified", note: "Module DoD: 15/15 checks.",
    source: "README.md:108-110; CHANGELOG.md:65-66" },
  { name: "more-itertools", result: "round-6-set", source: "README.md:94-95" },
  { name: "arrow",          result: "round-6-set", source: "README.md:94-95" },
  { name: "inflect",        result: "round-6-set", source: "README.md:94-95" },
  { name: "boltons",        result: "round-6-set", source: "README.md:94-95" },
];
```

`sandbox.ts` — `read-only rootfs`, `--network none`, `cap-drop ALL`, `mem-limit`, `pids-limit`, `fresh --rm container per command`. Source: `INTERFACES.md:81-88`, `CHANGELOG.md:24-28`.

`mcp.ts` — **exactly five**, no more: `query_structure`, `query_decisions`, `record_decision`, `task_status`, `list_repos`. Source: `README.md:137-141`, `INTERFACES.md:963-975`. Include the note that Vex is **also** an MCP client over stdio (`INTERFACES.md:647-657`).

- [ ] **Step 4: Add a guard test that every record cites a source**

`site/scripts/check-sources.mjs`:
```js
import { ABLATION_ROWS, ABLATION_SOURCE } from "../src/lib/content/ablation.ts";
```
Simplest robust form: a Node script that reads each file in `src/lib/content/` as text and fails if any exported array literal contains an object without a `source:` key.

Run: `node scripts/check-sources.mjs`
Expected: PASS. Then delete a `source:` line and re-run — expected FAIL. Restore.

- [ ] **Step 5: Commit**

```bash
git add site/src/lib/content site/scripts/check-sources.mjs
git commit -m "feat(site): verified content layer, every figure carrying its source"
```

---

## Task 4: Primitives

**Files:**
- Create: `site/src/components/primitives/*.tsx` (12 files, one component each)

**Interfaces:**
- Produces: `Button({ variant: "copper" | "ghost" | "secondary", size: "sm" | "md" | "lg", ... })` — **copper variant renders `text-ink`, never white** (contrast ledger).
- Produces: `Panel({ children, className })` — `bg-slab` + `border border-rule` + `etch` utility. No box-shadow.
- Produces: `SpecPlate({ items: string[] })` — the D3 hero eyebrow: lowercase mono items divided by 1px `bg-rule` hairlines, `gap-0`, each item `px-3`.
- Produces: `SectionHeader({ id, title, lead })` — `h2` in `font-display`, lead capped at `max-w-[58ch]`.
- Produces: `Hairline`, `Badge`, `Card`, `Prose`, `Link`, `Icon`, `VisuallyHidden`, `SkipLink`.

- [ ] **Step 1: Build `Button` with the contrast-correct copper variant**

```tsx
const variants = {
  copper:    "bg-copper text-ink hover:bg-ember",       // ink text — §1.7
  secondary: "bg-char text-quench border border-rule hover:bg-forge etch",
  ghost:     "text-ash hover:text-quench border border-transparent hover:border-rule",
};
```
All variants: `transition-colors duration-150 ease-forge`, `min-h-11` (44px), `rounded-md`, `font-sans font-medium`. **No scale transform on hover** (ban list).

- [ ] **Step 2: Build `SpecPlate` (D3)**

```tsx
export function SpecPlate({ items }: { items: string[] }) {
  return (
    <div className="inline-flex items-stretch border border-rule rounded-sm etch">
      {items.map((it, i) => (
        <span key={it}
          className={cn(
            "px-3 py-1.5 font-mono text-mono text-smoke lowercase",
            i > 0 && "border-l border-rule"
          )}>
          {it}
        </span>
      ))}
    </div>
  );
}
```
Note `text-smoke` is ≥14px-only per the ledger; `--text-mono` is 0.8125rem = 13px, so **use `text-ash` here instead** to stay above the contrast floor. Fix this in implementation.

- [ ] **Step 3: Build a dev-only gallery route to see every state**

Create `site/src/app/_gallery/page.tsx` rendering every primitive in every variant and size, plus a row that is focused via `autoFocus` so the focus ring is visible in a screenshot.

- [ ] **Step 4: Verify**

Run: `npm run dev`, then `URL=http://localhost:3000/_gallery npm run shots`
Expected: PASS at all four widths, zero console errors.

Manual keyboard pass: Tab through the gallery. Expected: every interactive element shows a **2px ember ring with 2px offset**; no element is skipped; no element traps focus.

Run: `npm run gate`
Expected: PASS (primitives are server components; this should barely move).

- [ ] **Step 5: Commit**

```bash
git add site/src/components/primitives site/src/app/_gallery
git commit -m "feat(site): 12 primitives with contrast-correct variants and focus rings"
```

---

## Task 5: The shader — GLSL, ogl lifecycle, and the full six-rung fallback ladder

The single highest-risk task. `MASTER_BUILD_PROMPT` §7 step 7 says get this right early because it sets the tone.

**Files:**
- Create: `site/src/components/motion/shader/forge.glsl.ts`
- Create: `site/src/components/motion/shader/mountForge.ts`
- Create: `site/src/components/motion/ShaderHero.tsx`
- Create: `site/src/components/motion/GrainOverlay.tsx`

**Interfaces:**
- Consumes: `ogl` (`Renderer`, `Program`, `Mesh`, `Triangle`) — D6.
- Produces: `mountForge(host: HTMLElement, opts: { reducedMotion: boolean; mobile: boolean }) => { destroy(): void } | null` — returns `null` when WebGL is unavailable so the caller can show the static rung.
- Produces: `<ShaderHero />` — `'use client'`, dynamically imported with `ssr: false`.

- [ ] **Step 1: Write the domain-warped fBm fragment shader**

Implements `DESIGN.md §3.1`'s formula with 5-octave fBm and two warp passes (Iñigo Quílez technique).

```glsl
precision highp float;
uniform float uTime;
uniform vec2  uResolution;
varying vec2  vUv;

float hash(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}

float noise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i), hash(i + vec2(1,0)), u.x),
             mix(hash(i + vec2(0,1)), hash(i + vec2(1,1)), u.x), u.y);
}

float fbm(vec2 p) {
  float v = 0.0, a = 0.5;
  mat2 rot = mat2(0.80, 0.60, -0.60, 0.80); // decorrelate octaves
  for (int i = 0; i < 5; i++) { v += a * noise(p); p = rot * p * 2.02; a *= 0.5; }
  return v;
}

void main() {
  float aspect = uResolution.x / max(uResolution.y, 1.0);
  vec2 p = vec2(vUv.x * aspect, vUv.y) * 2.6;
  float t = uTime;

  // Domain warp, twice — DESIGN.md §3.1
  vec2 q = vec2(fbm(p + t * 0.06), fbm(p + vec2(5.2, 1.3) + t * 0.06));
  vec2 r = vec2(fbm(p + 1.7 * q + vec2(1.7, 9.2) + t * 0.04),
                fbm(p + 1.7 * q + vec2(8.3, 2.8) + t * 0.04));
  float f = fbm(p + 1.7 * r);

  vec3 ink    = vec3(0.031, 0.027, 0.024);
  vec3 basalt = vec3(0.059, 0.051, 0.043);
  vec3 scorch = vec3(0.478, 0.247, 0.141);
  vec3 copper = vec3(0.773, 0.416, 0.243);

  float band = clamp(f * f * 2.1 + length(r) * 0.22, 0.0, 1.0);
  vec3 col = mix(ink, basalt, smoothstep(0.05, 0.42, band));
  col = mix(col, scorch, smoothstep(0.44, 0.80, band) * 0.72);
  col = mix(col, copper, smoothstep(0.80, 0.98, band) * 0.38); // crests ONLY — §3.4.2

  // Vignette to ink at the edges — §3.4.4
  vec2 c = vUv - 0.5; c.x *= aspect;
  col = mix(ink, col, smoothstep(0.95, 0.25, length(c)));
  // Fade the lower edge into the page ground
  col = mix(ink, col, smoothstep(0.0, 0.35, vUv.y));

  gl_FragColor = vec4(col, 1.0);
}
```

Vertex shader is the `ogl` fullscreen-triangle standard: `attribute vec2 uv; attribute vec2 position; varying vec2 vUv; void main(){ vUv = uv; gl_Position = vec4(position, 0, 1); }`

- [ ] **Step 2: Write `mountForge` with every performance rung**

```ts
import { Renderer, Program, Mesh, Triangle } from "ogl";
import { VERT, FRAG } from "./forge.glsl";

const FROZEN_T = 18.0; // the composed still frame for reduced-motion

export function mountForge(host, { reducedMotion, mobile }) {
  let renderer;
  try {
    renderer = new Renderer({
      alpha: false, antialias: false, depth: false,
      dpr: mobile ? 0.5 : Math.min(window.devicePixelRatio || 1, 1.5), // §3.5
      powerPreference: "high-performance",
    });
  } catch { return null; } // no WebGL -> static rung
  const gl = renderer.gl;
  host.appendChild(gl.canvas);
  Object.assign(gl.canvas.style, { width: "100%", height: "100%", display: "block" });

  const program = new Program(gl, {
    vertex: VERT, fragment: FRAG,
    uniforms: { uTime: { value: 0 }, uResolution: { value: [1, 1] } },
  });
  const mesh = new Mesh(gl, { geometry: new Triangle(gl), program });

  const resize = () => {
    const r = host.getBoundingClientRect();
    renderer.setSize(r.width, r.height);
    program.uniforms.uResolution.value = [r.width, r.height];
  };
  resize();
  const ro = new ResizeObserver(resize); ro.observe(host);

  let raf = 0, visible = true, onscreen = true;
  const draw = (t) => {
    raf = requestAnimationFrame(draw);
    program.uniforms.uTime.value = t * 0.001;
    renderer.render({ scene: mesh });
  };
  const start = () => { if (!raf && visible && onscreen) raf = requestAnimationFrame(draw); };
  const stop  = () => { if (raf) { cancelAnimationFrame(raf); raf = 0; } };

  if (reducedMotion) {
    // FREEZE, do not remove — §3.5. Render exactly one composed frame.
    program.uniforms.uTime.value = FROZEN_T;
    renderer.render({ scene: mesh });
  } else {
    const io = new IntersectionObserver(([e]) => { onscreen = e.isIntersecting; onscreen ? start() : stop(); });
    io.observe(host);
    const onVis = () => { visible = !document.hidden; visible ? start() : stop(); };
    document.addEventListener("visibilitychange", onVis);
    gl.canvas.addEventListener("webglcontextlost", (e) => { e.preventDefault(); stop(); host.dataset.lost = "1"; });
    start();
    return { destroy() { stop(); io.disconnect(); ro.disconnect(); document.removeEventListener("visibilitychange", onVis); gl.canvas.remove(); } };
  }
  return { destroy() { stop(); ro.disconnect(); gl.canvas.remove(); } };
}
```

- [ ] **Step 3: Write `ShaderHero` with the static fallback beneath**

`'use client'`. Renders `<img src="/hero-fallback.webp">` as the base layer, absolutely positioned, and mounts the canvas over it. If `mountForge` returns `null`, the image simply remains visible — the no-WebGL rung needs no extra branch. Reads `window.matchMedia("(prefers-reduced-motion: reduce)")` and `("(max-width: 768px)")`.

Imported from the Hero section as:
```tsx
const ShaderHero = dynamic(() => import("@/components/motion/ShaderHero"), { ssr: false });
```

- [ ] **Step 4: Grain + scrim**

`GrainOverlay` is a CSS-only layer at **3–5% opacity** (`DESIGN.md §3.4.3`) over the canvas, plus a `linear-gradient` scrim to `--color-ink` behind the text column so the headline clears 4.5:1 against the **brightest** shader frame, not the average (§3.4.5). Pure CSS — costs zero JS.

- [ ] **Step 5: Verify all six rungs and the budget**

| Rung | How to verify | Expected |
|---|---|---|
| Desktop WebGL2 | DevTools Performance, 5s capture | ~60fps, 1 draw call |
| Mobile | Playwright 390px + CPU throttle | renders, canvas backing store is half CSS size |
| `prefers-reduced-motion` | Playwright `colorScheme`/`reducedMotion: "reduce"` context | canvas present and **non-blank**; `uTime` never advances |
| No WebGL | Launch Chromium with `--disable-gpu --disable-software-rasterizer` | `hero-fallback.webp` visible, no console error |
| Tab hidden | `page.evaluate` dispatch `visibilitychange` with `document.hidden = true` | RAF count stops increasing |
| Scrolled past | Scroll 200vh, check RAF | stops |

Run: `npm run gate`
Expected: PASS — record the exact kB delta that `ogl` + the shader added. It should be ≈13 kB.

- [ ] **Step 6: Pre-render the static fallback**

With the shader running at `uTime = FROZEN_T`, screenshot the canvas at 1600×1000 and export to `public/hero-fallback.webp` at quality ~80. Verify the file is **under 60 kB**.

- [ ] **Step 7: Commit**

```bash
git add site/src/components/motion site/public/hero-fallback.webp
git commit -m "feat(site): domain-warped fBm ogl shader with full degradation ladder"
```

---

## Task 6: Chrome — Nav, MobileNav, Footer

**Files:**
- Create: `site/src/components/chrome/Nav.tsx`, `MobileNav.tsx`, `Footer.tsx`
- Modify: `site/src/app/layout.tsx`

**Interfaces:**
- Consumes: `site.navLinks` from Task 1; `Button` from Task 4.
- Produces: `<Nav />`, `<Footer />`.

- [ ] **Step 1: Nav — the one sanctioned glass surface**

Sticky, `bg-ink/80` + `backdrop-blur-md` + `border-b border-rule`. This is the **only** `backdrop-blur` on the site (ban list). Wordmark `vex` lowercase in `font-display`. Links: How it works · Architecture · Benchmarks · Docs · GitHub. Copper CTA with `text-ink`.

- [ ] **Step 2: MobileNav — full-screen sheet below 820px**

Requirements, all testable: Esc closes · focus returns to the trigger on close · focus is **trapped** while open (Tab from the last item wraps to the first) · `aria-expanded` on the trigger · `role="dialog"` + `aria-modal="true"` on the sheet · body scroll locked while open.

- [ ] **Step 3: Footer — 4 columns**

Product / Architecture / Docs / GitHub. Hairline top border, no box-shadow.

- [ ] **Step 4: Verify keyboard behaviour explicitly**

Run at 390px in a real browser:
1. Tab once from page load → **skip link appears first**. Activate it → focus lands on `<main>`.
2. Tab to the menu trigger, press Enter → sheet opens, focus moves inside.
3. Tab repeatedly → focus cycles **within** the sheet only.
4. Press Esc → sheet closes, focus returns to the trigger.

Expected: all four behaviours pass. Then `npm run shots` → PASS at all widths.

- [ ] **Step 5: Commit**

```bash
git add site/src/components/chrome site/src/app/layout.tsx
git commit -m "feat(site): sticky nav, focus-trapped mobile sheet, footer"
```

---

## Task 7: §2 Hero — the resolved type treatment

**Files:**
- Create: `site/src/components/sections/Hero.tsx`
- Create: `site/src/components/motion/SplitText.tsx`, `Reveal.tsx`
- Create: `site/src/components/product/Terminal.tsx`, `CommandLine.tsx`

**Interfaces:**
- Consumes: `ShaderHero`, `SpecPlate`, `Button`.
- Produces: `<Hero />`, `<Terminal steps={...} />`, `<CommandLine command={...} />` (copy button, `$` prefix, copies **without** the `$`).

- [ ] **Step 1: Compose the hero per D3 and D4**

```tsx
<SpecPlate items={["cli-first", "verifier-gated", "open source"]} />

<h1 className="font-display text-h1 text-quench">
  Fix <span className="text-copper">real</span> bugs.<br />
  Verified, not vibed.
</h1>
```
**Not** `Verified` in copper (D4). **No** ALL-CAPS dot-joined eyebrow (D3). No gradient text. No typewriter.

Lead paragraph `max-w-[58ch]`. Then `CommandLine` with the **verified** install (D2):
```
git clone https://github.com/Pavanteja2007/coding-harness && cd coding-harness && pip install -e .
```
Then two CTAs: copper primary (`text-ink`), ghost secondary.

- [ ] **Step 2: `SplitText` character stagger, 40ms**

Split the `h1` into spans per character, `opacity:0; transform: translateY(14px)`, animate to rest with `--ease-forge` over 700ms, **40ms** apart (§4.2). Under `prefers-reduced-motion`, render at final state with no animation. Preserve the accessible name: wrap the real text in `VisuallyHidden` and mark the split spans `aria-hidden="true"`.

- [ ] **Step 3: `Terminal` — the hero's proof object**

Chrome with three dots in `--color-soot` (not red/amber/green — that reads as macOS candy). Steps animate in on enter, ending on the **verifier gate turning `--color-patina-bright`**. Steps must reflect a real run shape:
```
$ vex fix --repo . --issue "mean() returns the sum, not the mean"
  plan          3 steps
  step 1/3      read mathutil.py
  step 2/3      apply fix
  verify        target test ........ pass
  verify        full suite ......... no regressions
  ✔ verified    branch vex/fix-mean · 6 calls · $0.053
```
The `✔` is a **Lucide SVG**, never an emoji (ban list). The `$0.053` and `6 calls` are jaraco/path's real figures (D9).

- [ ] **Step 4: Verify**

Run: `npm run shots` → PASS at 1440/1024/768/390.
Inspect `.shots/390.png`: the h1 must not clip, the terminal must not force horizontal scroll (it scrolls **inside** its own container).
Contrast check: sample the headline pixel against the brightest shader frame → must be **≥4.5:1**.
Run: `npm run gate` → PASS.

- [ ] **Step 5: Commit**

```bash
git add site/src/components/sections/Hero.tsx site/src/components/motion site/src/components/product
git commit -m "feat(site): hero with spec plate, copper 'real', and verifier-gate terminal"
```

---

## Task 8: §3 Stat band + §10 Benchmarks + the HonestyNote

**Files:**
- Create: `site/src/components/sections/StatBand.tsx`, `Benchmarks.tsx`
- Create: `site/src/components/product/StatTile.tsx`, `RouterTable.tsx`, `HonestyNote.tsx`
- Create: `site/src/components/motion/CountUp.tsx`

**Interfaces:**
- Consumes: `STATS`, `ABLATION_ROWS`, `HONESTY_NOTE` from Task 3.
- Produces: `<RouterTable rows={ABLATION_ROWS} />` — adaptive rows emphasised, `tabular-nums`, wrapped in `overflow-x-auto` for mobile.

- [ ] **Step 1: StatTile with count-up**

Four tiles, hairline dividers, **no cards, no box-shadow**. The number in `font-display` (this is the sanctioned "big number in a stat" use). `tabular-nums`. Count-up runs once on enter, 900ms, `--ease-forge`; under reduced motion it renders the final value immediately. Each tile shows its `n` in the sub-line. Asterisks link to `#honesty`.

- [ ] **Step 2: RouterTable**

Six rows from `ABLATION_ROWS`. Adaptive rows get `text-quench` + a `--color-rule-hot` left border; always-expensive rows stay `text-ash`. Cost and wall columns for adaptive rows use `--color-copper` (large/UI text only — these are ≥16px, so the ledger permits it). Horizontal scroll container on mobile; the table itself never causes body scroll.

- [ ] **Step 3: HonestyNote — a designed component, not fine print**

`--color-warn` left rule, `bg-char`, `etch`, a Lucide `TriangleAlert` icon, and `HONESTY_NOTE` rendered **verbatim**. Placed immediately adjacent to the RouterTable in §10 and referenced by the stat-band asterisks. `id="honesty"`.

- [ ] **Step 4: Verify the numbers against the repo one final time**

Run:
```bash
cd .. && sed -n '58,63p' README.md
```
Expected: the six printed rows match `ABLATION_ROWS` exactly — set, arm, success, calls, tokens, cost, wall. Any mismatch is a blocking defect.

Run: `npm run shots` → PASS. At 390px confirm the table scrolls **inside its container** and the body does not.

- [ ] **Step 5: Commit**

```bash
git add site/src/components/sections/StatBand.tsx site/src/components/sections/Benchmarks.tsx site/src/components/product
git commit -m "feat(site): stat band, router ablation table, verbatim honesty note"
```

---

## Task 9: §4 Thesis + §5 How it works — the one scroll-linked moment

**Files:**
- Create: `site/src/components/sections/Thesis.tsx`, `HowItWorks.tsx`
- Create: `site/src/components/product/LayerStack.tsx`, `LoopDiagram.tsx`

**Interfaces:**
- Produces: `<LoopDiagram />` — hand-authored SVG; the gate element exposes `data-gate` for the ScrollTrigger to drive.

- [ ] **Step 1: Thesis — asymmetric LayerStack, NOT a bento grid**

"The integration is the point." Four layers as an offset editorial stack with alternating indentation and hairline connectors. Explicitly **not** four equal cards (ban list). Layer names and one-line descriptions from `README.md:37-42`.

- [ ] **Step 2: LoopDiagram — hand-authored SVG**

Plan → Step (sandboxed) → **Verify (the gate)** → Git-native output. The gate is a drawn shutter that **opens** and turns `--color-patina` when the suite passes. Not an icon font, not a Lucide composite.

- [ ] **Step 3: Wire GSAP ScrollTrigger — lazily, and only here**

This is the **single** scroll-linked hero moment on the page (§4.3). A second one anywhere is a defect.

```tsx
useEffect(() => {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
    setGateOpen(true); // resolve to final state, no ScrollTrigger at all
    return;
  }
  let ctx: any;
  (async () => {
    const { gsap } = await import("gsap");
    const { ScrollTrigger } = await import("gsap/ScrollTrigger");
    gsap.registerPlugin(ScrollTrigger);
    ctx = gsap.context(() => { /* drive [data-gate] transform + colour */ }, ref);
  })();
  return () => ctx?.revert();
}, []);
```
The dynamic `import()` is what keeps GSAP out of the landing's initial chunk (D5). Verify this in Step 5.

- [ ] **Step 4: Verify reduced-motion resolves to the open gate**

Run Playwright with `reducedMotion: "reduce"`.
Expected: the gate renders **open and patina** with no scrolling required, and **GSAP is never fetched** (assert via `page.on("request")` that no URL matching `/gsap/` is requested).

- [ ] **Step 5: Verify GSAP is NOT in the initial bundle**

Run: `npm run gate`
Expected: PASS, and the printed top-12 chunk list must **not** contain a gsap chunk. Run `npm run analyze` and confirm GSAP sits in a separate async chunk.

- [ ] **Step 6: Commit**

```bash
git add site/src/components/sections/Thesis.tsx site/src/components/sections/HowItWorks.tsx site/src/components/product/LoopDiagram.tsx site/src/components/product/LayerStack.tsx
git commit -m "feat(site): thesis layer stack and the single scroll-linked gate moment"
```

---

## Task 10: §6 Harness · §7 Execution · §8 Runtime

**Files:**
- Create: `site/src/components/sections/LayerHarness.tsx`, `LayerExecution.tsx`, `LayerRuntime.tsx`
- Create: `site/src/components/product/DiffBlock.tsx`, `RationaleCard.tsx`, `SandboxFlags.tsx`

- [ ] **Step 1: §6 Harness — asymmetric 7/5 row**

Planner / step-agent / verifier. Show a real `rationale.md` excerpt in `RationaleCard` and a real `DiffBlock` (+/- gutters, `--color-patina` for additions, `--color-fail` for deletions, **plus a `+`/`−` glyph** so colour is never the sole signal).

- [ ] **Step 2: §7 Execution — mirror the row direction (5/7)**

`SandboxFlags` chip row from `sandbox.ts`: read-only rootfs · `--network none` · `cap-drop ALL` · mem-limit · pids-limit · fresh container per command. Add the verified adversarial line: **24/24 sequential attacks held; 78 concurrent hostile runs, 0 findings** (`CHANGELOG.md:69-71`).

- [ ] **Step 3: §8 Runtime — the novel mechanism**

Adaptive model routing. Re-use `RouterTable` from Task 8 (DRY — do not build a second table). Add the mechanism explanation from `README.md:44-50` and the verified detail that **96–97% of ON-arm calls ran on the cheap tier** (`RESULTS.md:100-101`). Include the honest negative from `RESULTS.md:86-97` — the v1 predictor saturated at "hard" and the ON arm cost ~2x baseline. That failure is the most on-brand content in the repo; do not omit it.

- [ ] **Step 4: Verify rhythm and responsiveness**

Run: `npm run shots`
Inspect `.shots/1440.png`: §6 and §7 must have **opposite** split directions; no two consecutive sections may share the same rhythm. Confirm no section is a 3-up icon-above-heading row.
Expected: PASS at all widths.

- [ ] **Step 5: Commit**

```bash
git add site/src/components/sections site/src/components/product
git commit -m "feat(site): harness, execution, and runtime layer sections"
```

---

## Task 11: §9 Reliability · §11 Memory+MCP · §12 Multi-repo

**Files:**
- Create: `site/src/components/sections/Reliability.tsx`, `LayerMemory.tsx`, `MultiRepo.tsx`
- Create: `site/src/components/product/SessionBoard.tsx`, `MCPToolList.tsx`, `RepoRow.tsx`

- [ ] **Step 1: §9 Reliability — SessionBoard**

Columns for running / killed / resumed / passed. Resumed cards sweep into Passed with `--color-patina` badges on enter. Figures: **45 tasks @ concurrency 45, 8 simultaneous mid-run hard kills → 45/45 success, 8/8 genuine resumes, zero leaked containers** (`README.md:124-127`). Add the soak: **3,600 tasks, 120 kills, 15/15 checks, latency p95 drift 1.02x** (`RESULTS.md:139-148`).

- [ ] **Step 2: §11 Memory + MCP**

tree-sitter code graph (**state plainly that it is Python-only** — §5.6) + SQLite decision memory + `MCPToolList` rendering **exactly five** tools from `mcp.ts`. State that Vex is **also** an MCP client over stdio. Include the memory-informed-planning result: model calls **35→27 (−23%)**, tokens **147,916→112,390 (−24%)**, past-mistake recurrences **3→0** (`INTERFACES.md:262-272`).

- [ ] **Step 3: §12 Multi-repo — RepoRow**

Render only D9 repos. **bottle/click/parse must not appear as successes** (D7). Include jaraco/path's honest first failure, which exposed and fixed a real harness bug.

- [ ] **Step 4: Verify no banned claim leaked in**

Run:
```bash
cd site && grep -rniE "swe-bench|pip install vex|multi-language|testimonial|trusted by|stars" src/ || echo "CLEAN"
```
Expected: `CLEAN`, or matches only where the text explicitly *denies* the claim (e.g. "SWE-bench numbers are deferred to Phase 6").

Run: `npm run shots` → PASS.

- [ ] **Step 5: Commit**

```bash
git add site/src/components/sections site/src/components/product
git commit -m "feat(site): reliability board, memory/MCP, multi-repo validation"
```

---

## Task 12: §13 Get started · §14 Honest by design · §15 Closing

**Files:**
- Create: `site/src/components/sections/GetStarted.tsx`, `HonestByDesign.tsx`, `Closing.tsx`
- Create: `site/src/components/product/InstallTabs.tsx`

- [ ] **Step 1: `InstallTabs` with full WAI-ARIA tab semantics**

`role="tablist"` / `role="tab"` / `role="tabpanel"`, `aria-selected`, `aria-controls`, roving `tabIndex`. Keyboard: **ArrowLeft/ArrowRight move between tabs, Home/End jump to first/last, Enter/Space activate.**

Tabs and their **verified** content (D1, D2):
- **clone + install** → `git clone …` then `pip install -e .`
- **run without installing** → `python -m cli fix --repo … --issue "…"`
- **legacy alias** → note that `harness` still works and maps to the same entry point (`pyproject.toml:22-23`)

**There is no `pip install vex` tab.** If a reviewer asks for one, the answer is D2.

- [ ] **Step 2: §14 Honest by design**

The verifier discipline, the adversarial testing, the built-with strip (Python 3.10 · litellm · Docker · tree-sitter · MCP SDK · argparse · stdlib dashboard — `README.md:236-241`). Include verbatim: *"No logos. No testimonials. The credibility is the numbers."*

- [ ] **Step 3: §15 Docs off-ramp + closing CTA + footer**

Full-width centred block — this is one of the **three maximum** centred blocks on the page (hero, closing, and at most one other).

- [ ] **Step 4: Verify tab keyboard semantics**

In a real browser, focus the tablist and press ArrowRight, ArrowLeft, Home, End.
Expected: selection moves correctly, `aria-selected` follows, the focused tab is the only one with `tabIndex=0`, and the panel content swaps.

Run: `npm run shots` → PASS.

- [ ] **Step 5: Commit**

```bash
git add site/src/components/sections site/src/components/product/InstallTabs.tsx
git commit -m "feat(site): install tabs, honesty section, closing CTA"
```

---

## Task 13: Full verification pass

Nothing here is optional. This is the task that discharges `MASTER_BUILD_PROMPT` §8.

**Files:**
- Create: `site/src/app/sitemap.ts`, `site/src/app/robots.ts`
- Create: `site/public/og.png`, `site/public/favicon.svg`
- Modify: `site/src/app/layout.tsx` (metadata, JSON-LD)
- Delete: `site/src/app/_gallery/` (dev-only, must not ship)

- [ ] **Step 1: SEO surface**

Per-page title/description, OG + Twitter tags, `og.png` (1200×630, Forge palette, no fabricated claims), `theme-color` `#080706`, `sitemap.ts`, `robots.ts`, and JSON-LD `SoftwareApplication` with `applicationCategory: "DeveloperApplication"` and the real repo URL.

- [ ] **Step 2: Run every gate**

```bash
cd site
rm -rf src/app/_gallery
npm run gate          # <180 kB gzip
npm run build         # zero errors
npm run dev &         # then:
npm run shots         # 4 widths, zero console errors, zero h-scroll
node scripts/check-sources.mjs
npx @axe-core/cli http://localhost:3000 --exit
npx lighthouse http://localhost:3000 --only-categories=performance,accessibility --chrome-flags="--headless"
```

Expected: bundle PASS · build clean · shots PASS×4 · sources PASS · axe **0 violations** · Lighthouse **performance ≥90, accessibility ≥95, CLS <0.05, LCP <2.0s**.

- [ ] **Step 3: Ban-list audit**

```bash
grep -rniE "purple|violet|#8b5cf6|#a855f7|three\.js|@react-three|box-shadow" src/ \
  | grep -v "inset 0 1px 0" || echo "CLEAN"
grep -rn "backdrop-blur" src/ | wc -l    # expect exactly 1 (Nav)
node -e "console.log(Object.keys(require('./package.json').dependencies))"  # expect NO three / @react-three
```
Expected: `CLEAN`, backdrop-blur count `1`, no three.js in dependencies.

- [ ] **Step 4: Contrast ledger verification**

Verify every pair in `DESIGN.md §1.7` with a real checker, and specifically confirm **no copper CTA renders white text**:
```bash
grep -rn "bg-copper" src/ | grep -v "text-ink" || echo "CLEAN: all copper fills take ink text"
```

- [ ] **Step 5: The judgment call**

Look at `.shots/1440.png` and `.shots/390.png` beside three current AI-agent landing pages. Per §8's final criterion: **if it could be mistaken for one of them, it has failed regardless of every check above.** If it could, identify the specific elements causing the resemblance and fix them before proceeding.

- [ ] **Step 6: Commit**

```bash
git add site/
git commit -m "feat(site): SEO surface, full verification pass, gallery removed"
```

---

## Task 14: `site/README.md` — run and deploy

- [ ] **Step 1: Write it**

Cover: prerequisites (node ≥20) · `npm install` · `npm run dev` · `npm run build` · the three gates (`gate`, `shots`, `check-sources`) and what each enforces · Vercel deploy (framework preset Next.js, no env vars needed) · a "changing a number" section stating that **all figures live in `src/lib/content/` and every record must carry a `source`**.

- [ ] **Step 2: Verify the instructions from a clean clone**

```bash
rm -rf node_modules .next && npm install && npm run build && npm run gate
```
Expected: succeeds with no undocumented step.

- [ ] **Step 3: Commit**

```bash
git add site/README.md
git commit -m "docs(site): run and deploy instructions"
```

---

## Self-Review

**1. Spec coverage.** Landing sections §1–§15 → Tasks 6,7,8,9,10,11,12. Stack (§2) → Task 1,2. Design language (§3) → Task 1 tokens, Task 5 shader. Ground truth (§5.1–5.4) → Task 3 + Task 8 Step 4 + Task 11 Step 4. CLI entry (§5.5) → D1/D2, Task 12. Claim audit (§5.6) → Task 11 Step 4 grep, D7, D8. Hard constraints (§6) → Global Constraints + Task 13. Build order (§7) → task sequence. DoD (§8) → Task 13.

**Known gaps, deliberate:** `/docs`, `/architecture`, `/benchmarks`, `/changelog`, `/about` are **not** in this plan. They are Plans 2 and 3, written after the landing ships and the component system is proven. `cmdk`, MDX, and `shiki` are therefore unused here by design (D5) — this is why the landing can hold 180 kB.

**2. Placeholder scan.** No "TBD"/"TODO"/"handle edge cases". Every verification step names a command and its expected output. Component tasks give exact props, exact token classes, and testable acceptance criteria rather than full JSX, which would exceed the code itself.

**3. Type consistency.** `mountForge(host, opts) => { destroy() } | null` is used identically in Task 5 Steps 2 and 3. `AblationRow` fields in Task 3 match `RouterTable`'s consumption in Task 8. `SpecPlate({ items })` is declared in Task 4 and consumed in Task 7. `Button` variant names (`copper`/`secondary`/`ghost`) are consistent across Tasks 4, 6, 7, 12.

**4. One correction carried into implementation.** Task 4 Step 2 notes that `SpecPlate` must use `text-ash`, not `text-smoke` — `--text-mono` is 13px and `--color-smoke` is ≥14px-only per the `DESIGN.md §1.7` ledger.
