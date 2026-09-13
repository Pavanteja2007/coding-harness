import type { Metadata } from "next";
import { PageHeader } from "@/components/primitives/PageHeader";
import { Section } from "@/components/primitives/Section";
import { Reveal } from "@/components/motion/Reveal";
import { Badge } from "@/components/primitives/Badge";
import { Link } from "@/components/primitives/Link";
import { RELEASES } from "@/lib/content/releases";
import { site } from "@/lib/site";

export const metadata: Metadata = {
  title: "Changelog",
  description:
    "Release history for vex, transcribed from the repository's own CHANGELOG.",
};

/**
 * /changelog — read from lib/content/releases.ts, which transcribes
 * CHANGELOG.md. Untagged build history is labelled as history rather than
 * presented as a version, because inventing version numbers would be exactly
 * the kind of small dishonesty this project is built against.
 */
export default function ChangelogPage() {
  return (
    <>
      <PageHeader
        eyebrow="Changelog"
        title="What shipped, and when."
        lead="Taken from the repository's own changelog. One tagged release so far; the rounds beneath it are build history, labelled as such rather than dressed up as versions."
      />

      <Section tone="ink">
        <div className="flex flex-col gap-16">
          {RELEASES.map((r, ri) => (
            <Reveal key={r.version} delay={ri * 80}>
              <article className="grid gap-8 lg:grid-cols-12 lg:gap-12">
                <div className="min-w-0 lg:col-span-4">
                  <div className="lg:sticky lg:top-24">
                    <div className="mb-3 flex flex-wrap items-center gap-3">
                      <h2 className="font-display text-h3 text-quench">
                        {r.version}
                      </h2>
                      {r.tagged ? (
                        <Badge state="verified">tagged</Badge>
                      ) : (
                        <Badge>history</Badge>
                      )}
                    </div>
                    <div className="mb-4 font-mono text-mono text-smoke">
                      {r.date}
                    </div>
                    <p className="max-w-[42ch] text-small text-ash">
                      {r.summary}
                    </p>
                    <p className="mt-4 font-mono text-mono text-smoke">
                      {r.source}
                    </p>
                  </div>
                </div>

                <div className="min-w-0 lg:col-span-8">
                  <div className="flex flex-col gap-8">
                    {r.groups.map((g) => (
                      <section key={g.title}>
                        <h3 className="mb-4 border-b border-rule pb-2 font-mono text-mono text-ox-bright">
                          {g.title}
                        </h3>
                        <ul className="flex flex-col gap-3">
                          {g.items.map((it) => (
                            <li
                              key={it}
                              className="flex gap-3 text-body text-ash"
                            >
                              <span
                                aria-hidden="true"
                                className="mt-3 h-px w-4 shrink-0 bg-rule-hot"
                              />
                              <span className="max-w-[64ch]">{it}</span>
                            </li>
                          ))}
                        </ul>
                      </section>
                    ))}
                  </div>
                </div>
              </article>
            </Reveal>
          ))}
        </div>

        <Reveal delay={120}>
          <p className="mt-14 border-t border-rule pt-6 text-small text-smoke">
            The authoritative record is{" "}
            <Link href={site.repo + "/blob/main/CHANGELOG.md"} external>
              CHANGELOG.md
            </Link>{" "}
            in the repository. Per-module detail lives in each module&apos;s
            AGENTS.md, and the cross-module contracts in INTERFACES.md.
          </p>
        </Reveal>
      </Section>
    </>
  );
}
