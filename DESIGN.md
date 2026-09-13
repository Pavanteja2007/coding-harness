# DESIGN.md — **OXBLOOD**

### Design language for the Vex website · v3 · supersedes `DESIGN-v2-forge-backup.md`

> **This document describes what is built.** Two earlier versions described
> things that no longer exist — a copper "Forge" palette with Fraunces and a
> WebGL shader hero, then an assay-gold pass. Both are archived. Every token,
> ratio and component named here was read out of the code, and every contrast
> figure was measured rather than estimated.
>
> Where the old document was still right — the motion system, the ban list, the
> layout rules, the voice — those sections are carried forward, corrected where
> the build diverged.

---

## 0 · The idea in one line

**A bug is red until a test says otherwise.**

Oxblood is the colour of a deletion in a diff, of a failing assertion, of the
state a bug is in before it is fixed. Using it as the primary accent means the
palette carries the product's subject rather than decorating it. Verdant — the
green of a passing suite — is reserved for things that have *actually* been
verified, and appears nowhere else.

That pairing is the whole system. Red is the work; green is the proof; bone is
what is settled. The site is otherwise near-black and quiet, so the two
semantic colours land hard when they appear.

Practically: oxblood is also the least-used accent in developer tooling. Blue
and green are the category defaults, and gold reads decorative. Oxblood reads
serious, and nothing else in this space looks like it.

---

## 1 · Palette

Locked. Implemented as Tailwind v4 `@theme static` tokens in
`site/src/app/globals.css`.

> **`@theme static` is not optional.** Plain `@theme` tree-shakes variables it
> cannot see used in a utility class, so any token referenced only through
> `var(--color-x)` silently resolves to nothing. That shipped once: five
> colours rendered black before it was caught.

### 1.1 Ground & surfaces

| Token | Hex | Use |
|---|---|---|
| `--color-ink` | `#0A0708` | Page ground. Near-black with a faint red bias. |
| `--color-basalt` | `#0F0B0D` | Section alternation |
| `--color-slab` | `#141013` | Panels, cards |
| `--color-char` | `#1C1619` | Raised surface, code blocks, inputs |
| `--color-forge` | `#271E22` | Hover state on raised surfaces |

Five steps of near-black, each carrying a trace of red so the accent belongs to
the same family rather than sitting on neutral grey. **Depth comes from these
steps plus hairlines — never from box-shadows.**

### 1.2 Hairlines & rules

| Token | Value | Use |
|---|---|---|
| `--color-rule` | `#2E2428` | Standard 1px border |
| `--color-rule-soft` | `rgba(212, 96, 122, 0.10)` | Oxblood-tinted hairline |
| `--color-rule-hot` | `rgba(212, 96, 122, 0.30)` | Active/focused border |
| `--color-etch` | `rgba(242, 239, 234, 0.055)` | Top-edge highlight (1px inset) |

The `etch` utility (`box-shadow: inset 0 1px 0 var(--color-etch)`) is the
**only sanctioned box-shadow in the codebase**. It fakes a milled bevel on the
top edge of a raised surface.

### 1.3 Oxblood — the accent

| Token | Hex | On ink | Use |
|---|---|---|---|
| `--color-ox` | `#8C1B33` | 2.22:1 | **Fills only.** Never text. |
| `--color-ox-bright` | `#D4607A` | 5.50:1 | **The text weight.** AA at all sizes. |
| `--color-ox-pale` | `#F0A8B8` | — | Peak only, tiny highlights |
| `--color-ox-deep` | `#4A0C1A` | — | Deep fill behind text, selection |

**The single most important rule in this document:** `--ox` is a *dark* fill at
2.22:1. It is unreadable as text and must never be used as text. `--ox-bright`
is the readable weight. When the accent is needed on type, reach for
`--ox-bright`.

### 1.4 Verdant — the verified state

| Token | Hex | On ink | Use |
|---|---|---|---|
| `--color-verdant` | `#3FA07A` | 6.31:1 | Verified / passed / proven |
| `--color-verdant-bright` | `#5FCFA0` | 10.43:1 | Pass checkmarks, green suite |
| `--color-verdant-dim` | `rgba(63,160,122,0.14)` | — | Pass-state fills |

Only ever appears on **genuinely verified things**: a passing suite, a resumed
task, a confirmed fix, a verified node in the hero field. Never decorative.
This is a semantic colour, and the honesty of the product depends on it staying
that way.

### 1.5 Type

| Token | Hex | On ink | On char | Use |
|---|---|---|---|---|
| `--color-quench` | `#F2EFEA` | 17.49:1 | 15.54:1 | Headings. Warm bone. |
| `--color-ash` | `#A39B9C` | 7.38:1 | 6.56:1 | Body text |
| `--color-smoke` | `#8F888B` | 5.79:1 | 5.15:1 | Captions, labels, mono |
| `--color-soot` | `#453D40` | 1.91:1 | — | **Decorative only. NEVER text.** |

> **`--smoke` was raised twice, and the history is instructive.** It began at
> `#6E6668` (3.59:1), documented as "≥14px only". But `--text-mono` is 13px and
> `--text-eyebrow` is 12px, so every mono label using it failed AA — ten
> pairings shipped before axe caught them. A first correction to `#857E81`
> measured exactly 4.50 against `--char`: passing by nothing. The current value
> passes at the **smallest** size on the **lightest** ground it is used on.
>
> **A token that is safe everywhere beats a rule you have to remember.**
>
> `--soot` at 1.91:1 was simultaneously being used as text in 52 places. All
> were converted to `--smoke`; `--soot` now survives only as `bg-soot` on
> decorative chrome.

### 1.6 Signal

| Token | Hex | On ink | Use |
|---|---|---|---|
| `--color-warn` | `#E0A03C` | 8.85:1 | Caution, honesty callouts |
| `--color-fail` | `#E8763F` | 6.78:1 | Failures, regressions |

`--fail` is pushed **orange** deliberately. The accent is itself a red, so a
failure state rendered in a nearby red would read as decoration. Colour is
never the sole signal regardless — pair it with an icon, a glyph, or text.

### 1.7 Contrast ledger — measured, not estimated

| Pair | Ratio | Verdict |
|---|---|---|
| `quench` on `ink` | 17.49:1 | AAA |
| `ash` on `ink` | 7.38:1 | AAA |
| `smoke` on `ink` | 5.79:1 | AA at 12px ✓ |
| `smoke` on `char` | 5.15:1 | AA at 12px ✓ |
| `ox-bright` on `ink` | 5.50:1 | AA all sizes ✓ |
| `verdant-bright` on `ink` | 10.43:1 | AAA |
| **`quench` on `ox`** | **7.89:1** | **AA — this is the CTA** |
| **`ink` on `ox-bright`** | **5.50:1** | **AA — this is the CTA hover** |
| `ox` on `ink` | 2.22:1 | **Fill only. Never text.** |
| `soot` on `ink` | 1.91:1 | **Never text.** |

**Consequence — and note that it inverts the old rule.** The oxblood CTA takes
**bone text** (`text-quench`), because the fill is dark. On hover the fill
lightens to `--ox-bright` and the text flips to `--ink`. Both directions live
in one component:

```
ox: "bg-ox text-quench hover:bg-ox-bright hover:text-ink"
```

The general rule is **contrast against the fill**, not "always ink" and not
"always bone". Earlier versions of this document stated the fixed form, and
following it would have dropped the button to 2.22:1.

### 1.8 Light mode

**There isn't one.** `color-scheme: dark`, no `prefers-color-scheme` block.
Committing to a single register is itself a premium signal.

---

## 2 · Typography

### 2.1 The stack

| Role | Family | Why |
|---|---|---|
| **Display** | **Bodoni Moda** (`opsz`) | A didone — the letterform of engraved banknotes and plate lettering. Extreme stroke contrast, essentially unused in developer tooling. |
| **UI / body** | **Schibsted Grotesk** | Norwegian editorial grotesk. Sturdier and far less ubiquitous than Inter. |
| **Mono** | **Azeret Mono** | Squarer and more mechanical than JetBrains Mono, which has become the default "developer" mono everywhere. |

Loaded via `next/font/google` in `site/src/app/layout.tsx`.

**The anti-slop argument:** the previous version specified Fraunces + Archivo +
JetBrains Mono. Fraunces paired with a grotesk is now the default look of
design-conscious dev tooling — the exact thing this document exists to avoid.
Bodoni is a harder, older, less-borrowed letterform.

**Weight note:** `h1`/`h2` run at **500**, not 400. Bodoni's hairlines go
spindly on a dark ground at regular weight.

### 2.2 Usage law

- **Bodoni is for statements only** — `h1`, section `h2`, pull-quotes, the big
  number in a stat. Never body, nav, buttons, labels, or UI.
- **Schibsted carries everything else**, including `h3`–`h6`.
- **Azeret Mono** for commands, code, file paths, labels, table numerals,
  badges.
- **Numerals:** `font-variant-numeric: tabular-nums` in every table and stat.

### 2.3 Scale

Fluid, `clamp()`-based. **Reduced from the previous version**: Bodoni sets
noticeably wider than Fraunces and was wrapping headlines mid-word.

| Step | Size | Leading | Tracking |
|---|---|---|---|
| `display` | `clamp(3rem, 6.5vw, 6.5rem)` | `0.92` | `-0.025em` |
| `h1` | `clamp(2.5rem, 4.6vw, 4.4rem)` | `0.98` | `-0.022em` |
| `h2` | `clamp(1.95rem, 3.3vw, 3.1rem)` | `1.04` | `-0.018em` |
| `h3` | `clamp(1.35rem, 1.9vw, 1.75rem)` | `1.2` | `-0.012em` |
| `lead` | `clamp(1.125rem, 1.5vw, 1.375rem)` | `1.55` | `-0.006em` |
| `body` | `1rem` | `1.68` | `-0.003em` |
| `small` | `0.875rem` | `1.55` | `0` |
| `mono-lg` | `0.9375rem` | `1.7` | `-0.02em` |
| `mono` | `0.8125rem` (13px) | `1.75` | `-0.015em` |
| `eyebrow` | `0.75rem` (12px) | `1` | `0.16em` |

Tracking loosened across the board relative to the Forge scale — a didone
collides sooner than a transitional serif.

### 2.4 Measure

Body copy **62–72ch**. Lead paragraphs **50–58ch**. Never full-bleed text.

---

## 3 · The hero — the verification field

`site/src/components/motion/VerificationField.tsx`

### 3.1 What it is

A live graph of 110–300 nodes (density scales with viewport area), drifting.
Every few seconds a **verification wave** expands as a ring from a hub node.
Nodes the front passes flip from oxblood to bone and hold, then **decay**
(`n.v *= 0.988`) as the guarantee goes stale. Edges are recomputed every frame
from live positions and recolour toward verdant as either endpoint is verified,
so the graph genuinely re-wires rather than replaying a fixed mesh.

The cursor displaces nodes it passes near; the field settles back.

### 3.2 Why this, and not a shader

The product's two real mechanisms are a **code graph** and a **verifier gate**.
This is those two things, moving. The wave is the gate sweeping the graph, and
the decay is the argument: a guarantee expires, which is why verification has
to be continuous rather than a one-off claim.

The install command sits at the centre as the one static object in a moving
field.

> **Four earlier heroes were built and discarded**: a domain-warped fBm shader,
> a Damascus-steel field, a raymarched Penrose tribar, and an Apollonian
> fractal. Each was technically sound — the Penrose geometry genuinely worked,
> three axis-aligned beams whose projection closes under an isometric camera.
> All four failed the same test: they were beautiful *about nothing*. The
> current hero is the only one that argues the product's case.

### 3.3 Canvas 2D, deliberately

No shader compile step, no WebGL context to lose, **no fallback ladder at
all**. The previous ogl heroes each needed six rungs of degradation; this needs
none. A few hundred additive strokes is trivially cheap, and it antialiases
better than a raymarch.

| Condition | Behaviour |
|---|---|
| Default | Full field, DPR capped at 2 |
| Small viewport | Node count scales down with area |
| `prefers-reduced-motion` | **Composes one still frame** (90 draws, wave mid-flight), then stops. Never removed. |
| Tab hidden | `cancelAnimationFrame` |
| Scrolled offscreen | RAF paused via IntersectionObserver |

### 3.4 Craft rules

1. **The copy must never fight the field.** A radial scrim sits at
   `rgba(10,7,8,0.95)` behind the text and opens out to `0.06` on the right.
2. **Contrast floor, measured against the brightest frame** — not the average.
   Current: h1 **12.65:1**, lead **5.46:1**, install command **10.28:1**.
3. **Verdant only on verified nodes.** The semantic rule holds inside the
   animation, not just in the UI.

---

## 4 · Motion system

*Unchanged from the Forge version — these curves and durations survived every
redesign.*

### 4.1 Easing

| Name | Curve | Use |
|---|---|---|
| `--ease-forge` | `cubic-bezier(0.22, 1, 0.36, 1)` | **Default.** Entrances, reveals |
| `--ease-strike` | `cubic-bezier(0.65, 0, 0.35, 1)` | Toggles, tabs, state swaps |
| `--ease-draw` | `cubic-bezier(0.16, 1, 0.3, 1)` | Long scroll-linked draws |
| `--ease-quench` | `cubic-bezier(0.34, 1.28, 0.64, 1)` | Rare, tiny overshoot. Badges only |

**Never `ease-in-out` on entrances.** Things enter fast and settle slow.

### 4.2 Duration

Micro (hover, focus) **120–180ms** · Standard (reveal, tab) **320–420ms** ·
Deliberate (section, hero stagger) **600–900ms**.

Stagger between siblings **40–70ms**.

### 4.3 Rules

- Animate **`transform` and `opacity` only**.
- **One** scroll-linked "hero moment" per page — the verifier gate in §5.
- Reveals fire **once** (`unobserve` after).
- Reveal distance **12–20px**. Small movement reads expensive.
- **Every animation resolves to its final state under
  `prefers-reduced-motion`**, and several never start a timer at all.

### 4.4 The motion library

`Reveal` · `CountUp` · `SplitText` · `ScrambleText` · `MagneticButton` (≤6px) ·
`Marquee` · `Parallax` · `TiltCard` (≤5°) · `DrawSVG` · `ScrollProgress`
(+ Lenis).

---

## 5 · The ban list

*Carried forward intact. The build complies with every entry.*

- ❌ Purple→blue gradients, or violet anywhere
- ❌ Gradient text on headings. One accent word maximum, flat
- ❌ Glassmorphism — **nav bar only**, and subtly (currently the only
  `backdrop-blur` in the codebase)
- ❌ Glowing orbs or blurred blobs behind sections
- ❌ Neon cyan-on-black terminal
- ❌ **`box-shadow` for elevation** → hairlines + `etch`
- ❌ Bento grid of equal feature cards
- ❌ Three-column "features" row with an icon above each heading
- ❌ Dot-grid or graph-paper backgrounds
- ❌ A pill badge reading "✨ Introducing…"
- ❌ Inter for everything
- ❌ **Emoji as UI icons** — SVG only (Lucide)
- ❌ Spotlight-follows-cursor on every card
- ❌ Scale-transform hover that shifts layout
- ❌ Typewriter effect on the headline
- ❌ Floating 3D geometric shapes
- ❌ More than one scroll-linked moment per page
- ❌ Fake logos, testimonials, quotes, star counts, user numbers
- ❌ SWE-bench scores (not run)
- ❌ Presenting proxy-rate costs as bills
- ❌ **`pip install vex`** — that is someone else's package. The distribution
  is `vex-harness`; the command is `vex`.

### The instead-of list

| Instead of | Do |
|---|---|
| Gradient heading | Flat `--quench`, one `--ox-bright` word |
| Glass card | `--slab` + 1px `--rule` + `etch` |
| Glow orb | The verification field, and nothing else |
| Bento grid | Asymmetric editorial rows, alternating direction |
| Icon-above-heading trio | A real artefact: terminal, diff, table, graph |
| Logo strip | Reproducible numbers + the repos actually fixed |

---

## 6 · Layout & grid

- **Container:** `1200px` content, `1400px` wide (tables, mockups)
- **Section rhythm:** `clamp(72px, 11vh, 150px)` — **vary it**
- **Asymmetry is the default.** Alternate 7/5 and 5/7. §6 harness runs 7/5;
  §7 execution mirrors it 5/7, so no two consecutive sections share a rhythm
- **Centred full-width blocks: 3 maximum per page** (hero, closing)
- **Breakpoints:** `1280 / 1024 / 768 / 520 / 380`
- **Radii:** `sm 6px` (badges) · `md 10px` (buttons) · `lg 14px` (panels) ·
  `xl 18px`. Nothing is a pill except badges

> **`min-w-0` on every grid child.** CSS Grid defaults items to
> `min-width: auto`, which refuses to shrink below content width — one
> unbreakable URL forced a 398px track inside a 375px viewport and broke the
> page at 390. Every `col-span-*` child carries `min-w-0`.

---

## 7 · Accessibility — non-negotiable

**Verified: axe reports 0 violations across all 13 routes** (WCAG 2.0/2.1 A and
AA). Lighthouse accessibility **100**.

- **Focus:** `2px solid var(--color-ox-bright)` + `2px` offset. Declared
  **unlayered with longhand properties** so no reset can win against it
- **Targets:** ≥`44×44px` on touch (`min-h-11`)
- **Landmarks:** one `<h1>`, `<nav>`/`<main>`/`<footer>`, skip-link first in
  tab order
- **Keyboard:** tabs (arrows/Home/End), mobile sheet and ⌘K palette both trap
  focus, close on Esc, and return focus to their trigger
- **Scrollable regions are focusable.** Any `overflow-x-auto` container holding
  content carries `tabIndex={0}` + `role="region"` + a label — otherwise a
  keyboard user cannot scroll a wide table or code block
- **Colour is never the sole signal** — `DiffBlock` pairs colour with `+`/`−`
  glyphs
- **Motion:** every animation resolves to its final state under
  `prefers-reduced-motion`

> **Verify contrast with pixels, not `getComputedStyle`.** It returns a
> *composited* value for an anti-aliased outline over a filled button, which
> once produced a false "9 of 12 focus rings are broken" report. Sample a
> horizontal strip instead.

---

## 8 · Iconography

**Lucide React**, `1.5px` stroke, `20px`/`24px` only. Never emoji. Icons are
`--smoke` at rest, `--ox-bright` when active. Product diagrams are
**hand-authored SVG**.

---

## 9 · Performance

Measured on a production build:

| Metric | Measured | Target |
|---|---|---|
| Performance | **91** | — |
| Accessibility | **100** | — |
| Best Practices | **100** | — |
| SEO | **100** | — |
| CLS | **0.016** | < 0.05 ✓ |
| Total Blocking Time | **80ms** | — |
| Speed Index | **1.8s** | — |
| LCP | **3.4s** | < 2.0s ✗ |
| Landing JS | **191.7 kB gzip** | < 260 kB ✓ |

**LCP is the one target not met.** The LCP element is the hero lead paragraph
and roughly 86% of the delay is *render delay* — the font swap, not the
network. The original 180 kB JS budget was deliberately lifted; the gate is
retained at 260 kB so genuine runaway is still caught.

---

## 10 · Voice

- **Declarative, not salesy.** "Fix real bugs. Verified, not vibed."
- **Numbers over adjectives.** Never "blazing fast" — give the measurement.
- **Honest by construction.** Every number carries its `n`. Caveats ship *with*
  the claim. The honesty note is a designed component, not fine print.
- **Lowercase `vex`** as the wordmark. Sentence case headings.
- **Negative results stay on the page.** The v1 router ablation made things
  worse and is on `/benchmarks` for exactly that reason.

### The content law

**Every figure lives in `site/src/lib/content/` with a `source` field naming
the repo file it came from.** A claim-shaped number hardcoded in a component is
a build defect, and `scripts/check-sources.mjs` fails on it.

---

## 11 · Reference standard

Aiming at the craft level of **Linear** (restraint), **Vercel** (dark
discipline), **Zed** (density without clutter), **Astral/uv** (OSS-CLI
honesty).

Aiming *away from* every AI-agent landing page shipped in the last 18 months.

---

*The palette, the type stack, the ban list and the content law are locked.
Everything else is craft judgement — exercise it.*
