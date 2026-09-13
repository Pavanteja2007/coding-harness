# DESIGN.md — **THE FORGE**
### Design language for the Vex website · v2 · supersedes `DESIGN-v1-backup.md`

> v1 was dark-violet-on-black. That is the house style of every AI-agent product on
> the internet. It is banned here. This document defines a language nobody else in
> this category is using.

---

## 0 · The idea in one line

**Vex is a forge, not a crystal ball.**

Other agents sell magic — glowing orbs, purple nebulas, "AI ✨". Vex sells the
opposite: a machined instrument that refuses to claim success until the test suite
is green. The design language is therefore **cold obsidian, machined metal, and
controlled heat** — the visual vocabulary of a precision workshop at night.
Copper is the *only* hot thing on the page, and it appears exactly where work is
happening.

Three metal states carry the whole system:

| State | Meaning in the product | Color |
|---|---|---|
| **Cold obsidian** | The ground. Everything at rest. | near-black |
| **Molten copper** | Active, working, the model is thinking | copper/ember |
| **Verdigris patina** | Aged, proven, *verified* | oxidized teal-green |

That last one is the trick. Copper oxidizes to verdigris over time — so "proven"
is literally "copper that has aged." The verifier-pass color isn't an arbitrary
green; it's what copper *becomes* when it has been around long enough to trust.
This is the concept the whole site hangs on, and it is why the palette is not
decoration.

---

## 1 · Palette — Obsidian + Molten Copper

Locked. These are the tokens. Do not add a second accent hue.

### 1.1 Ground & surfaces

| Token | Hex | Use |
|---|---|---|
| `--ink` | `#080706` | Page ground. True near-black, warm-biased. |
| `--basalt` | `#0F0D0B` | Section alternation, subtle band |
| `--slab` | `#14110F` | Panels, cards |
| `--char` | `#201A16` | Raised surface, inputs, code blocks |
| `--forge` | `#2B221C` | Hover state on raised surfaces |

Five levels of near-black. **Depth comes from these steps plus hairlines — never
from box-shadows.** A drop shadow on a dark UI reads as a smudge; a 1px hairline
reads as machined.

### 1.2 Hairlines & rules

| Token | Value | Use |
|---|---|---|
| `--rule` | `#2E2621` | Standard 1px border |
| `--rule-soft` | `rgba(197,106,62,0.10)` | Copper-tinted hairline, panels |
| `--rule-hot` | `rgba(197,106,62,0.34)` | Active/focused border |
| `--etch` | `rgba(245,240,234,0.05)` | Top-edge highlight (1px inset, fakes a bevel) |

The `--etch` inset on the top edge of a panel is what makes it read as *milled
metal* rather than a flat rectangle. Use it on every raised surface:
`box-shadow: inset 0 1px 0 var(--etch);`

### 1.3 Heat — the accent

| Token | Hex | Use |
|---|---|---|
| `--copper` | `#C56A3E` | **Primary accent.** Links, active states, key numbers |
| `--ember` | `#E8945C` | Highlights, hover, gradient top-stop |
| `--flare` | `#F4B183` | Peak heat only — tiny highlights, never fields |
| `--scorch` | `#7A3F24` | Deep copper, for fills behind text |

**Rule:** copper is expensive because it is rare. Target **≤12% of viewport pixels**
carrying any copper at rest. If a section has three copper elements, two of them
are wrong.

### 1.4 Patina — the verified state

| Token | Hex | Use |
|---|---|---|
| `--patina` | `#4FA88B` | Verified / passed / proven |
| `--patina-bright` | `#6FD4AE` | Pass checkmarks, green suite |
| `--patina-dim` | `rgba(79,168,139,0.14)` | Pass-state fills |

Only ever appears on **genuinely verified things**: a passing suite, a resumed
task, a confirmed fix. Never decorative. This is a semantic color, and the honesty
of the product depends on it staying that way.

### 1.5 Type

| Token | Hex | Use |
|---|---|---|
| `--quench` | `#F5F0EA` | Headings. Warm white, like quenched steel |
| `--ash` | `#A29488` | Body text. **4.9:1 on `--ink` — passes AA** |
| `--smoke` | `#6E645C` | Captions, labels. Use ≥14px only |
| `--soot` | `#473F39` | Disabled, decorative rules only — never text |

### 1.6 Signal

| Token | Hex | Use |
|---|---|---|
| `--warn` | `#D9A441` | Caution, honesty callouts |
| `--fail` | `#D4614E` | Failures, regressions |

⚠️ `--fail` and `--copper` are close in hue. **Never** put a failure state next to
a copper accent without a shape/icon difference. Color is never the sole signal.

### 1.7 Contrast ledger (verify these at build)

| Pair | Ratio | Verdict |
|---|---|---|
| `--quench` on `--ink` | ~17.8:1 | AAA |
| `--ash` on `--ink` | ~4.9:1 | AA body ✓ |
| `--smoke` on `--ink` | ~2.9:1 | **Large/decorative only** |
| `--copper` on `--ink` | ~4.6:1 | AA large / UI ✓ — **not for small body text** |
| `--ember` on `--ink` | ~7.1:1 | AA all sizes ✓ |
| `--patina-bright` on `--ink` | ~9.4:1 | AAA ✓ |
| `--quench` on `--copper` | ~3.9:1 | **Fails AA.** Use `--ink` on copper fills |

**Consequence:** copper CTA buttons take **`--ink` text, not white.** Dark text on
copper is also more "machined" and less "candy."

### 1.8 Light mode

**There isn't one.** A forge is dark. Committing to a single register is itself a
premium signal — Linear, Vercel, and Zed all ship marketing sites that pick one.
Docs pages may offer light later; the marketing site does not.

---

## 2 · Typography

Three families, each doing one job. Verify availability at build; fall back as noted.

### 2.1 The stack

| Role | Family | Source | Fallback |
|---|---|---|---|
| **Display** | **Fraunces** (variable: `opsz`, `SOFT`, `WONK`) | Google Fonts | `Instrument Serif`, Georgia, serif |
| **UI / body** | **Archivo** (variable) | Google Fonts | `Inter`, system-ui, sans-serif |
| **Mono** | **JetBrains Mono** | Google Fonts | `ui-monospace`, `SFMono-Regular`, monospace |

**Why Fraunces.** It is a variable display serif with a `WONK` axis that lets
letterforms get slightly strange at large sizes and normalize at small ones. Set
at `opsz 120, WONK 1, SOFT 20`, it reads chiselled and warm — struck metal, not a
wedding invitation. It is the single most important anti-slop decision on this
page: **nobody in dev tooling uses a display serif**, and it is what will make the
site read as editorial and expensive rather than as another SaaS template.

**Why Archivo.** Grotesk rooted in industrial/highway signage. Sturdy, slightly
condensed, with a real bold. Reads *machined*. Inter is the default AI-slop sans —
using it would undo the serif's work.

**Why JetBrains Mono.** The audience writes code in it. It carries authenticity
that a "designer mono" cannot.

### 2.2 Usage law

- **Fraunces is for statements only.** Page `h1`, section `h2`, pull-quotes, the
  big number in a stat. **Never** for body copy, nav, buttons, labels, or UI.
  Target: ≤2 Fraunces elements visible at any one scroll position.
- **Archivo carries everything else.**
- **JetBrains Mono** for: commands, code, file paths, eyebrows/kickers, table
  numerals, badges, metrics.
- **Numerals:** always `font-variant-numeric: tabular-nums` in tables and stats.

### 2.3 Scale

Fluid, `clamp()`-based. Ratio ~1.32 (major third-ish, tightened at the top).

| Step | Size | Family | Tracking | Leading |
|---|---|---|---|---|
| `display` | `clamp(3.25rem, 7.5vw, 7rem)` | Fraunces | `-0.035em` | `0.94` |
| `h1` | `clamp(2.75rem, 5.5vw, 4.75rem)` | Fraunces | `-0.03em` | `1.0` |
| `h2` | `clamp(2rem, 3.6vw, 3.25rem)` | Fraunces | `-0.025em` | `1.06` |
| `h3` | `clamp(1.35rem, 1.9vw, 1.75rem)` | Archivo 600 | `-0.015em` | `1.2` |
| `lead` | `clamp(1.125rem, 1.5vw, 1.375rem)` | Archivo 400 | `-0.008em` | `1.55` |
| `body` | `1rem` | Archivo 400 | `-0.005em` | `1.65` |
| `small` | `0.875rem` | Archivo 400 | `0` | `1.55` |
| `mono-lg` | `0.9375rem` | JetBrains Mono | `-0.01em` | `1.7` |
| `mono` | `0.8125rem` | JetBrains Mono | `0` | `1.75` |
| `eyebrow` | `0.75rem` | JetBrains Mono 500 | `0.18em` UPPER | `1` |

**Optical tracking rule:** tracking tightens as size grows — but do not over-tighten
below ~`-0.035em`; letters start colliding at display weights. Small mono text gets
`0` or slightly positive tracking, never negative.

### 2.4 Measure

Body copy: **62–72ch**. Lead paragraphs: **50–58ch**. Never full-bleed text.

---

## 3 · The hero background — domain-warped fBm

The signature element. Gets its own section because it is the first impression.

### 3.1 What it is

A single fullscreen quad running a custom GLSL fragment shader. Fractional
Brownian motion (fBm) noise, **domain-warped** — the noise field is sampled at
coordinates that are themselves offset by another noise field, twice. This is
Iñigo Quílez's warping technique and it produces slow, folding, liquid-metal
structure that no CSS gradient can imitate.

```
q = fbm(p + t*0.06)
r = fbm(p + q*1.7 + t*0.04)
color = mix(ink, copper, smoothstep(...)) modulated by r
```

### 3.2 Why it is the right call

- **Bespoke by nature.** It is ~120 lines of math we wrote. It cannot look like a
  template because there is no template.
- **It is the product's metaphor, moving.** Molten metal folding in darkness.
- **It is cheap.** One draw call, one quad, no geometry.

### 3.3 Implementation — use `ogl`, NOT three.js

This is measured, not opinion. For an identical fullscreen-shader workload:

| Approach | Bundle (gzip) |
|---|---|
| Raw WebGL | ~0.6 kB |
| **`ogl`** | **~12.8 kB** ← use this |
| `three` (best-case tree-shaken) | ~130 kB |
| `@react-three/fiber` + three | ~241 kB |

three.js cannot tree-shake below ~130 kB for this job because `WebGLRenderer`
statically pulls in `ShaderLib`/`ShaderChunk` — every built-in material's GLSL —
and it is indivisible. Worse, **R3F does `import * as THREE`, which defeats
three's tree-shaking entirely.** We are drawing one quad. We do not need a scene
graph. **`ogl` is a 10× saving for zero loss.**

### 3.4 Craft rules — what separates expensive from cheap

1. **Slow.** Full cycle **40–60s**. If a viewer can perceive the loop, it's cheap.
2. **Desaturated.** The shader outputs mostly `--ink`→`--scorch`. `--copper`
   appears only at the crests. **Never** let it hit `--flare` across a field.
3. **Grain is mandatory.** An animated film-grain overlay at **3–5% opacity**,
   preferably regenerated per frame. Grain is the single highest-leverage
   "expensive" signal — it kills the plastic CGI look instantly.
4. **Vignette.** Radial darkening to `--ink` at the edges so the shader never
   fights the nav or the headline.
5. **Contrast floor.** Text sits on a `--ink` scrim gradient. The headline must
   clear **4.5:1 against the brightest possible shader frame**, not the average.
6. **Never full-viewport-bright.** The shader lives in the upper ~70vh and fades.

### 3.5 Performance & fallback ladder

| Condition | Behavior |
|---|---|
| Desktop, WebGL2 | Full shader, DPR capped at **1.5**, 60fps |
| Mobile / low-power | Half-resolution render target, upscaled; DPR cap **1.0** |
| `prefers-reduced-motion` | **Freeze at a composed still frame.** Not removed — frozen |
| No WebGL / context lost | Static pre-rendered WebP + subtle CSS gradient drift |
| Tab hidden | `cancelAnimationFrame` — never burn battery offscreen |
| Scrolled past hero | Pause the RAF loop |

Reduced-motion freezing (rather than hiding) matters: the user still gets the
composition, just not the movement.

---

## 4 · Motion system

### 4.1 Easing — the house curves

| Name | Curve | Use |
|---|---|---|
| `--ease-forge` | `cubic-bezier(0.22, 1, 0.36, 1)` | **Default.** Entrances, reveals |
| `--ease-strike` | `cubic-bezier(0.65, 0, 0.35, 1)` | Toggles, tabs, state swaps |
| `--ease-draw` | `cubic-bezier(0.16, 1, 0.3, 1)` | Long scroll-linked draws |
| `--ease-quench` | `cubic-bezier(0.34, 1.28, 0.64, 1)` | Rare, tiny overshoot. Badges only |

**Never `ease-in-out` on entrances.** It is the single most common tell of an
un-art-directed site. Things enter fast and settle slow.

### 4.2 Duration

| Class | Duration |
|---|---|
| Micro (hover, focus, button) | **120–180ms** |
| Standard (reveal, fade, tab) | **320–420ms** |
| Deliberate (section, hero stagger) | **600–900ms** |
| Ambient (shader, marquee) | **40–60s loop** |

Stagger between siblings: **40–70ms**. More than ~90ms reads as sluggish.

### 4.3 Rules

- Animate **`transform` and `opacity` only**. Never `width`, `height`, `top`,
  `left`, `filter` on scroll.
- **One** scroll-linked "hero moment" per page. Two is a carnival.
- Reveals fire **once** (`unobserve` after) — re-animating on scroll-back is cheap.
- Reveal distance: **12–20px**. Not 60px. Small movement reads expensive.
- Every animation resolves to its final state under `prefers-reduced-motion`.

---

## 5 · The AI-slop ban list

Any of these appearing in a build is a defect. Non-negotiable.

### Color & surface
- ❌ Purple→blue 45° gradients **(this is what v1 did — the origin of the rebuild)**
- ❌ Gradient text on headings. Headings are `--quench`. One copper word maximum, flat
- ❌ Glassmorphism / `backdrop-blur` cards. **Nav bar only**, and subtly
- ❌ Glowing orbs or blurred blobs behind sections. Zero. The shader is the only ambience
- ❌ Neon cyan-on-black terminal
- ❌ `box-shadow` for elevation → use hairlines + `--etch`

### Layout
- ❌ Bento grid of 6 equal feature cards
- ❌ Everything centered, every section the same rhythm
- ❌ Three-column "features" row with an icon above each heading
- ❌ The faint dot-grid or graph-paper background
- ❌ A pill badge above the hero reading "✨ Introducing…"

### Type
- ❌ Inter for everything
- ❌ Emoji as UI icons — **SVG only** (Lucide, or hand-drawn)
- ❌ All-caps everything, or no caps anywhere

### Motion
- ❌ Spotlight-follows-cursor on every card
- ❌ Scale-transform hover that shifts layout
- ❌ Everything fading up 60px with the same 500ms `ease-in-out`
- ❌ Typewriter effect on the main headline
- ❌ Floating 3D geometric shapes

### Content
- ❌ Fake logos / "Trusted by" strips. **We have no customers. Say nothing.**
- ❌ Invented testimonials, quotes, star counts, user numbers
- ❌ SWE-bench scores (not run — deferred)
- ❌ Presenting proxy-rate costs as bills
- ❌ A `pip install vex` one-liner if no PyPI package exists

### The instead-of list

| Instead of | Do |
|---|---|
| Gradient heading | Flat `--quench`, one copper word |
| Glass card | `--slab` + 1px `--rule` + `--etch` inset |
| Glow orb | The shader, and nothing else |
| Bento grid | Asymmetric editorial rows, alternating weight |
| Icon-above-heading trio | A real artifact: terminal, diff, table, graph |
| Logo strip | Reproducible numbers + the repos actually fixed |
| Dot-grid bg | Flat `--ink`; let the type breathe |

---

## 6 · Layout & grid

- **Container:** `1200px` content, `1400px` wide (tables, mockups), `100vw` bleed
- **Columns:** 12, `24px` gutter desktop / `16px` mobile
- **Section rhythm:** `clamp(96px, 14vh, 200px)` vertical — **vary it.** Dense
  sections get less; the hero and closing get more. Uniform rhythm reads templatey.
- **Asymmetry is the default.** Alternate 7/5 and 5/7 splits. Full-width centered
  blocks are reserved for genuine statements (hero, closing CTA) — 3 max per page.
- **Breakpoints:** `1280` / `1024` / `768` / `520` / `380`

### Radii
`--r-sm: 6px` (badges) · `--r-md: 10px` (buttons, inputs) · `--r-lg: 14px` (panels)
· `--r-xl: 18px` (major surfaces). **Nothing is a pill except badges.** Big
border-radii read cheap.

---

## 7 · Component inventory

The site needs a real system, not a page. Build these as typed, reusable components.

### 7.1 Primitives (12)
`Button` (primary/secondary/ghost/copper, 3 sizes) · `Badge` · `Pill` · `Card` ·
`Panel` · `Hairline` · `Kicker/Eyebrow` · `SectionHeader` · `Prose` · `Link` ·
`Icon` (Lucide wrapper) · `VisuallyHidden`

### 7.2 Product-specific (14)
`Terminal` (chrome, typing, step states, copy) · `CommandLine` (copyable, `$`) ·
`InstallTabs` (OS/manager tabs, WAI-ARIA) · `DiffBlock` (+/- gutters) ·
`RationaleCard` · `RouterTable` (sortable, tabular numerals) · `StatTile`
(count-up) · `SessionBoard` (kanban, resume animation) · `LoopDiagram` (gate
opens) · `LayerStack` (four layers) · `SandboxFlags` (chip row) · `MCPToolList` ·
`RepoRow` (multi-repo results) · `HonestyNote` (the caveat callout)

### 7.3 Motion (10)
`ShaderHero` (ogl) · `GrainOverlay` · `Reveal` (IO wrapper) · `StaggerGroup` ·
`SplitText` (char/word/line) · `CountUp` · `ScrambleText` (decrypt — **hover/eyebrow
only, not the h1**) · `MagneticButton` (subtle, ≤6px) · `ScrollProgress` ·
`Marquee` (slow, for the stack strip)

### 7.4 Navigation & chrome (10)
`Nav` (sticky, the one glass surface) · `MobileNav` (full-screen sheet) ·
`Footer` · `DocsSidebar` · `DocsTOC` (on-this-page, scroll-spy) · `DocsSearch`
(⌘K) · `Breadcrumb` · `PrevNext` · `SkipLink` · `ThemeMark` (favicon/logo)

### 7.5 Content (8)
`MDXComponents` · `CodeBlock` (highlight, copy, filename, line-highlight) ·
`CodeTabs` · `Callout` (note/warn/honest) · `Table` · `Steps` · `Accordion`
(WAI-ARIA) · `ChangelogEntry`

**~54 components.** That is what "many components" actually means.

---

## 8 · Iconography

- **Lucide React**, `1.5px` stroke, `20px`/`24px` only. Never emoji.
- Icons are `--smoke` at rest, `--copper` when the element is active.
- Never an icon above a heading in a 3-up row (see ban list).
- Product diagrams are **hand-authored SVG**, not icon-fonts.

---

## 9 · Accessibility (non-negotiable)

- **Focus:** `2px solid var(--ember)` + `2px` offset. Visible on every interactive
  element. Never `outline: none` without a replacement.
- **Targets:** ≥`44×44px` on touch.
- **Landmarks:** one `<h1>`, proper heading order, `<nav>`/`<main>`/`<footer>`,
  skip-link first in tab order.
- **Keyboard:** tabs (arrows/Home/End), accordion, ⌘K search, mobile nav (Esc
  closes + focus returns), focus trap in the mobile sheet.
- **Motion:** every animation resolves to final state under `prefers-reduced-motion`.
  The shader freezes rather than disappears.
- **Color is never the sole signal** — pair with icon, shape, or text.
- **Contrast:** verify the §1.7 ledger with a real checker at build.

---

## 10 · Performance budget

| Metric | Budget |
|---|---|
| LCP | < 2.0s |
| CLS | < 0.05 |
| INP | < 200ms |
| JS (landing, gzip) | **< 180 kB** incl. shader |
| Shader | 1 draw call, DPR ≤1.5, paused offscreen |
| Fonts | `next/font`, `display: swap`, preload display + body only |

Mono loads only where code appears. Fraunces subsets to Latin. No font file over
~40 kB.

---

## 11 · Voice

- **Declarative, not salesy.** "Fix real bugs. Verified, not vibed." not
  "Supercharge your workflow with AI ✨".
- **Numbers over adjectives.** Never "blazing fast" — give the measurement and
  link the log.
- **Honest by construction.** Every number carries its `n`. Caveats ship *with* the
  claim, not in a footer nobody reads. The honesty note is a designed component
  (`HonestyNote`), not fine print.
- **Lowercase `vex`** as the wordmark. Sentence case for headings. No Title Case.

---

## 12 · Reference standard

Aiming at the craft level of: **Linear** (restraint, type), **Vercel** (dark
discipline), **Zed** (density without clutter), **Astral/uv** (OSS-CLI honesty),
**Resend** (editorial warmth), **Rive/Awwwards winners** (motion choreography).

Aiming *away* from: every "AI agent" landing page shipped in the last 18 months.

---

*The palette, the fonts, the ban list, and the ogl decision are locked. Everything
else is craft judgment — exercise it.*
