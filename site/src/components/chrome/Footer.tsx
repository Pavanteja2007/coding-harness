import { site } from "@/lib/site";

/** Four columns: Product / Architecture / Docs / GitHub. Hairline top, no shadow. */
const COLUMNS = [
  {
    heading: "Product",
    links: [
      { label: "How it works", href: "#how-it-works" },
      { label: "Benchmarks", href: "/benchmarks" },
      { label: "Changelog", href: "/changelog" },
      { label: "About", href: "/about" },
    ],
  },
  {
    heading: "Architecture",
    links: [
      { label: "The four layers", href: "/architecture" },
      { label: "The verifier gate", href: "/docs/verifier-gate" },
      { label: "Adaptive routing", href: "/docs/adaptive-routing" },
      { label: "Sandbox model", href: "/docs/sandbox" },
    ],
  },
  {
    heading: "Docs",
    links: [
      { label: "Install", href: "/docs/install" },
      { label: "Quickstart", href: "/docs/quickstart" },
      { label: "CLI reference", href: "/docs/cli" },
      { label: "MCP tools", href: "/docs/mcp" },
    ],
  },
];

const LINK_CLASS =
  "text-small text-ash transition-colors duration-150 ease-forge hover:text-quench";

export function Footer() {
  return (
    <footer className="border-t border-rule">
      <div className="mx-auto max-w-[1200px] px-6 py-16">
        <div className="grid gap-10 sm:grid-cols-2 lg:grid-cols-4">
          {COLUMNS.map((col) => (
            <nav key={col.heading} aria-label={col.heading}>
              <h2 className="mb-4 font-mono text-mono text-smoke">
                {col.heading}
              </h2>
              <ul className="flex flex-col gap-2.5">
                {col.links.map((l) => (
                  <li key={l.label}>
                    <a href={l.href} className={LINK_CLASS}>
                      {l.label}
                    </a>
                  </li>
                ))}
              </ul>
            </nav>
          ))}

          <nav aria-label="GitHub">
            <h2 className="mb-4 font-mono text-mono text-smoke">GitHub</h2>
            <ul className="flex flex-col gap-2.5">
              <li>
                <a href={site.repo} target="_blank" rel="noreferrer noopener" className={LINK_CLASS}>
                  Source
                </a>
              </li>
              <li>
                <a
                  href={site.repo + "/blob/main/RESULTS.md"}
                  target="_blank"
                  rel="noreferrer noopener"
                  className={LINK_CLASS}
                >
                  Results
                </a>
              </li>
              <li>
                <a
                  href={site.repo + "/blob/main/CHANGELOG.md"}
                  target="_blank"
                  rel="noreferrer noopener"
                  className={LINK_CLASS}
                >
                  Changelog
                </a>
              </li>
              <li>
                <a href={site.repo} target="_blank" rel="noreferrer noopener" className={LINK_CLASS}>
                  Issues
                </a>
              </li>
            </ul>
          </nav>
        </div>

        <div className="mt-14 flex flex-wrap items-center justify-between gap-4 border-t border-rule pt-6">
          <span className="font-mono text-mono text-smoke">{site.tagline}</span>
          <span className="font-mono text-mono text-smoke">
            No logos. No testimonials.
          </span>
        </div>
      </div>
    </footer>
  );
}
