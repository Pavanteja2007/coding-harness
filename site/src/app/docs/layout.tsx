import Link from "next/link";
import { DOCS, DOC_SECTIONS } from "@/lib/content/docs";

/**
 * Docs shell: a sticky sidebar of every page, grouped by section.
 *
 * The sidebar is a real <nav> with a list, so it is navigable by landmark and
 * by list semantics rather than being a wall of anonymous links.
 */
export default function DocsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="mx-auto flex max-w-[1200px] gap-12 px-6 py-14 lg:py-20">
      <nav
        aria-label="Documentation"
        className="hidden w-56 shrink-0 lg:block"
      >
        <div className="sticky top-24 flex flex-col gap-7">
          {DOC_SECTIONS.map((section) => (
            <div key={section}>
              <h2 className="mb-3 font-mono text-mono text-soot">{section}</h2>
              <ul className="flex flex-col gap-1.5 border-l border-rule">
                {DOCS.filter((d) => d.section === section).map((d) => (
                  <li key={d.slug}>
                    <Link
                      href={`/docs/${d.slug}`}
                      className="-ml-px block border-l border-transparent pl-4 text-small text-ash transition-colors duration-150 ease-forge hover:border-gilt hover:text-quench"
                    >
                      {d.title}
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </nav>

      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}
