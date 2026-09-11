/**
 * Single source of truth for site-level copy and links.
 * No figure lives here - verified numbers belong in src/lib/content/.
 */
export const site = {
  name: "vex",
  tagline: "Fix real bugs. Verified, not vibed.",
  description:
    "An open-source, CLI-first AI coding agent that refuses to claim success until the test suite is green.",
  repo: "https://github.com/Pavanteja2007/coding-harness",
  // No domain is invented here. Set NEXT_PUBLIC_SITE_URL at deploy time.
  url: process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000",
  navLinks: [
    { href: "#how-it-works", label: "How it works" },
    { href: "/architecture", label: "Architecture" },
    { href: "/benchmarks", label: "Benchmarks" },
    { href: "/docs", label: "Docs" },
  ],
} as const;
