/**
 * Single source of truth for site-level copy and links.
 * No figure lives here - verified numbers belong in src/lib/content/.
 */
export const site = {
  name: "neo",
  tagline: "Fix real bugs. Verified, not vibed.",
  /**
   * The `<title>` and meta description. Deliberately SEPARATE from
   * `tagline`: the tagline is on-screen design copy and reads as a slogan,
   * which carries none of the terms anyone actually searches. These are the
   * two strings a crawler and a search-result list read first, so they name
   * the product ("neo agent", "AI coding agent", "agent harness") in plain
   * English rather than repeating a slogan.
   */
  seoTitle: "Neo Agent — AI Coding Agent & Bug-Fixing Harness",
  seoDescription:
    "Neo is an open-source AI coding agent and agent harness. It fixes real bugs in your repository and refuses to call a fix done until your test suite passes. Runs entirely in your terminal: `neo fix`, or just `neo`.",
  /** The spellings that mean this same product, for entity disambiguation. */
  alternateNames: [
    "Neo Agent",
    "Neo Coding Agent",
    "Neo Agent Harness",
    "Neo Harness",
    "neo-agent-cli",
  ],
  /** The terms this product should be findable under. */
  keywords: [
    "neo agent",
    "neo coding agent",
    "neo harness",
    "ai coding agent",
    "agent harness",
    "coding agent",
    "autonomous coding agent",
    "ai agent",
    "bug fixing agent",
    "agent harness for developers",
    "cli coding agent",
    "llm agent",
    "mcp",
  ],
  description:
    "An open-source, CLI-first AI coding agent that refuses to claim success until the test suite is green.",
  /** Version, kept next to the copy that quotes it so the two cannot drift. */
  version: "0.3.0",
  pypi: "https://pypi.org/project/neo-agent-cli/",
  repo: "https://github.com/Pavanteja2007/coding-harness",
  /**
   * The canonical origin. Every canonical URL, the sitemap, the robots.txt
   * sitemap line and the OpenGraph image URL are derived from this one
   * string, so it is declared here and nowhere else.
   *
   * `NEXT_PUBLIC_SITE_URL` still wins so a preview deployment (or a fork)
   * can point somewhere else. The fallback is the real production domain
   * rather than `http://localhost:3000`: a build that forgets the env var
   * must not ship `localhost` into every canonical, sitemap entry and
   * social card, which is a silent SEO failure that looks fine in review.
   */
  url: (
    process.env.NEXT_PUBLIC_SITE_URL ?? "https://neo-agent.si"
  ).replace(/\/+$/, ""),
  /** Bare host, for a same-origin assertion and for the redirect config. */
  host: "neo-agent.si",
  navLinks: [
    { href: "#how-it-works", label: "How it works" },
    { href: "/architecture", label: "Architecture" },
    { href: "/benchmarks", label: "Benchmarks" },
    { href: "/docs", label: "Docs" },
  ],
} as const;

/**
 * Absolute canonical URL for a route.
 *
 * Passed to a page's own `metadata.alternates.canonical`, never to the root
 * layout: an inherited canonical is a canonical for every route in the tree.
 * `/` is the origin itself (no trailing slash), which is the form the
 * redirect rules in `vercel.json` send `www.` to, so the two cannot disagree
 * about which spelling is canonical.
 */
export function canonical(path = "/"): string {
  const clean = path === "/" ? "" : `/${path.replace(/^\/+|\/+$/g, "")}`;
  return `${site.url}${clean}`;
}
