import Link from "next/link";
import { ButtonLink } from "@/components/primitives/Button";
import { DOCS } from "@/lib/content/docs";

export const metadata = {
  title: "Not found",
  // A 404 should never be indexed as content.
  robots: { index: false, follow: true },
};

/**
 * 404.
 *
 * Without this file Next serves an unstyled default: white ground, system
 * font, no nav. On a site this committed to one visual register, that reads
 * as a different site entirely.
 *
 * An error page is also a navigation opportunity, not just an apology — so it
 * offers the pages someone landing here most likely wanted. It does not
 * apologise, per the voice rules; it says what happened and what to do.
 */
export default function NotFound() {
  const starters = DOCS.filter((d) => d.section === "Getting started").slice(0, 3);

  return (
    <section className="mx-auto flex min-h-[70svh] max-w-[1200px] flex-col justify-center px-6 py-24">
      <div className="max-w-[52rem]">
        <div className="mb-6 flex items-center gap-3">
          <span className="font-mono text-mono uppercase tracking-[0.16em] text-ox-bright">
            404
          </span>
          <span aria-hidden="true" className="h-px w-16 bg-rule-hot" />
        </div>

        <h1 className="mb-6 max-w-[16ch] text-h1 text-quench">
          That page does not exist.
        </h1>

        <p className="mb-10 max-w-[54ch] text-lead text-ash">
          The link may be out of date, or the page may have moved. Everything
          below is a reasonable place to pick up.
        </p>

        <div className="mb-12 flex flex-wrap gap-3">
          <ButtonLink variant="ox" size="lg" href="/">
            Back to the start
          </ButtonLink>
          <ButtonLink variant="secondary" size="lg" href="/docs">
            Browse the docs
          </ButtonLink>
        </div>

        <div className="border-t border-rule pt-8">
          <h2 className="mb-4 font-mono text-mono text-smoke">
            Getting started
          </h2>
          <ul className="flex flex-col gap-3">
            {starters.map((d) => (
              <li key={d.slug}>
                <Link
                  href={`/docs/${d.slug}`}
                  className="group flex flex-col gap-1"
                >
                  <span className="text-body text-quench transition-colors duration-150 ease-forge group-hover:text-ox-bright">
                    {d.title}
                  </span>
                  <span className="max-w-[58ch] text-small text-ash">
                    {d.summary}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}
