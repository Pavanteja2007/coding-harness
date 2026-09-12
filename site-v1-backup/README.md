# Vex — landing page

A self-contained static landing page for **Vex**, the verifier-gated coding agent.
Built from `../MASTER_BUILD_PROMPT.md` against the design system in `../DESIGN.md`.

## Files

| File | What it is |
|---|---|
| `index.html` | The page — all 15 sections, semantic markup. |
| `styles.css` | Design system tokens + all styles. Depth via panels + hairlines. |
| `main.js` | Vanilla JS: reveals, terminal/loop/board animation, count-ups, copy, tabs, mobile nav. |
| `favicon.svg` | Browser-tab mark (violet "v" + verifier-pass dot). |
| `og-image.svg` | **Source** for the social-share image. See below. |

No build step, no framework, no runtime dependencies beyond Google Fonts.

## Run locally

Just open `index.html`, or serve the folder:

```bash
python -m http.server 8000 --directory site
# → http://localhost:8000
```

## Deploy

Drop the `site/` folder on any static host — GitHub Pages, Netlify, Cloudflare Pages, S3.

- **GitHub Pages:** Settings → Pages → deploy from `main` / `/site` (or move these files to the repo root / a `docs/` folder). Then update the `canonical`, `og:url` in `index.html` to the live URL.
- **Netlify / Cloudflare Pages:** set the publish directory to `site`, no build command.

## OG image — one manual step

Social platforms need a **raster** image (`og-image.png`, 1200×630). `og-image.svg`
is the source; rasterize it once:

```bash
# with rsvg-convert (librsvg)
rsvg-convert -w 1200 -h 630 site/og-image.svg -o site/og-image.png
# or with ImageMagick
magick -background none -density 144 site/og-image.svg -resize 1200x630 site/og-image.png
# or: open og-image.svg in a browser at 1200×630 and screenshot
```

`index.html` already points `og:image` / `twitter:image` at `og-image.png`.

## Notes

- **Palette is locked** to the dark-violet-and-black tokens in `styles.css :root`
  (see `../DESIGN.md` §3). Green (`--ok`) appears only on verifier-pass states.
- All product numbers are copied from `../README.md` / `../RESULTS.md`. The honesty
  footnote ships with the ablation table — don't strip it.
- Fully responsive (360–1440px), keyboard-navigable, and honors
  `prefers-reduced-motion` (all animation resolves to final states).
