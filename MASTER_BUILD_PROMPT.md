# MASTER BUILD PROMPT — the Vex website (v2)

> **How to use this:** paste this entire file as the first message of a **fresh
> Claude Code session** in `C:\Users\pavan\Desktop\projects\coding-harness`.
> It is written to be executed in one pass.
>
> v1 (`site-v1-backup/`, `DESIGN-v1-backup.md`) was a static violet-on-black page.
> It was rejected as generic. **Do not look at it for inspiration — only for the
> verified product numbers.** The new design language is `DESIGN.md` ("The Forge").

---

## 0 · USE THE TOOLING — do this first, not last

This project has skills and plugins installed. **You are expected to use them.**

### 0.1 Skills to invoke (in this order)

| When | Skill | Why |
|---|---|---|
| **Before writing any code** | `superpowers:brainstorming` | Pressure-test the creative direction before committing |
| **Before building** | `frontend-design` | The Anthropic plugin whose entire purpose is *avoiding generic AI aesthetics*. This is the single most relevant skill to this brief — invoke it explicitly |
| **Before building** | `ui-ux-pro-max` | Run `--design-system` for the recommendation pass, then `--domain ux` for a11y/animation rules, then `--stack nextjs` |
| **To plan** | `superpowers:writing-plans` | Write the phased plan to disk before executing |
| **To execute** | `superpowers:executing-plans` | Work the plan phase by phase |
| **For parallel work** | `superpowers:subagent-driven-development` / `dispatching-parallel-agents` | Fan out independent sections — but see §0.4 |
| **Before claiming done** | `superpowers:verification-before-completion` | Mandatory. No "it should work." |

Announce each skill as you invoke it (`Using [skill] to [purpose]`).

### 0.2 MCP plugins to use

- **`context7`** — **mandatory** before writing code against any library. Resolve
  the ID, then query docs for: **Next.js 15 App Router**, **Tailwind CSS v4
  `@theme`**, **motion (framer-motion) v12**, **GSAP ScrollTrigger**, **ogl**,
  **Lenis**, **MDX in Next**. Your training data predates these versions. Do not
  guess API surfaces.
- **`playwright`** — **mandatory** for verification. Screenshot every section at
  **1440 / 1024 / 768 / 390**, check the console for errors, test keyboard nav,
  tabs, ⌘K, mobile nav. A build is not done until it has been *seen*.

### 0.3 Artifact
When the build is complete, offer to publish a live preview via the **Artifact**
tool so the page can be viewed in a browser without a local server.

### 0.4 Subagent caution
The API has been returning intermittent 400/402 errors on long-running subagents
in this project. **Prefer doing the work in the main session.** If you do fan out,
keep each subagent's task small and bounded, and be ready to fall back to doing it
yourself.

---

## 1 · WHAT YOU ARE BUILDING

A **full marketing + docs website** for **Vex** — an open-source, CLI-first,
verifier-gated AI coding agent (a "harness").

It must be **award-caliber**: the quality bar is Linear / Vercel / Zed / Astral,
not "a nice landing page." The user's explicit words: *legendary*, *royal*,
*premium*, *techy*, *aesthetic*, **not generic, not AI slop, not templatey**, and
**it must not look like other agent/harness pages.**

### Pages

| Route | Purpose |
|---|---|
| `/` | **The showpiece.** Full landing page, §4 |
| `/docs` + `/docs/[...slug]` | Sidebar IA, ⌘K search, on-this-page TOC, MDX |
| `/architecture` | The four layers, deep-dive, diagrams |
| `/benchmarks` | Ablation tables + full methodology + honesty |
| `/changelog` | Release history from `CHANGELOG.md` |
| `/about` | Thesis, honest limitations, license |

### Docs IA
```
Getting started → Install · Quickstart · Your first fix
Guides          → Fixing a real bug · BYO provider/key · Multi-repo · Dashboard
Concepts        → The verifier gate · The four layers · Adaptive routing ·
                  Sandbox model · Decision memory · MCP
Reference       → CLI commands · Config keys · MCP tools · Trace events
```

---

## 2 · STACK (locked)

```
next@15 (App Router) · react@19 · typescript (strict)
tailwindcss@4                 — CSS-first @theme tokens, NOT tailwind.config.js
motion (framer-motion v12)    — component-level animation
gsap + ScrollTrigger          — scroll choreography (free for commercial use since Webflow)
ogl                           — the hero shader (NOT three.js — see below)
lenis                         — smooth scroll (subtle; lerp ~0.08)
lucide-react                  — icons
next-mdx-remote or @next/mdx  — docs
shiki                         — code highlighting (build-time, zero client JS)
cmdk                          — ⌘K docs search
next/font                     — Fraunces, Archivo, JetBrains Mono
```

### The shader library decision is **locked to `ogl`** — measured, not preference

| Approach | gzip for an identical fullscreen-shader quad |
|---|---|
| raw WebGL | ~0.6 kB |
| **`ogl`** | **~12.8 kB** ← **use this** |
| `three` (best-case tree-shaken) | ~130 kB |
| `@react-three/fiber` + `three` | ~241 kB |

`three`'s `WebGLRenderer` statically pulls `ShaderLib`/`ShaderChunk` and is
indivisible, so it cannot shrink below ~130 kB; and `@react-three/fiber` does
`import * as THREE`, which defeats tree-shaking entirely. **We draw one quad. We
do not need a scene graph.**

### React Bits
`reactbits.dev` — 165+ components, MIT + Commons Clause, consumed by
**copy-paste or CLI** (`npx shadcn@latest add @react-bits/<Name>-TS-TW`), not npm.
**Use it as a reference/starting point, then restyle to the Forge tokens.** If you
copy a background component, **only take an `ogl`-based one** (Aurora, Threads,
Iridescence, Plasma, LightRays, Orb, Galaxy ≈13 kB). **Never** take a
`three`/R3F-based one (Silk, Beams, Dither, Ballpit, LiquidEther ≈130–240 kB).
Anything you copy must be re-themed — shipping it with its original colors is an
instant slop failure.

---

## 3 · DESIGN LANGUAGE

**`DESIGN.md` is the authority. Read it fully before writing a line of CSS.**

Summary of what is **locked**:
- **Palette:** Obsidian + Molten Copper + Verdigris Patina. Full token table in
  `DESIGN.md §1`. Implement as Tailwind v4 `@theme` variables.
- **Type:** Fraunces (display, statements only) / Archivo (UI+body) / JetBrains
  Mono (code, labels, numerals).
- **Hero:** domain-warped fBm GLSL shader via `ogl`, with grain + vignette.
- **Depth:** hairlines + `--etch` inset. **No box-shadows.**
- **Ban list:** `DESIGN.md §5`. Treat every entry as a build defect.

---

## 4 · THE LANDING PAGE — section by section

Vary the rhythm. Alternate asymmetric 7/5 and 5/7 splits. Centered full-width
blocks are reserved for the hero and closing only.

**§1 · Nav** — sticky, the *one* glass surface (subtle `backdrop-blur`, `--ink/80`).
Wordmark `vex` (lowercase, Fraunces). Links: How it works · Architecture ·
Benchmarks · Docs · GitHub. Copper CTA with **`--ink` text**. Collapses to a
full-screen sheet below 820px (Esc closes, focus returns, focus trapped).

**§2 · Hero** — the shader behind everything, fading out by ~70vh.
- Eyebrow (mono, tracked): `CLI-FIRST · VERIFIER-GATED · OPEN SOURCE`
- `h1` (Fraunces, display): **"Fix real bugs. Verified, not vibed."** — one copper
  word, flat color, **no gradient text**. Character-stagger reveal, ~40ms apart.
- Lead paragraph (≤58ch)
- The copyable command + an `InstallTabs` teaser
- Two CTAs: copper primary, ghost secondary
- **Right/lower:** the `Terminal` component, animating its steps on enter, ending
  on the green verifier gate. This is the hero's proof object.

**§3 · Stat band** — 4 tiles, count-up on enter, tabular numerals, hairline
dividers. Each number carries its `n`. Asterisks link to the honesty note.

**§4 · The thesis** — "The integration is the point." Four layers as an
asymmetric `LayerStack`, **not** a bento grid.

**§5 · How it works** — the agent loop: Plan → Step (sandboxed) → **Verify (the
gate)** → Git-native output. Scroll-linked: the gate visibly *opens* and turns
patina when the suite passes. This is the single "hero moment" of scroll
choreography — do not add a second.

**§6 · Layer 1 · Harness** — planner/step-agent/verifier. Show a real
`rationale.md` + a `DiffBlock`. Asymmetric row.

**§7 · Layer 2 · Execution** — the Docker sandbox. `SandboxFlags` chips:
read-only rootfs, `--network none`, `cap-drop ALL`, mem-limit, pids-limit.
Mirror the row direction of §6.

**§8 · Layer 3 · Runtime** — **adaptive model routing, the novel mechanism.**
The `RouterTable` (full ablation, §5 numbers) with tabular numerals and the
adaptive rows emphasized. Horizontal scroll container on mobile.

**§9 · Reliability** — the `SessionBoard`: concurrency, mid-run hard kills,
resumes sweeping into Passed with patina badges.

**§10 · Benchmarks** — reproduce-it command, artifact paths, and the
**`HonestyNote`** (see §5.4 — verbatim, mandatory, designed as a real component).

**§11 · Layer 4 · Memory + MCP** — tree-sitter code graph + SQLite decision
memory + the 5 MCP tools. Note that Vex is **also** an MCP client.

**§12 · Multi-repo validation** — real OSS repos fixed, with cost and attempts.
Include the honest first-failure.

**§13 · Get started** — `InstallTabs` (WAI-ARIA, arrows/Home/End), copy buttons,
the real command surface.

**§14 · Honest by design** — the verifier discipline, the adversarial testing, the
built-with strip. Include the line: *"No logos. No testimonials. The credibility is
the numbers."*

**§15 · Docs off-ramp + closing CTA + footer** — 4-column footer (Product /
Architecture / Docs / GitHub).

---

## 5 · GROUND TRUTH — content rules

### 5.1 The law
**Every number on the site must be traceable to `README.md`, `RESULTS.md`,
`CHANGELOG.md`, `INTERFACES.md`, or a log under `logs/`.**
**Re-verify each one yourself by reading those files. Do not trust this prompt,
and do not trust `site-v1-backup/`.** If you cannot verify a number, cut it.

### 5.2 Router ablation (verify against `README.md` / `RESULTS.md` before shipping)

| Run | Arm | Success | Calls | Tokens | Cost* | Wall |
|---|---|---|---|---|---|---|
| 5 fixture bugs | always-expensive | 5/5 | 17 | 38,680 | $0.0528 | 575s |
| 5 fixture bugs | **adaptive** | 5/5 | 31 | 69,615 | **$0.0237** | **300s** |
| 16-task set | always-expensive | 16/16 | 81 | 138,526 | $0.1505 | 2717s |
| 16-task set | **adaptive** | 16/16 | 71 | 136,436 | **$0.0581** | **812s** |
| 5 real OSS repos | always-expensive | 2/5 | 71 | 329,438 | $0.3059 | 2992s |
| 5 real OSS repos | **adaptive** | 3/5 | 75 | 302,801 | **$0.0730** | **581s** |

### 5.3 Other verified material
- Sandbox: read-only rootfs, `--network none`, `cap-drop ALL`, mem/pids limits;
  adversarial probing found and fixed a real leak.
- Memory: tree-sitter code graph (Python-only), SQLite decision store.
- **MCP server exposes exactly 5 tools:** `query_structure`, `query_decisions`,
  `record_decision`, `task_status`, `list_repos`. Vex is **also an MCP client**
  (stdio).
- Multi-repo: `jaraco/path` (verified, 1 attempt, 6 calls, $0.053), plus
  `python-semver`, `more-itertools`, `arrow`, `inflect`, `boltons`, `bottle`,
  `click`, `parse` — **check which are real and current before listing any.**
- Stack: Python 3.10, litellm, Docker, tree-sitter, official MCP Python SDK,
  argparse CLI, stdlib dashboard, CI on Linux/macOS/Windows × 3.10/3.12.

### 5.4 The honesty note — **verbatim, mandatory, ships with the numbers**

> Costs use proxy price rates for comparable model classes on free-tier BYO
> endpoints (both report $0) — the delta is a price-model delta, not a bill. n=5
> and n=16 runs are directional, not benchmark-grade. SWE-bench numbers are
> deferred to Phase 6.

### 5.5 ⚠️ Verify the CLI entry point before writing install docs
There is a **known discrepancy**: `INTERFACES.md` documents `vex`, while some
module docstrings and `README.md` show `harness …` / `python -m cli`.
**Read `pyproject.toml` `[project.scripts]` and resolve it.** Document what is
*actually true*, and if both exist, say so plainly. **Do not invent
`pip install vex`** unless a PyPI package genuinely exists — if it doesn't, the
honest instruction is `pip install -e .` from a clone.

### 5.6 Claim audit — things the repo does NOT support
Do **not** claim: SWE-bench results; a published PyPI package (unless verified);
multi-language support (the code graph is **Python-only**); token-budgeted
context (there is no tokenizer — context is bounded by file/line counts);
semantic/embedding retrieval (keyword + structural only); a live state machine
(`harness/state_machine.py` exists but is **not wired into the running loop** —
if you mention phases, say it's a designed contract, not a shipped runtime).
Say nothing about users, stars, adoption, or customers.

---

## 6 · HARD CONSTRAINTS

### Must
- Fully responsive **380 → 1920**. No horizontal body scroll at any width.
  Wide tables/mockups scroll **inside their own container**.
- `prefers-reduced-motion`: everything resolves to final state; **the shader
  freezes on a composed still frame rather than disappearing**.
- Keyboard-complete: skip link, visible `--ember` focus ring (2px + 2px offset),
  tabs, accordion, ⌘K, mobile nav focus trap + Esc.
- Contrast: verify the `DESIGN.md §1.7` ledger. **Copper CTAs take `--ink` text,
  not white** (white on copper fails AA).
- SEO: per-page title/description, OG + Twitter tags, OG image, `theme-color`
  `#080706`, sitemap, robots, JSON-LD `SoftwareApplication`.
- Perf: LCP < 2.0s, CLS < 0.05, **landing JS < 180 kB gzip including the shader**.
- Shader: DPR ≤1.5, half-res on mobile, RAF paused when offscreen or tab hidden,
  static fallback if WebGL is unavailable.

### Must not
- Anything on the `DESIGN.md §5` ban list.
- Violet/purple anywhere. That was v1.
- `three.js` or `@react-three/fiber` for the background.
- Fabricated logos, testimonials, quotes, metrics, or star counts.
- `box-shadow` for elevation.
- Emoji as UI icons.
- More than one scroll-linked hero moment.
- Gradient text on headings.

---

## 7 · BUILD ORDER

1. **Skills pass** — `frontend-design`, `ui-ux-pro-max`, brainstorm the direction.
2. **Read** `DESIGN.md`, `README.md`, `RESULTS.md`, `INTERFACES.md`,
   `pyproject.toml`, `CHANGELOG.md`, `docs/`. Extract and verify every number.
3. **context7 pass** — pull current docs for Next 15, Tailwind v4, motion, GSAP,
   ogl, MDX.
4. **Plan** — write the phased plan to disk (`superpowers:writing-plans`).
5. **Scaffold** — `site/` (fresh; `site-v1-backup/` stays untouched). Next 15 +
   TS + Tailwind v4 `@theme` with the Forge tokens. Fonts via `next/font`.
6. **Primitives** — build `DESIGN.md §7.1` before any page.
7. **Shader** — the ogl hero + grain + vignette + the whole fallback ladder.
   Get this right early; it sets the tone for everything.
8. **Landing** — §4, section by section, verifying visually as you go.
9. **Docs + remaining routes.**
10. **Polish** — motion choreography, micro-interactions, focus states, empty states.
11. **Verify** — Playwright at 4 widths, console clean, keyboard pass, contrast
    check, Lighthouse. Then `superpowers:verification-before-completion`.
12. **Report** — what's built, what's verified, what's left. Offer an Artifact preview.

---

## 8 · DEFINITION OF DONE

- [ ] All 6 routes build and render; `next build` clean; **zero console errors**
- [ ] Screenshotted at 1440 / 1024 / 768 / 390 — **no horizontal scroll anywhere**
- [ ] The shader runs at 60fps, degrades on mobile, freezes under reduced-motion,
      falls back without WebGL, and pauses offscreen
- [ ] Keyboard-complete; focus ring visible on every interactive element
- [ ] Every number traced to a repo file; honesty note ships with the numbers
- [ ] CLI entry point verified against `pyproject.toml` — no invented install
- [ ] Zero ban-list violations; zero violet; no `three.js` in the bundle
- [ ] Landing JS < 180 kB gzip
- [ ] `site/README.md` documents run + deploy
- [ ] **It does not look like any other AI-agent site.** If it could be mistaken
      for one, it has failed regardless of every checkbox above.

---

## 9 · THE STANDARD

The user asked for *legendary*. That means:

- **Restraint reads as expensive.** One accent, used sparingly. Small movements.
  Slow ambient motion. Most of the page is quiet — that's what makes the copper
  land.
- **Real artifacts beat decoration.** A working terminal, an actual diff, a real
  ablation table, a genuine code graph. Product truth *is* the visual interest.
  Never a stock illustration, never a floating 3D shape.
- **Craft lives in the details** — optical type tracking, hairline `--etch`
  bevels, 40ms staggers, grain over the shader, tabular numerals, focus rings
  that look designed rather than defaulted.
- **Honesty is the brand.** This product refuses to claim success without a green
  suite. The site must hold the same standard: every claim sourced, every caveat
  visible, nothing invented.

Build it like the portfolio piece it deserves to be.
