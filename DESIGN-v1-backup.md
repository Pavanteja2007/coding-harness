# Vex — Landing Page Design Plan (`DESIGN.md`)

> **Stage 1 deliverable — research + plan only.** This document does **not** build
> the page. It captures what the best coding-agent / dev-tool product pages
> actually do (Task A), then turns that into a concrete, Vex-specific design plan
> (Task B). The companion file `MASTER_BUILD_PROMPT.md` is the ready-to-run build
> spec (Task C).
>
> **The one locked, non-negotiable constraint:** the brand color anchor is the
> **dark-violet-and-black** palette with the exact hex tokens in
> [§3 Color System](#3-color-system--the-locked-brand-anchor). Everything else —
> information architecture, motion, spacing, typography pairing, section
> comprehensiveness — is derived from the research below, not from the palette.
>
> **Palette provenance note:** the task named `VEX_DESIGN_SYSTEM.md` as the hex
> source. That file does not exist anywhere in the repo, adjacent folders, or
> tracked source (the only palette in the codebase is the CLI's amber/ember
> `VEX_THEME`, which is explicitly *not* the page palette). With the user's
> authorization, the token set in §3 is a **proposed, WCAG-checked
> dark-violet-and-black system** that serves as the canonical anchor. It is
> structured so that if the real `VEX_DESIGN_SYSTEM.md` surfaces, its values drop
> straight into the same token names with no other change to this plan.

---

## 0. What Vex is (grounding for every content decision)

Vex is a **CLI-first AI coding-agent harness that fixes real software bugs
end-to-end**. It is not a chat wrapper or an autocomplete; it is a whole system
where four layers are integrated deliberately — *the integration is the point.*

| Layer | What it is |
|---|---|
| **Harness** | planner → step agent → **verifier gate**; repo snapshot + diff; git-native output (branch + commit + PR text); `rationale.md`; resume contract |
| **Execution** | Docker sandbox — fresh container per command, read-only rootfs, no network, resource limits, `cap-drop ALL`; stateless verify + flake detection |
| **Runtime** | process-per-task scheduler (proven 10–50 concurrent); checkpoint/resume across hard kills; approval gate; **adaptive model router** + per-call cost ledger |
| **Memory + MCP** | tree-sitter code graph; SQLite decision memory; MCP server exposing 5 tools; MCP client for external servers |

**The novel mechanism — adaptive model routing.** Per call, the runtime predicts
difficulty (intrinsic signal from the issue text + struggle signal from the
conversation tail) and routes easy/medium calls to a cheap model, hard calls to
an expensive one. Every call lands in a per-task JSONL ledger — *measurable, not
asserted.*

**Verifier-gated completion.** A task reports `success` only when the target test
passes **and** the full suite shows no regressions. Honesty is enforced by the
architecture — this is Vex's single strongest differentiator and the spine of
the page's credibility strategy.

**The real, defensible numbers (from `README.md` / `RESULTS.md`):**

- Adaptive routing: **same 100% success at ~45% (n=5) / ~39% (n=16) of baseline
  cost**; on 5 real OSS repos, 3/5 vs 2/5 at ~24% of the cost and ~5× faster wall.
- Runtime: **45 tasks @ concurrency 45 with 8 mid-run hard kills → 45/45 success,
  8/8 resumes, zero leaked containers.**
- **7 real OSS repos** driven end-to-end by the real stack (jaraco/path,
  python-semver, more-itertools, arrow, inflect, boltons, +).
- **~300 tests**; adversarially hardened (Docker sandbox 24/24 attack suites;
  101 adversarial tests pinning a real fixed leak); CI on Linux/macOS/Windows ×
  Python 3.10/3.12. Current release **v0.1.0**.
- Stack: Python 3.10 · litellm (BYO-key, multi-provider) · Docker · tree-sitter ·
  MCP (official Python SDK) · argparse CLI · stdlib HTTP dashboard.

> **Honesty guardrail (applies to the whole page):** costs use proxy price rates
> on free-tier BYO endpoints; n=5 / n=16 are directional, not benchmark-grade.
> Numbers must appear **with the same asterisked honesty footnote the README
> uses.** Vex has **no** enterprise customers, celebrity testimonials, or large
> GitHub star count — and the page must **never fabricate** them. See
> [§8 Credibility strategy](#8-credibility-strategy--what-vex-may-and-may-not-claim).

---

## Task A — Research findings

Nine live reference pages studied first-hand (headless browser + `getComputedStyle`
for type/color, live DOM for section order, verbatim copy). Additional sites
chosen beyond the required list: **Cognition/Devin** (autonomous-agent category
leader), **Factory** (closest analog — enterprise autonomy stack), **Claude Code**
(the gold-standard CLI-agent page), **Zed** (elite dev-tool craft + credibility),
with an **Amp** note (multi-model routing analog to Vex's router).

### Reference matrix (measured)

| Site | Ground | Display type | Mono | Motion budget | Credibility idiom |
|---|---|---|---|---|---|
| **Linear** | `#08090A` near-black | Inter Variable **510**, −0.022em | (app only) | 1 gradient, 0 blur, fade-up only | Taste + logo wall + status testimonials (40,000 teams) |
| **Warp** | near-black | tight sans | **core to brand** | signature magenta→violet gradient + demos | **model×cost table**, "$80→$30 per PR", 800k devs |
| **Claude Code** | `#FAF9F5` warm paper | serif **400 @ 84px** | very heavy (148 nodes) | 1 gradient, 0 blur | copyable `curl \| bash`, live code, 32-logo wall, named quotes |
| **Cursor** | `#F7F7F4` warm paper | CursorGothic **400**, −0.325px | Berkeley Mono | 0 canvas/video/blur, 21 subtle gradients | living DOM agent mockups, Jensen Huang / Karpathy quotes |
| **Devin Desktop** | `#F7F6F5` warm paper | NB Intl Pro **400 @ 64px**, −1.92px | Geist/IBM Plex (406) | 0 video, DOM session-board | the interface *is* the proof (Kanban of live agents) |
| **Cognition** | near-monochrome | big grotesque | none | austere, motion withheld | dated release log + enterprise logos, "60% Lower Cost" |
| **Aider** | `#F8F9FA` light | Inter **800 @ 40px** | very heavy (commands = texture) | 1 img, dark terminal blocks | **reproducible polyglot leaderboard**, "88% written by itself" |
| **Cline** | `#F8FAFB` light | DM Sans **400 @ 72px**, −1.44px | install block only | 1 autoplay film, 2 canvas | 8M devs, enterprise logo wall, copyable `npm i -g cline` |
| **Factory** | dark hero / light body | Geist **400 @ 36px**, −1.12px | Geist Mono (56) | sparse, big type | "autonomy stack" thesis, enterprise logos, clear pricing |
| **Zed** | dark home | IBM Plex Serif + custom sans | zedMono (213) | animated real product mockups | **named engineers** (Valim, Abramov, Bostock) + hard install #s |

### The six dimensions, synthesized

**1. Information architecture.** Two shapes work: the *confident-short* page
(Cognition 4 sections, Factory ~5) and the *deep single-scroll* page (Claude Code
11, Cursor 13, Devin 15). Both **route real depth to a docs subdomain** rather
than dumping it inline, and both **"show, don't tell"** — feature sections are a
one-line heading + a live product mockup + a "Learn about…" link, never a wall of
prose. Linear's spine is the most reusable: **four alternating capability blocks**
(text left / product-UI right, alternating) between a hero and a closing CTA.
Claude Code's **named "Get the technical rundown" docs off-ramp** treats docs as a
first-class destination, not a footer link.

**2. Hero treatment.** Static or lightly-animated, **never a hype video**. The
strongest CLI-agent pattern (Claude Code, Warp, Aider) is a **copyable install
command in the hero** plus a **live terminal/product artifact** showing the agent
actually working. Headlines are terse and confident: a bare wordmark (Claude Code
"Claude Code", Devin "Devin Desktop") or a one-line thesis (Cursor "…your coding
agent for building ambitious software", Factory "THE INDUSTRIAL REVOLUTION…").
CTAs are dual-track: a self-serve primary + a softer secondary (docs / demo).

**3. Visual & motion language.** **Restraint is the premium signal, and it is
measurable.** Linear: 1 gradient, 0 blur. Claude Code: 1 gradient, 0 blur, 0
backdrop-filter. Cursor: 0 canvas, 0 video, 0 glassmorphism. The universal
pattern: **one signature accent** (Warp's single gradient glow) on an otherwise
flat ground, **glass reserved for the sticky nav only**, and motion limited to
**fade/slide-up on scroll** + micro-animation *inside* product mockups. **No
parallax, no swarming orbs, no decorative blur.** The "wow" comes from a
**living DOM product mockup** (Cursor's "Mission Control", Devin's session-board
Kanban, Zed's editor catching a type error) — not from effects.

**4. Typography.** A precise **two-font backbone**: a tightly-tracked grotesque
(or a serif display accent — the 2026 "crafted" cue, per Claude Code + Zed) over
a **strong, named monospace** that does double duty as brand texture (uppercase
eyebrow labels) and as code/terminal chrome. The premium cue is consistently
**regular-to-medium weight + tight negative tracking** (Linear 510/−0.022em,
Devin 400/−1.92px, Cursor 400/−0.325px), *not* heavy bold — Aider's 800 is the
utilitarian outlier. Monospace usage is heavy on every credible dev-tool page.

**5. Technical credibility.** The dev-tool market rewards **verifiable numbers
over adjectives.** The two most transferable idioms for Vex: Warp's
**model×cost benchmark table** ("$80→$30 per PR", 96% eval pass rate) and Aider's
**reproducible leaderboard** (225 named exercises, methodology stated, "reproduce
this") + its **self-referential dogfooding stat** ("88% of the last release
written by Aider itself"). Everyone else leans on **logo walls + named quotes**
(Cursor: Jensen Huang; Zed: José Valim/Dan Abramov; Claude Code: 32 logos) — a
lever Vex honestly lacks and must not fake.

**6. Spacing & layout rhythm.** Airy, rigorously gridded, 8px-based. Content in a
centered ~1024–1120px column with **full-bleed product panels breaking wider**.
Separation via **1px hairline borders / low-opacity fills**, not heavy dividers
or drop-shadow theatrics. Generous vertical section padding sets a slow,
confident cadence. Depth = layered flat panels (Linear `#08090A` page →
`#18191A` card), not shadows.

---

## Task B — the Vex design plan

### 1. Positioning & thesis

- **Category claim (Factory-style thesis line):**
  **"The verifier-gated autonomy stack for fixing real bugs."**
- **What the page argues, in order:** (1) Vex fixes real bugs end-to-end; (2) it
  only *claims* success when your tests agree (the verifier gate); (3) it does so
  at ~40% of the naive cost via adaptive model routing; (4) it survives crashes at
  scale; (5) it remembers, and speaks MCP; (6) here are the reproducible numbers,
  here's how to run it, here are the docs.
- **Audience:** engineers and technical leads who evaluate tools by re-running
  the numbers — so the page's job is to be *credible and reproducible*, not
  glossy.
- **Voice:** lowercase, engineer-native (Warp), confident and terse (Cursor,
  Linear), honest by default (Vex's own brand). Monospace carries the technical
  register.

### 2. Information architecture

A **deep single-scroll page** (Claude Code / Linear hybrid) that treats Vex as a
genuine whole-system product — with real "how it works" depth — while routing the
deepest material to the existing docs. Structural spine = Linear's alternating
capability blocks; each layer *shows* a living mockup (Cursor/Devin) rather than
explaining in prose. **~15 sections:**

| # | Section | Purpose | Content brief | Inspired by |
|---|---|---|---|---|
| 1 | **Sticky nav** | wayfinding | Wordmark `vex`; links: How it works · Architecture · Benchmarks · Memory · Docs · GitHub; primary "Get started". Glass (only glass on page). | Linear nav (backdrop-blur), Warp taxonomy |
| 2 | **Hero** | thesis + proof in one view | Thesis headline + subhead; **copyable install line**; primary "Get started" + secondary "Read the docs"; **animated terminal** running `vex fix …` → plan → sandbox → **verifier gate turns green** → diff. Single violet orb behind. | Claude Code (`curl\|bash` + terminal), Aider (dark terminal), Warp (gradient) |
| 3 | **Stat band** | instant credibility | 3–4 mono number tiles, count-up on scroll: **~39% of baseline cost** · **100% verifier-gated success (n=16)** · **45/45 @ concurrency 45, 8 kills** · **7 real OSS repos**. Asterisk → honesty footnote. | Aider stat strip, Warp numbers |
| 4 | **The thesis / four layers** | frame the system | One-line "integration is the point" statement + a **labeled architecture matrix** (the 4 layers, one line each) as an interactive diagram, not four generic cards. | Factory thesis, Cline architecture matrix |
| 5 | **How it works — the loop** | the centerpiece, shown not told | Animated/stepped DOM mockup: **plan → step (in sandbox) → verify → git-native output**, with the gate visibly gating. | Devin plan→code→test→PR, Cursor living mockup |
| 6 | **Layer 1 · Harness** | depth block | planner/step/verifier, snapshot+diff (original never touched), `rationale.md`, resume contract. Mockup: rationale + diff. | Linear alternating block |
| 7 | **Layer 2 · Execution** | depth block | Docker sandbox: fresh container/command, read-only rootfs, **no network**, `cap-drop ALL`, resource limits; stateless verify + flake detection; **24/24 adversarial**. Mockup: sandbox spec chips. | Linear block; Zed "just works" |
| 8 | **Layer 3 · Runtime + the router** | **elevated differentiator** | The **adaptive model router** — difficulty prediction (intrinsic + struggle) → easy/medium→cheap, hard→expensive → per-call JSONL ledger. Scheduler, checkpoint/resume, approval gate. Home of the **model×cost table**. | Warp model×cost table, Amp routing |
| 9 | **Runtime reliability** | proof block | **45 @ 45, 8 mid-run kills → 45/45, 8/8 resumes, 0 leaked containers**, shown as a **live session-board** (Running / Verifying / Passed, per-task route + checkpoint). | Devin session-board Kanban |
| 10 | **The numbers — ablation** | credibility centerpiece | The three ablation runs as a **reproducible benchmark table** (arm · success · calls · tokens · cost · wall) + artifact paths + "reproduce this". Full honesty footnote. | Aider leaderboard, Warp table |
| 11 | **Layer 4 · Memory + MCP** | depth block | tree-sitter code graph; SQLite decision memory (auto-ingests every task); **MCP server, 5 tools**; MCP client. Mockup: `vex mcp call … query_decisions` + JSON. | Linear "AI & automations" block |
| 12 | **Multi-repo validation** | momentum proof | 7 real OSS repos driven end-to-end (jaraco/path full DoD $0.053/6 calls; python-semver 15/15; more-itertools/arrow/inflect/boltons). "Beyond its home turf." | Cognition dated release log |
| 13 | **Install / Get started** | conversion | Tabbed **copyable** quickstart: zero-setup offline demo (`python demo/run_demo.py`) · real-model `vex fix …` · MCP · dashboard. CLI command list. | Cline install tabs, Claude Code get-started |
| 14 | **Honest by design + stack** | trust without logos | The verifier-gate honesty statement (no false success); stack badges (Python 3.10, litellm, Docker, tree-sitter, MCP, ~300 tests, CI matrix, v0.1.0); adversarial-tested note. | Aider dogfooding honesty, Zed hard numbers |
| 15 | **Docs off-ramp + closing CTA + footer** | hand off + convert | "Get the technical rundown" → README / INTERFACES / architecture-harness / CHANGELOG. Dual CTA: "Fix your first bug." / "Read the docs." Footer link map. | Claude Code docs off-ramp, Linear closing CTA |

**Depth-vs-restraint resolution:** the page is long and genuinely comprehensive
(every real layer + the router + reliability + memory + numbers), but each
section is *one clear idea shown as a mockup*, and the deepest material is linked
out to the real docs — satisfying "not a single-scroll teaser" without becoming a
text-heavy anti-pattern.

### 3. Color System — the locked brand anchor

Dark-violet-and-black. **These token names and hex values are canonical for the
build.** Contrast ratios are against `--bg` unless noted; verify all pairs at
build time (see accessibility in the build prompt).

```css
:root{
  /* ---- Backgrounds (near-black, violet undertone) ---- */
  --bg:          #0A0711;   /* page ground */
  --surface:     #14101E;   /* cards / panels */
  --surface-2:   #1B1526;   /* raised / nested panels */
  --hairline:    #2A2136;   /* 1px borders / dividers */
  --hairline-soft: rgba(196,181,253,0.08); /* violet-tinted low-opacity fill */

  /* ---- Brand violet ramp ---- */
  --violet-300:  #C4B5FD;   /* bright accents, small-text links on dark (~9:1) */
  --violet-400:  #A78BFA;   /* default accent / focus ring (~7:1) */
  --violet-500:  #8B5CF6;   /* primary brand / CTA start (~5.4:1) */
  --violet-600:  #7C3AED;   /* buttons / CTA end */
  --violet-700:  #6D28D9;   /* gradient end / orb core */

  /* ---- Text ---- */
  --text:        #F4F1FB;   /* primary text (~15:1) */
  --text-muted:  #A79FB8;   /* secondary / body-muted (~6.5:1) */
  --text-faint:  #6E667E;   /* large text & meta only — NOT for body */

  /* ---- Functional (kept violet-adjacent; used sparingly) ---- */
  --ok:          #4ADE80;   /* verifier-pass green — the ONE non-violet accent, earns its place */
  --warn:        #FBBF24;   /* escalation / caution */
  --danger:      #F87171;   /* failure / regression */

  /* ---- Signature effects ---- */
  --cta:         linear-gradient(135deg, var(--violet-500), var(--violet-600));
  --focus-ring:  var(--violet-400);            /* 2px ring + 2px offset */
  --orb:         var(--violet-700);            /* low opacity, heavy blur, ONE per viewport */
}
```

**Rules:**
- **`--ok` green is the only sanctioned non-violet color.** It exists because the
  verifier gate *turning the suite green* is the emotional payload of the whole
  page — the green is meaningful, not decorative. Use it only for pass states.
- **Small text / links on dark use `--violet-400` or `--violet-300`, never
  `--violet-500`** (which is ~5.4:1 — fine for large text and UI, tight for body).
- **Primary CTA label = pure `#FFFFFF`, ≥16px semibold** (large-text threshold),
  on the `--cta` gradient. Verify ≥3:1 for the button; add a subtle
  `--violet-400` inner hairline for definition.
- **The orb:** exactly **one** low-opacity, heavily-blurred `--orb` radial per
  viewport section max — this is Warp's "single signature gradient", recolored to
  brand violet. No multi-orb fields.
- **Depth = layered flat panels + hairlines** (`--bg`→`--surface`→`--surface-2`),
  Linear-style — not drop shadows.

> **Cited from research:** dark near-black canvas + one signature gradient accent
> = Linear (`#08090A`) × Warp (single gradient). Violet undertone replaces
> Linear's neutral gray and Warp's magenta as Vex's brand. Panel-layering-not-
> shadows = Linear.

### 4. Typography

**Backbone = precise grotesque + strong monospace, all free/self-hostable** (no
paid faces; the reference pages use commissioned type, which we emulate honestly
with the best open equivalents):

| Role | Face | Notes | Cited from |
|---|---|---|---|
| **Display / headings** | **Inter** (variable) | weight **520–560** (emulating Linear's bespoke 510), **tracking −0.02em**, line-height 1.0–1.1 on large sizes | Linear (Inter Var 510 / −0.022em) |
| **Body / UI** | **Inter** | 400/450, 1.6 line-height, `--text` / `--text-muted` | Linear (single-family discipline) |
| **Monospace** | **JetBrains Mono** | terminal, code, diffs, benchmark table, stat numbers, **uppercase eyebrow labels** | Warp/Devin/Aider heavy-mono; IBM Plex Mono is an acceptable swap |
| **Serif accent (optional, scoped)** | **Instrument Serif** or **Newsreader** | *at most* the wordmark lockup or ONE editorial pull-quote — the 2026 "crafted" cue | Claude Code (anthropicSerif), Zed (IBM Plex Serif) |

**Type scale (rem, 1rem = 16px):** hero display 3.5–4.5 · H2 2.25–2.75 · H3 1.5 ·
body-lg 1.125 · body 1.0 · mono-label 0.8125 (uppercase, +0.08em tracking) ·
caption 0.875. Fluid via `clamp()`.

**Weight discipline:** never heavier than ~560 for display (premium = tight
tracking on medium weight, per Linear/Cursor/Devin — *not* bold). Aider's 800 is
noted and deliberately **not** adopted.

### 5. Spacing, grid, radii, elevation

- **Grid:** 8px base. Centered content column **max-width ~1120px**; full-bleed
  product panels break to ~1320px. Side gutter ≥24px (≥16px on mobile). *(Linear.)*
- **Vertical rhythm:** section padding-block `clamp(80px, 12vh, 160px)` — airy,
  slow cadence. *(Linear / Claude Code.)*
- **Radii (engineered, not consumer):** buttons/inputs **8px**; cards/panels
  **14px**; terminal window **12px**; pills (eyebrow, tags) full-round. Deliberately
  *not* Cursor's full-pill buttons (too consumer) and *not* Devin's 2px (too
  harsh) — a defensible middle that reads "tool". *(Devin/Factory engineered feel,
  moderated.)*
- **Elevation:** hairline borders (`--hairline`) + low-opacity fills
  (`--hairline-soft`); **no heavy shadows.** One soft violet glow allowed on the
  hero terminal and the primary CTA on hover. *(Linear depth model.)*

### 6. Motion language (a strict budget)

Match the measured restraint of Linear (1 gradient, 0 blur) and Claude Code:

- **Allowed:** fade/slide-up reveals on scroll (IntersectionObserver, 12–20px
  travel, 300–500ms, staggered ≤80ms); the hero terminal **typing/step
  animation** (steps appear in sequence, gate flips to green); **number count-up**
  on the stat band; the session-board rows advancing state once; hover =
  **color/opacity/border only** (no layout-shifting transforms); the single orb
  drifts very slowly (20s+) — or is static under reduced-motion.
- **Forbidden:** parallax, multiple/swarming orbs, glassmorphism anywhere except
  the sticky nav, decorative blur, autoplaying hype video, scale-on-hover that
  shifts layout, anything busier than "the agent demonstrating itself."
- **`prefers-reduced-motion`:** disable reveals/typing/count-up/drift; show final
  states immediately. *(ui-ux-pro-max `reduced-motion` rule.)*
- Micro-interaction timing 150–300ms; use `transform`/`opacity` only. *(ui-ux-pro-max.)*

> **Cited from research:** "the demo is a living DOM mockup, not a video" =
> Cursor + Devin; "one gradient, zero blur, fade-up only" = Linear + Claude Code;
> "motion demonstrates the agent, doesn't decorate" = Devin/Cognition restraint.

### 7. Component system (brief)

- **Buttons:** primary = `--cta` gradient, white label, 8px radius, violet glow on
  hover; secondary = transparent + `--hairline` border + `--text`, fills to
  `--surface-2` on hover. Lowercase engineer-voice labels ("get started", "read
  the docs"). *(Warp CTA voice; Devin two-tier corners → moderated.)*
- **Sticky nav:** glass (`backdrop-filter: blur(12px)` over `--bg` at ~72%
  opacity) + bottom hairline. The **only** glass on the page. *(Linear.)*
- **Terminal / code block:** `--surface` panel, `--hairline` border, 12px radius,
  three faux window dots, JetBrains Mono, syntax tint via violet ramp; `--ok`
  green reserved for PASS lines; a **Copy** button on install blocks. *(Aider dark
  terminal on the page; Claude Code copyable install.)*
- **Benchmark table:** mono numerals, right-aligned; hairline row separators; the
  adaptive arm's cost cell highlighted with `--violet-400`; footnote row for the
  honesty asterisk. *(Warp model×cost table, Aider leaderboard.)*
- **Stat tile:** big mono number (count-up) + `--text-muted` label + optional
  asterisk. *(Aider stat strip.)*
- **Architecture matrix / layer cards:** four hairline panels on `--surface`,
  mono eyebrow label, one-line description, hover raises border to `--violet-400`.
- **Footer:** dense multi-column link map on `--bg`, hairline top. *(Linear/Cursor.)*
- **Icons:** inline SVG only (Lucide/Heroicons), 24×24 viewBox — **no emoji as
  UI icons.** *(ui-ux-pro-max `no-emoji-icons`.)*

### 8. Credibility strategy — what Vex may and may not claim

This is the most important non-color decision on the page. The research shows two
credibility idioms: **(A) logos + celebrity quotes** (Cursor, Zed, Claude Code,
Cline, Factory) and **(B) reproducible numbers + dogfooding honesty** (Aider,
Warp). **Vex must use idiom B exclusively.**

**Honest levers Vex actually has (use these):**
- The **reproducible ablation table** with artifact paths (`logs/ablations/…`) and
  a "reproduce this" pointer — Aider's leaderboard move.
- The **verifier-gate honesty statement**: "Vex only reports success when your
  target test passes and the full suite shows no regressions" — the trust anchor.
- **Runtime reliability data** (45/45, 8/8 resumes, 0 leaked containers) from the
  event journal.
- **7 real OSS repos** driven end-to-end; the one honest first-failure that
  exposed and fixed a real bug (this *increases* credibility — keep it).
- **Adversarial hardening** (24/24 sandbox suites; 101 tests pinning a real leak).
- **Stack + test count + CI matrix + v0.1.0** as concrete engineering signals.

**Forbidden (do not fabricate — enforced in the build prompt's DO-NOT list):**
- ❌ Enterprise customer logos Vex does not have.
- ❌ Named/celebrity testimonials or any invented quotes.
- ❌ Inflated GitHub stars, user counts, "trusted by N teams".
- ❌ SWE-bench numbers (explicitly deferred to Phase 6 in the README).
- ❌ Presenting proxy-rate costs as real bills, or n=5/n=16 as benchmark-grade.

**Honesty footnote (must appear near every headline number), adapted from README:**
> \* Costs use proxy price rates for comparable model classes on free-tier BYO
> endpoints (both report \$0); the delta is a price-model delta, not a bill. n=5
> and n=16 runs are directional, not benchmark-grade. SWE-bench numbers are
> deferred to Phase 6.

Presenting limitations openly is *on-brand* for a verifier-gated product — honesty
is the differentiator, so the footnote is a feature, not fine print.

### 9. Reference map — every major decision → its source

| Vex decision | Cited reference |
|---|---|
| Dark near-black canvas, flat panels, hairline depth | **Linear** (`#08090A`, `#18191A`, `rgba(255,255,255,.05)`) |
| Single signature gradient accent (recolored violet) | **Warp** (one "warp gradient") |
| Inter display at medium weight + tight −0.02em tracking | **Linear** (Inter Var 510 / −0.022em) |
| Heavy monospace as brand texture + eyebrow labels | **Devin/Warp/Aider** (Geist/IBM Plex/commands-as-texture) |
| Optional serif accent for wordmark/pull-quote | **Claude Code** (anthropicSerif), **Zed** (IBM Plex Serif) |
| Copyable install command in hero | **Claude Code** (`curl\|bash`), **Cline** (`npm i -g`), **Warp** (`curl`) |
| Animated terminal proof (fix → gate turns green) | **Aider** (dark terminal), **Claude Code** (terminal demo) |
| "Show, don't tell" via living DOM mockups (no video) | **Cursor** (Mission Control), **Devin** (session board) |
| Four alternating capability blocks = the four layers | **Linear** (Intake/Planning/AI/Build) |
| Session-board Kanban for runtime reliability | **Devin Desktop** (Running/Review/Done) |
| Model×cost benchmark table for the router | **Warp** (per-model cost/quality rows) |
| Reproducible benchmark + "reproduce this" + dogfooding honesty | **Aider** (polyglot leaderboard, "88%") |
| "Autonomy stack" thesis line | **Factory** ("the autonomy stack for enterprise teams") |
| Feature trio (Autonomous / Verified / Cheaper) option | **Zed** (Fast / Agentic / Collaborative) |
| Dated release/validation log as momentum | **Cognition** (Articles feed) |
| Named "Get the technical rundown" docs off-ramp | **Claude Code** |
| Restraint budget: 1 gradient, 0 blur, fade-up only | **Linear**, **Claude Code** (measured) |
| Lowercase engineer-voice CTAs | **Warp** |
| Credibility via numbers, not logos (honesty guardrail) | **Aider** (and Vex's own verifier-gate brand) |

---

## Handoff

`MASTER_BUILD_PROMPT.md` turns this plan into a single ready-to-run build prompt:
exact section-by-section spec, the locked `:root` token block verbatim, real Vex
copy and numbers, responsive/performance/accessibility/SEO requirements, the
motion budget, deployment, a DO-NOT list, and a Definition of Done checklist.
Both files are standalone.
