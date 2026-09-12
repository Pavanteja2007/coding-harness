import type { Metadata } from "next";
import { PageHeader } from "@/components/primitives/PageHeader";
import { Section } from "@/components/primitives/Section";
import { Reveal } from "@/components/motion/Reveal";
import { Panel } from "@/components/primitives/Panel";
import { Marquee } from "@/components/motion/Marquee";
import { ButtonLink } from "@/components/primitives/Button";
import { MagneticButton } from "@/components/motion/MagneticButton";
import { site } from "@/lib/site";
import { LIMITS } from "@/lib/content/limits";

export const metadata: Metadata = {
  title: "About",
  description: "The thesis behind vex, its honest limitations, and the license.",
};

const STACK = [
  "Python 3.10",
  "litellm",
  "Docker",
  "tree-sitter",
  "MCP Python SDK",
  "argparse",
  "SQLite",
  "pytest",
  "stdlib dashboard",
];

export default function AboutPage() {
  return (
    <>
      <PageHeader
        eyebrow="About"
        title="A harness that refuses to claim success."
        lead="Most coding agents finish when the model says they have finished. That is a claim about the work, produced by the thing that did the work. vex replaces it with a test."
      />

      <Section tone="ink">
        <div className="grid gap-10 lg:grid-cols-12 lg:gap-14">
          <Reveal className="min-w-0 lg:col-span-6">
            <h2 className="mb-5 max-w-[16ch] text-h2 text-quench">
              The thesis.
            </h2>
            <div className="flex max-w-[58ch] flex-col gap-4 text-body text-ash">
              <p>
                An agent that grades its own homework will always pass. The only
                way to know whether a fix is real is to run the tests that were
                already there, in an environment the agent cannot reach into,
                and to treat the result as final.
              </p>
              <p>
                That constraint shapes everything else. Because success must be
                provable, the sandbox has to be sealed. Because a run can be
                killed mid-flight, progress has to be checkpointed. Because the
                loop makes many calls and most of them are easy, routing by
                predicted difficulty becomes worth doing. The four layers are
                not a feature list — each exists because the gate demands it.
              </p>
              <p>
                The same standard applies to this project&apos;s own claims.
                Every number on this site is traceable to a file in the
                repository, every caveat travels with the figure it qualifies,
                and the results that came out badly are still here.
              </p>
            </div>
          </Reveal>

          <Reveal delay={80} className="min-w-0 lg:col-span-6">
            <Panel tone="char" className="p-7">
              <blockquote>
                <p className="mb-5 font-display text-h3 leading-snug text-quench">
                  The first version of the router made things worse — twice the
                  cost for no gain. That result is on the benchmarks page,
                  because a project that reports only its wins has told you
                  nothing about its losses.
                </p>
                <footer className="font-mono text-mono text-soot">
                  the v1 ablation, kept
                </footer>
              </blockquote>
            </Panel>
          </Reveal>
        </div>
      </Section>

      <Section tone="basalt">
        <Reveal>
          <h2 className="mb-4 max-w-[18ch] text-h2 text-quench">
            What it cannot do.
          </h2>
          <p className="mb-10 max-w-[60ch] text-lead text-ash">
            Stated plainly and up front, because this is the part that decides
            whether the rest is useful to you.
          </p>
        </Reveal>

        <dl className="grid gap-px overflow-hidden rounded-lg border border-rule bg-rule sm:grid-cols-2">
          {LIMITS.map((l, i) => (
            <Reveal key={l.k} delay={i * 45}>
              <div className="h-full bg-slab p-6">
                <dt className="mb-2 font-mono text-mono text-warn">{l.k}</dt>
                <dd className="text-small text-ash">{l.v}</dd>
              </div>
            </Reveal>
          ))}
        </dl>
      </Section>

      <Section tone="ink">
        <Reveal>
          <h2 className="mb-8 max-w-[18ch] text-h2 text-quench">Built with.</h2>
        </Reveal>
        <Reveal delay={60}>
          <Marquee items={STACK} className="mb-10" />
        </Reveal>
        <Reveal delay={100}>
          <p className="max-w-[64ch] text-body text-ash">
            Open source. The full source, the results write-up, and the changelog
            are all in the repository — including the logs layout that every
            figure on this site is derived from.
          </p>
        </Reveal>
        <Reveal delay={140}>
          <div className="mt-8 flex flex-wrap gap-3">
            <MagneticButton>
              <ButtonLink
                variant="gilt"
                size="lg"
                href={site.repo}
                target="_blank"
                rel="noreferrer noopener"
              >
                View the source
              </ButtonLink>
            </MagneticButton>
            <MagneticButton>
              <ButtonLink variant="secondary" size="lg" href="/benchmarks">
                See the numbers
              </ButtonLink>
            </MagneticButton>
          </div>
        </Reveal>
      </Section>
    </>
  );
}
