# Vex — Master Build Prompt (`MASTER_BUILD_PROMPT.md`)

> **What this is.** A complete, ready-to-run prompt for a **fresh session** to
> build the Vex landing page in one pass, without back-and-forth. It is
> self-contained: paste it (with the repo available) and build. The design
> rationale and research behind every decision live in `DESIGN.md`; this file is
> the executable spec.
>
> **Prerequisite context to load first:** `README.md` and `RESULTS.md` (real
> numbers), `INTERFACES.md` (CLI identity: the command is `vex`), and `DESIGN.md`
> (this page's design system). All product numbers below are copied from those
> files — **do not invent new ones.**

---

## 0. Mission

Build a **premium, production-quality landing page for Vex** — a CLI-first AI
coding-agent harness that fixes real software bugs end-to-end. It must look and
feel like a real, top-tier developer-tool product page (calibre of Linear, Warp,
Claude Code, Aider), grounded in Vex's *actual* capabilities and *actual, honest*
numbers. The emotional core: **Vex only claims success when the tests agree**,
and it does so at ~40% of the naive cost.

**Do NOT** ship a generic SaaS template, a hype-video hero, a text-heavy wall, or
any fabricated social proof. Restraint and reproducible numbers are the brand.

---

## 1. Tech & build constraints

- **Stack:** a single self-contained static site — **hand-authored
  `index.html` + one `styles.css` + one `main.js`** (no framework, no build
  step). This maximizes portability, reviewability, and zero-config deploy.
  Tailwind via CDN is acceptable *only if* you still centralize the tokens as CSS
  custom properties; hand-rolled CSS is preferred.
- **No external runtime dependencies** beyond Google Fonts (Inter, JetBrains
  Mono, and optionally Instrument Serif) and inline SVG icons. No jQuery, no
  animation libraries — the motion budget is small enough for vanilla JS +
  IntersectionObserver.
- **Icons:** inline SVG only (Lucide or Heroicons paths). **Never emoji as UI
  icons.**
- **Single accent image budget:** no stock photos. All "product shots" are
  **DOM-built mockups** (terminal, session board, diff) — not screenshots, not
  video.
- **File size:** keep total page weight lean; fonts subset where possible; no
  asset over what's needed for a fast first paint.

---

## 2. Locked color tokens — paste verbatim into `:root`

The palette is **non-negotiable** (dark-violet-and-black). Use these exact token
names and hex values. (Provenance: `DESIGN.md` §3 — proposed, WCAG-checked system
standing in for the never-found `VEX_DESIGN_SYSTEM.md`; swappable 1:1 if it
surfaces.)

```css
:root{
  /* Backgrounds */
  --bg:#0A0711; --surface:#14101E; --surface-2:#1B1526;
  --hairline:#2A2136; --hairline-soft:rgba(196,181,253,0.08);
  /* Violet ramp */
  --violet-300:#C4B5FD; --violet-400:#A78BFA; --violet-500:#8B5CF6;
  --violet-600:#7C3AED; --violet-700:#6D28D9;
  /* Text */
  --text:#F4F1FB; --text-muted:#A79FB8; --text-faint:#6E667E;
  /* Functional (sparingly; green is the ONLY non-violet accent) */
  --ok:#4ADE80; --warn:#FBBF24; --danger:#F87171;
  /* Signature */
  --cta:linear-gradient(135deg,var(--violet-500),var(--violet-600));
  --focus-ring:var(--violet-400); --orb:var(--violet-700);
}
```

**Color rules (enforce):**
1. `--bg` page ground; depth via `--surface`→`--surface-2` + `--hairline`, **not
   shadows**.
2. Body text `--text`; muted `--text-muted`; `--text-faint` for meta/large only —
   **never body**.
3. Links / small accents on dark = `--violet-400` or `--violet-300` (never
   `--violet-500` for small text).
4. Primary CTA = `--cta` gradient, **pure `#FFFFFF` label, ≥16px, semibold**.
5. `--ok` green **only** for verifier PASS states — it's the payload, not decor.
6. **Exactly one** blurred `--orb` radial per major section, low opacity. No orb
   fields, no glassmorphism except the sticky nav.

---

## 3. Typography setup

```
Display/headings + body:  Inter (variable)   — weight 400/450 body, 520–560 headings, tracking −0.02em on large
Monospace:                JetBrains Mono      — terminal, code, diffs, table numerals, uppercase eyebrows (0.8125rem, +0.08em, uppercase)
Serif accent (optional):  Instrument Serif    — ONLY the wordmark lockup or one pull-quote
```

- Load via `<link>` with `display=swap`; give every family a real fallback stack
  (`Inter, system-ui, sans-serif`; `"JetBrains Mono", ui-monospace, monospace`).
- **Weight discipline:** headings never heavier than ~560; premium = tight
  tracking on medium weight, not bold.
- **Type scale (fluid `clamp()`):** hero 3.5–4.5rem · H2 2.25–2.75 · H3 1.5 ·
  body-lg 1.125 · body 1.0 · mono-label 0.8125 · caption 0.875. Body line-height
  1.6; large-display line-height 1.0–1.1.

---

## 4. Layout & spacing

- 8px base grid. Centered content column **max-width 1120px**; full-bleed product
  panels may widen to ~1320px. Side gutter ≥24px desktop, ≥16px mobile (set once
  on the wrapper; vertical padding via `padding-block`).
- Section padding-block `clamp(80px, 12vh, 160px)`.
- Radii: buttons/inputs **8px**, cards/panels **14px**, terminal **12px**, pills
  full-round.

---

## 5. Section-by-section build spec

Build these **15 sections in order**. Each feature block *shows a DOM mockup*, not
prose. Copy below is real — use it (light editing for flow is fine; numbers are
fixed).

### §1 — Sticky nav
Glass (`backdrop-filter: blur(12px)`, `--bg` @ ~72%, bottom `--hairline`). Left:
wordmark **`vex`** (mono or serif-accent lockup). Center/right links: **How it
works · Architecture · Benchmarks · Memory · Docs · GitHub**. Primary button
**"get started"**. Collapses to a menu button under 820px. This is the **only**
glass element on the page.

### §2 — Hero
- **Eyebrow (mono, uppercase):** `CLI-FIRST · VERIFIER-GATED · OPEN`
- **Headline (recommended):** **"Fix real bugs. Verified, not vibed."**
  Alternates: "The coding agent that proves it fixed the bug." / "Autonomous
  bug-fixing, gated by your test suite."
- **Subhead:** "Vex is a CLI-first coding agent: a Docker-sandboxed
  planner–executor–verifier loop, a crash-proof concurrent runtime, a persistent
  memory layer over MCP, and adaptive model routing that hits the same success
  rate at ~40% of the cost."
- **Install line (copyable, mono, Copy button):** show the **real** quickstart —
  `python demo/run_demo.py` (zero-setup offline demo) as the primary copyable
  line, with a secondary real path `pip install -e .  ·  vex fix --repo . --issue "…"`.
  ⚠️ **Do not invent a PyPI one-liner** (`pip install vex`) unless the package
  actually exists — use the repo's real commands or clearly mark any placeholder.
- **CTAs:** primary **"get started"** (→ §13), secondary **"read the docs"** (→ README).
- **Hero artifact — animated terminal** (`--surface`, window dots, JetBrains Mono),
  steps appear in sequence then the gate flips to `--ok` green:
```
$ vex fix --repo ./myproject --issue "mean() returns sum instead of average"
◇ planning… 3 steps
◇ step 1/3  read src/mathutil.py           [sandbox · read-only · no-net · cap-drop ALL]
◇ step 2/3  patch mean(): sum → sum/len
◇ step 3/3  verify…
   ✓ tests/test_mathutil.py::test_mean  PASSED
   ✓ full suite: 214 passed, 0 regressions     ← verifier gate
✓ fix verified   router: 4 cheap · 1 hard   cost $0.021   branch vex/fix-mean
```
- One blurred violet `--orb` behind the hero. Under reduced-motion, terminal
  renders fully immediately.

### §3 — Stat band
Four mono tiles, count-up on scroll, each with a source tie:
- **~39%** — of baseline model cost, same success (n=16)\*
- **100%** — verifier-gated success rate (n=16 tasks)\*
- **45 / 45** — tasks @ concurrency 45 with 8 mid-run kills → 8/8 resumes
- **7** — real OSS repos fixed end-to-end
`\*` links to the honesty footnote (§10).

### §4 — Thesis + the four layers
Thesis line: **"The integration is the point."** One paragraph: Vex is not one
model call — it's four layers built to work as one system. Then a **labeled
architecture matrix** (four hairline panels, mono eyebrow each):
- **Harness** — planner · step agent · verifier gate · git-native output
- **Execution** — Docker sandbox · stateless verify · flake detection
- **Runtime** — concurrent scheduler · checkpoint/resume · adaptive router
- **Memory + MCP** — tree-sitter code graph · decision memory · 5 MCP tools

Hover raises a panel's border to `--violet-400`. Optional: clicking a panel scrolls
to its deep-dive section (§6–§8, §11).

### §5 — How it works (the loop)
The centerpiece, shown not told. A stepped/animated horizontal (vertical on
mobile) flow: **Plan → Step (in sandbox) → Verify (gate) → Git-native output.**
Show the gate visibly *gating*: the "Verify" node holds until PASS, only then does
"Git output" (branch + commit + PR + `rationale.md`) unlock in `--ok` green. One
line under it: "On verified fixes, Vex writes a branch, commit, PR description, a
human-readable `rationale.md`, and a full trace of every prompt/response/tool
call."

### §6 — Layer 1 · Harness (alternating block: text left / mockup right)
Copy: "A planner decomposes the fix into small, verifiable steps; a step agent
executes them; a **verifier gate** decides completion. The original repo is
snapshotted and never touched — you get a clean diff. Every run resumes from
completed steps via a resume contract." Mockup: a `rationale.md` card + a unified
diff snippet (violet syntax tint, `--ok` for additions).

### §7 — Layer 2 · Execution (alternating: mockup left / text right)
Copy: "Every command runs in a **fresh Docker container** — read-only rootfs, **no
network**, resource limits, **`cap-drop ALL`**. Verification is stateless and
flake-aware. The sandbox was adversarially confirmed: **24/24 sequential +
concurrent attack suites.**" Mockup: sandbox spec rendered as mono chips
(`read-only-rootfs`, `--network none`, `cap-drop ALL`, `mem-limit`, `pids-limit`) +
a "flake detected → re-run" line.

### §8 — Layer 3 · Runtime + the adaptive router (elevated differentiator)
This is the novel mechanism — give it the most visual weight.
Copy: "Per call, Vex predicts difficulty — an intrinsic signal from the issue text
plus a struggle signal from the conversation tail (failing tests, burned turns) —
and routes **easy/medium → a cheap model, hard → an expensive one.** Every call
lands in a per-task JSONL ledger. The mechanism is measurable, not asserted."
Include the **model-route/cost table** (mono, right-aligned numerals; highlight the
adaptive win):

| run | arm | success | calls | tokens | cost\* | wall |
|---|---|---|---:|---:|---:|---:|
| 5 fixture bugs | always-expensive | 5/5 | 17 | 38,680 | $0.0528 | 575s |
| 5 fixture bugs | **adaptive** | 5/5 | 31 | 69,615 | **$0.0237** | **300s** |
| 16-task set | always-expensive | 16/16 | 81 | 138,526 | $0.1505 | 2717s |
| 16-task set | **adaptive** | 16/16 | 71 | 136,436 | **$0.0581** | **812s** |
| 5 real OSS repos | always-expensive | 2/5 | 71 | 329,438 | $0.3059 | 2992s |
| 5 real OSS repos | **adaptive** | 3/5 | 75 | 302,801 | **$0.0730** | **581s** |

Callout: **"Same 100% success at ~45% (n=5) / ~39% (n=16) of baseline cost."**
Also mention the scheduler (proven 10–50 concurrent), checkpoint/resume, and the
approval gate here.

### §9 — Runtime reliability (proof, as a live session board)
Copy: "**45 tasks at concurrency 45, with 8 simultaneous mid-run hard kills →
45/45 success, 8/8 genuine resumes, zero leaked containers** — verified from the
event journal." Mockup: a **session-board** (Devin-style) with columns **Running ·
Verifying · Passed**, rows showing per-task route badges (`cheap`/`hard`) and a
`resumed ✓` badge on the killed-and-recovered ones. Animate 8 rows crossing into
Passed once.

### §10 — The numbers (ablation) + honesty footnote
Re-present the §8 table framed as a **reproducible benchmark** with artifact
paths (`logs/ablations/v2-heuristic-*`, `logs/ablations/v4`) and a **"reproduce
this"** pointer: `python -m runtime.ablation --tasks all --concurrency 2`.
**Honesty footnote (required, verbatim-ish):**
> \* Costs use proxy price rates for comparable model classes on free-tier BYO
> endpoints (both report \$0) — the delta is a price-model delta, not a bill. n=5
> and n=16 runs are directional, not benchmark-grade. SWE-bench numbers are
> deferred to Phase 6.

### §11 — Layer 4 · Memory + MCP (alternating block)
Copy: "A **tree-sitter code graph** (functions, classes, calls, imports) and a
**SQLite decision memory** that auto-ingests every task's structured state. Vex
exposes it over an **MCP server with 5 tools** — `query_structure`,
`query_decisions`, `record_decision`, `task_status`, `list_repos` — so any MCP
client (Claude Code, Cursor, …) can query it. Vex is also an MCP *client*."
Mockup: a mono block —
```
$ vex mcp call "python -m mcp_server" query_decisions --args '{"query":"pytest"}'
→ [{ "decision": "pin dev-only deps per-repo", "task": "arrow#fix-parse", … }]
```

### §12 — Multi-repo validation (momentum)
Copy: "Beyond its home fixtures, the full stack has fixed real, unfamiliar OSS
code." Dated-log style list: **jaraco/path** (full DoD end-to-end, verified in 1
attempt, $0.053, 6 calls, git + rationale + approval) · **python-semver** (module
DoD, 15/15 checks) · **more-itertools, arrow, inflect, boltons** (routing ablation,
Round 6). One honest line: "including one first-failure that exposed and fixed a
real harness bug — the verifier refused to claim success."

### §13 — Install / Get started (tabbed, copyable)
Tabs, each with a Copy button (use **real** commands from README):
- **Offline demo (zero setup):** `python demo/run_demo.py`
- **Fix a real bug (BYO key):** `pip install -e .` then
  `vex fix --repo <path> --issue "<bug report>" --provider openai --model <model> --api-key $MY_KEY --api-base <url>`
- **Query memory over MCP:** `vex mcp call "python -m mcp_server" query_decisions --args '{"query":"pytest"}'`
- **Dashboard:** `vex dashboard --logs-dir logs/…`
Below the tabs, a compact command list: `vex fix` · `vex run-benchmark` ·
`vex status` · `vex dashboard` · `vex memory query-decisions` · `vex mcp call`.
(Note: README still shows legacy `harness`/`python -m cli`; use `vex` per
`INTERFACES.md`.)

### §14 — Honest by design + stack
- **Honesty statement (feature it):** "Vex reports `success` only when your target
  test passes **and** the full suite shows no regressions. No green checkmark
  without a green suite."
- **Stack badges (mono chips):** Python 3.10 · litellm (BYO-key, multi-provider) ·
  Docker · tree-sitter · MCP (official Python SDK) · argparse CLI · stdlib
  dashboard · **~300 tests** · **CI: Linux/macOS/Windows × 3.10/3.12** · **v0.1.0**.
- **Adversarial note:** "Probed with path-traversal, shell/SQL injection, null
  bytes; one real leak found, fixed, and pinned by 101 adversarial tests."
- **No logos. No testimonials.** (See DO-NOT list.)

### §15 — Docs off-ramp + closing CTA + footer
- **"Get the technical rundown"** — cards linking to `README.md`, `INTERFACES.md`,
  `docs/architecture-harness.md`, `CHANGELOG.md`.
- **Closing CTA (dual):** "fix your first bug" (→ §13) / "read the docs" (→ README),
  on a `--surface` panel with one violet orb.
- **Footer:** multi-column link map (Product / Architecture / Docs / GitHub),
  hairline top, wordmark, "v0.1.0", license note.

---

## 6. Responsive requirements
- Works at **375 / 768 / 1024 / 1440px** and down to ~360px with no horizontal
  scroll on `body`.
- Alternating blocks stack to one column under ~820px (text above mockup).
- The benchmark table and any wide mockups get their own `overflow-x:auto`
  wrapper; the page body never scrolls sideways.
- Tap targets ≥44×44px; nav collapses to a menu; type stays ≥16px for body on
  mobile.

## 7. Performance requirements
- First paint fast: inline critical CSS or keep `styles.css` small; fonts
  `display=swap` with fallbacks; **no layout libraries**.
- Animate only `transform`/`opacity`. IntersectionObserver for reveals (no
  scroll-jank).
- Reserve space for animated/mockup content to avoid layout shift (CLS ~0).
- Lazy-init the terminal/session-board animations when they scroll into view.
- Target Lighthouse ≥95 Performance / ≥95 Best Practices / 100 Accessibility on
  desktop.

## 8. Accessibility requirements
- All interactive elements keyboard-reachable; **visible focus ring**
  (`--focus-ring`, 2px + 2px offset). Tab order matches visual order.
- Verify contrast: body text ≥4.5:1, large/UI ≥3:1. (Provided tokens are checked;
  re-verify any new pairs — especially violet-on-dark for small text.)
- Icon-only buttons get `aria-label`; the Copy buttons announce "Copied".
- Respect **`prefers-reduced-motion`**: disable reveals, terminal typing,
  count-ups, and orb drift; render final states.
- Color is never the only signal (PASS shows ✓ + text, not just green).
- Semantic landmarks (`<nav> <main> <section> <footer>`), one `<h1>`, logical
  heading order, `alt` on any meaningful SVG (or `aria-hidden` on decorative).

## 9. SEO & social-share meta (in `<head>`)
```html
<title>Vex — the verifier-gated coding agent</title>
<meta name="description" content="Vex is a CLI-first AI coding agent that fixes real bugs end-to-end: a Docker-sandboxed plan→execute→verify loop, a crash-proof concurrent runtime, MCP memory, and adaptive model routing at ~40% of the cost.">
<meta property="og:type" content="website">
<meta property="og:title" content="Vex — fix real bugs, verified not vibed">
<meta property="og:description" content="Verifier-gated autonomous bug-fixing. Same success at ~40% of the cost via adaptive model routing.">
<meta property="og:image" content="/og-image.png"><!-- 1200×630; dark-violet, wordmark + one stat. Generate or note as TODO. -->
<meta name="twitter:card" content="summary_large_image">
<meta name="theme-color" content="#0A0711">
<link rel="canonical" href="…">
```
Also: favicon (violet mark), `lang="en"`, viewport meta, and JSON-LD
`SoftwareApplication` is a nice-to-have.

## 10. Motion budget (strict — see DESIGN.md §6)
Allowed: fade/slide-up reveals (12–20px, 300–500ms, stagger ≤80ms); hero terminal
step animation; stat count-up; session-board rows advancing once; hover =
color/opacity/border only; one orb drifting ≥20s. **Forbidden:** parallax,
multiple/swarming orbs, glassmorphism beyond the nav, decorative blur, hype video,
layout-shifting hover scale. All gated by `prefers-reduced-motion`.

---

## 11. DO-NOT list (hard constraints)
- ❌ **Do not** change the color tokens (§2) — palette is locked.
- ❌ **Do not** fabricate customer logos, testimonials, quotes, star counts, or
  "trusted by N" claims. Vex has none — credibility is numbers + honesty.
- ❌ **Do not** cite SWE-bench scores (deferred to Phase 6).
- ❌ **Do not** present proxy-rate costs as bills or n=5/16 as benchmark-grade —
  the honesty footnote (§10) must appear with the numbers.
- ❌ **Do not** invent a `pip install vex` PyPI one-liner unless it truly exists;
  use the repo's real quickstart.
- ❌ **Do not** use emoji as UI icons, stock photos, screenshots, or video.
- ❌ **Do not** exceed the motion budget or add glassmorphism outside the nav.
- ❌ **Do not** use `--violet-500` for small body text (contrast); use `--violet-400/300`.

---

## 12. Definition of Done ✅
**Content & accuracy**
- [ ] All 15 sections present, in order, each feature block backed by a DOM mockup.
- [ ] Every number matches README/RESULTS exactly; honesty footnote present with
      the ablation table and stat band.
- [ ] CLI shown as `vex`; install commands are real (no invented PyPI package).
- [ ] Zero fabricated logos/testimonials/stars.

**Design fidelity**
- [ ] Locked `:root` tokens used verbatim; no off-palette colors except `--ok`
      green for PASS states.
- [ ] Inter (medium, −0.02em) + JetBrains Mono; optional serif only on
      wordmark/pull-quote; headings ≤~560 weight.
- [ ] Depth = flat panels + hairlines (no heavy shadows); ≤1 orb per section;
      glass only on nav.

**Motion**
- [ ] Reveals, terminal, count-up, session-board animate as specified; hover is
      color/opacity/border only.
- [ ] `prefers-reduced-motion` fully honored (final states shown, no motion).

**Responsive**
- [ ] No horizontal body scroll at 360/375/768/1024/1440px; blocks stack under
      ~820px; wide table/mockups scroll inside their own container; tap targets ≥44px.

**Performance**
- [ ] Only `transform`/`opacity` animated; IntersectionObserver reveals; no CLS
      from mockups; Lighthouse ≥95 perf / 100 a11y desktop.

**Accessibility**
- [ ] Keyboard-navigable; visible `--focus-ring`; contrast verified; icon buttons
      labeled; semantic landmarks + single `<h1>`; color never the sole signal.

**Meta & deploy**
- [ ] `<title>`, description, OG/Twitter tags, `theme-color`, favicon, viewport,
      `lang` all set; OG image present or flagged TODO.
- [ ] Builds/opens as a static site with **no build step**; deployable to GitHub
      Pages / Netlify / any static host by dropping the files.

---

*Design rationale, research citations, and the full reference map: see
`DESIGN.md`. Product source of truth: `README.md`, `RESULTS.md`, `INTERFACES.md`.*
