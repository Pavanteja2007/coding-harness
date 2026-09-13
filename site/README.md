# vex — site

The marketing and documentation site for [vex](https://github.com/Pavanteja2007/coding-harness),
a CLI-first AI coding agent.

Built with:

- **Next 15**, App Router (`src/app`)
- **React 19**
- **TypeScript**, `strict: true`
- **Tailwind v4**, CSS-first — design tokens live in an `@theme static` block in
  `src/app/globals.css`. There is no `tailwind.config.js` and there should not be;
  Tailwind is wired in through `@tailwindcss/postcss` in `postcss.config.mjs`.

The motion layer is `gsap`, `motion` and `lenis`. The hero is Canvas 2D
(`src/components/motion/VerificationField.tsx`), dynamically imported so it stays out
of the initial chunk.

## Requirements

Node 20 or newer.

## Running it

```bash
npm install
npm run dev      # next dev  — http://localhost:3000
npm run build    # next build
npm start        # next start — serves the production build
```

`npm run analyze` runs the build with `@next/bundle-analyzer` enabled
(`ANALYZE=true next build`).

## The three gates

Three checks guard this site. They are independent; run all three before shipping.

### `npm run gate` — JS budget

Runs `next build`, then `scripts/check-bundle.mjs`. It sums the **gzipped** JS a
landing visitor actually downloads: the initial chunks (from
`.next/app-build-manifest.json`, `/layout` + `/page`) **plus** this route's own lazy
chunks (from `.next/react-loadable-manifest.json`). Counting only the initial chunks
would under-report the real cost, since the hero arrives moments later as a second
request. Chunks belonging only to other routes are excluded.

The ceiling is **260 kB gzip**, overridable with `LIMIT_KB`. The original budget was
180 kB and was lifted deliberately — the hero plus the full motion layer cost more
than that allowed. The gate was kept rather than deleted, at a ceiling that still
catches genuine runaway.

It **refuses to measure dev output.** Production chunks are content-hashed
(`app/page-f47082ca351a8369.js`); dev chunks are not (`app/page.js`). If every chunk
in the manifest is unhashed, the script fails loudly instead of printing a number.
Measuring dev output once produced a false **1755 kB** reading — unminified sizes are
meaningless against a gzip budget. Always go through `npm run gate`, which builds
first, rather than pointing the script at a stale or dev `.next`.

### `npm run shots` — visual and console check

```bash
npm start          # or npm run dev, in another terminal
npm run shots
```

Drives Playwright (Chromium) over the page at **1440, 1024, 768 and 390** px, at
`deviceScaleFactor: 2`. At each width it asserts:

- **zero console errors** and zero uncaught page errors
- **zero horizontal body scroll** (`scrollWidth > clientWidth + 1` fails)

Full-page PNGs land in `.shots/<width>.png`. Exits non-zero on any failure.

It **scrolls the whole page first**, then returns to the top. A `fullPage` screenshot
does not move the viewport, so without that pass every `IntersectionObserver` reveal
stays unfired and each below-the-fold section photographs at opacity 0. Eight
components depend on this (`Reveal`, `CountUp`, `SplitText`, `DrawSVG`, and others).

Needs a server already running. Point it elsewhere with `URL`, and capture the
reduced-motion variant with `REDUCED=1` (writes `.shots/<width>-reduced.png`).

### `node scripts/check-sources.mjs` — content law

No npm alias; run it directly. Every number on the site must be traceable to a file in
the vex repo, so this enforces two things:

1. Each file in `src/lib/content/` cites a real repo artefact — a `.md`, `.toml` or
   `.py` path.
2. **No component or route hardcodes a product claim.** It walks `src/components` and
   `src/app` looking for claim-shaped numbers in JSX — `2.59×`, `45 concurrent`,
   `$0.0528`, `3,600`, `24/24` — and fails on each one it finds.

The real failure mode is not a content record forgetting its `source`; it is a number
getting hardcoded into a component, where no citation exists and no reviewer can trace
it. This has caught live defects: the stress and soak figures were once inline in
`Reliability.tsx` and now live in `src/lib/content/reliability.ts`.

An `ALLOW` list in the script exempts numbers that are layout, timing, geometry or
index rather than claims — CSS lengths, hand-authored SVG coordinates, animation
durations, hex colours, version strings. Those exemptions are deliberate and
commented; a guard that cries wolf trains you to ignore it.

## Changing a number

1. Figures live in `src/lib/content/*.ts` — `stats`, `ablation`, `reliability`,
   `repos`, `sandbox`, `mcp`, `limits`, `releases`, `install`, `docs`.
2. Every record carries a `source` field naming the repo file and line range it was
   read from, e.g. `"RESULTS.md:83"` or `"README.md:124-127"`.
3. Re-verify against the repo before editing, and update `source` in the same change.
4. **Never hardcode a figure in JSX.** Import it from the content layer;
   `check-sources.mjs` fails if you do not.

Site-level copy and links live in `src/lib/site.ts`. No figure belongs there.

## Deploying

Vercel, framework preset **Next.js**. No environment variables are required.

`NEXT_PUBLIC_SITE_URL` is optional and only affects absolute URLs in metadata and the
sitemap — `metadataBase` in `src/app/layout.tsx`, the sitemap line in
`src/app/robots.ts`, and `src/app/sitemap.ts`. Unset, it falls back to
`http://localhost:3000`; no domain is invented in the source.

## Project layout

```
src/
  app/                    # App Router: routes, metadata, robots, sitemap
    page.tsx              # landing — the route the JS budget measures
    architecture/  benchmarks/  changelog/  about/
    docs/  docs/[slug]/   # docs shell and pages, driven by content/docs.ts
    globals.css           # Tailwind import + @theme design tokens
  components/
    primitives/           # Section, Panel, Button, Badge, Prose, Hairline, ...
    motion/               # Reveal, CountUp, SplitText, VerificationField, ...
    product/              # Terminal, DiffBlock, LoopDiagram, InstallTabs, ...
    chrome/               # Nav, Footer
    sections/             # landing sections: Hero, StatBand, Thesis, Layer*, ...
  lib/
    content/              # every verified figure, each with its source
    site.ts  cn.ts
scripts/
  check-bundle.mjs        # npm run gate
  shot.mjs                # npm run shots
  check-sources.mjs       # content law
```

`.next/` and `.shots/` are build output and are gitignored.
