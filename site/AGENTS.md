# site/ — Terminal 4: Product and documentation site

## Scope

- Next.js 15 / React 19 marketing and documentation site.
- The site is a separate release surface. It is not included in the Python wheel or sdist.
- `src/lib/content/` is the authority for product claims; each figure cites a repository artifact.

## Current state

- `npm run gate` builds production output and enforces the 260 kB gzip landing-route JavaScript budget.
- `node scripts/check-sources.mjs` enforces traceable content and rejects claim-shaped numbers hardcoded in components/routes.
- `tests/test_installed_user_flow.py` and the Python release workflow do not replace the site gates; `.github/workflows/release-gate.yml` runs both site jobs independently.
- `error.tsx`, `not-found.tsx`, and `lib/highlight.ts` are required source files, not generated artifacts. They must be included in the clean release candidate.

## Verification

- `npm run gate`: exit 0, 28 generated pages, landing JavaScript 195.9 kB gzip.
- `node scripts/check-sources.mjs`: exit 0, 130 traceable content entries.
- The production build emits one expected warning for edge-runtime static generation; it does not fail the build.

## Release blockers

- A clean checkout must contain every required source file referenced by tracked imports.
- The site build is verified independently of Docker and Python package publication.
- Do not upload `.next/`, `.shots/`, `node_modules/`, or local TypeScript build state.
