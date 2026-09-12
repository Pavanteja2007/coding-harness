import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { Reveal } from "@/components/motion/Reveal";
import { DocBody } from "@/components/product/DocBody";
import { DOCS, docBySlug } from "@/lib/content/docs";

export function generateStaticParams() {
  return DOCS.map((d) => ({ slug: d.slug }));
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string }>;
}): Promise<Metadata> {
  const { slug } = await params;
  const doc = docBySlug(slug);
  if (!doc) return { title: "Not found" };
  return { title: doc.title, description: doc.summary };
}

export default async function DocPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const doc = docBySlug(slug);
  if (!doc) notFound();

  const idx = DOCS.findIndex((d) => d.slug === slug);
  const prev = idx > 0 ? DOCS[idx - 1] : null;
  const next = idx < DOCS.length - 1 ? DOCS[idx + 1] : null;

  // On-this-page links, derived from the heading blocks.
  const headings = doc.blocks
    .filter((b): b is Extract<typeof b, { type: "h" }> => b.type === "h")
    .map((b) => ({
      text: b.text,
      id: b.text.toLowerCase().replace(/[^a-z0-9]+/g, "-"),
    }));

  return (
    <div className="flex gap-12">
      <article className="min-w-0 flex-1">
        <Reveal>
          <div className="mb-4 flex items-center gap-3">
            <span className="font-mono text-mono uppercase tracking-[0.16em] text-gilt">
              {doc.section}
            </span>
            <span aria-hidden="true" className="h-px w-12 bg-rule-hot" />
          </div>
          <h1 className="mb-5 max-w-[18ch] text-h1 text-quench">{doc.title}</h1>
          <p className="mb-12 max-w-[62ch] text-lead text-ash">{doc.summary}</p>
        </Reveal>

        <Reveal delay={60}>
          <DocBody blocks={doc.blocks} />
        </Reveal>

        <nav
          aria-label="Pagination"
          className="mt-16 flex flex-wrap gap-4 border-t border-rule pt-8"
        >
          {prev ? (
            <Link
              href={`/docs/${prev.slug}`}
              className="group flex flex-col gap-1 rounded-md border border-rule bg-slab px-5 py-4 transition-colors duration-150 ease-forge hover:bg-char"
            >
              <span className="font-mono text-mono text-soot">Previous</span>
              <span className="text-body text-quench">{prev.title}</span>
            </Link>
          ) : null}
          {next ? (
            <Link
              href={`/docs/${next.slug}`}
              className="group ml-auto flex flex-col gap-1 rounded-md border border-rule bg-slab px-5 py-4 text-right transition-colors duration-150 ease-forge hover:bg-char"
            >
              <span className="font-mono text-mono text-soot">Next</span>
              <span className="text-body text-quench">{next.title}</span>
            </Link>
          ) : null}
        </nav>
      </article>

      {headings.length > 1 ? (
        <nav
          aria-label="On this page"
          className="hidden w-48 shrink-0 xl:block"
        >
          <div className="sticky top-24">
            <h2 className="mb-3 font-mono text-mono text-soot">On this page</h2>
            <ul className="flex flex-col gap-2 border-l border-rule">
              {headings.map((h) => (
                <li key={h.id}>
                  <a
                    href={`#${h.id}`}
                    className="-ml-px block border-l border-transparent pl-4 text-small text-ash transition-colors duration-150 ease-forge hover:border-gilt hover:text-quench"
                  >
                    {h.text}
                  </a>
                </li>
              ))}
            </ul>
          </div>
        </nav>
      ) : null}
    </div>
  );
}
