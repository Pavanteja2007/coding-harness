import type { Metadata } from "next";
import Link from "next/link";
import { Reveal } from "@/components/motion/Reveal";
import { DOCS, DOC_SECTIONS } from "@/lib/content/docs";

export const metadata: Metadata = {
  title: "Docs",
  description:
    "Install vex, fix your first bug, and understand the verifier gate, adaptive routing, the sandbox, and the MCP surface.",
};

export default function DocsIndex() {
  return (
    <>
      <Reveal>
        <div className="mb-4 flex items-center gap-3">
          <span className="font-mono text-mono uppercase tracking-[0.16em] text-ox-bright">
            Docs
          </span>
          <span aria-hidden="true" className="h-px w-16 bg-rule-hot" />
        </div>
        <h1 className="mb-5 max-w-[16ch] text-h1 text-quench">
          Everything, in the order you need it.
        </h1>
        <p className="mb-14 max-w-[60ch] text-lead text-ash">
          Start with install and the quickstart. The concept pages explain why
          the gate is absolute and how routing decides what to spend.
        </p>
      </Reveal>

      <div className="flex flex-col gap-12">
        {DOC_SECTIONS.map((section, si) => (
          <Reveal key={section} delay={si * 70}>
            <section>
              <h2 className="mb-5 font-mono text-mono text-smoke">{section}</h2>
              <ul className="grid gap-px overflow-hidden rounded-lg border border-rule bg-rule sm:grid-cols-2">
                {DOCS.filter((d) => d.section === section).map((d) => (
                  <li key={d.slug}>
                    <Link
                      href={`/docs/${d.slug}`}
                      className="group flex h-full flex-col bg-slab p-5 transition-colors duration-150 ease-forge hover:bg-char"
                    >
                      <span className="mb-2 text-h3 text-quench">{d.title}</span>
                      <span className="text-small text-ash">{d.summary}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          </Reveal>
        ))}
      </div>
    </>
  );
}
