import type { Metadata } from "next";
import { PageHeader } from "@/components/primitives/PageHeader";
import { Section } from "@/components/primitives/Section";
import { Reveal } from "@/components/motion/Reveal";
import { RouterTable } from "@/components/product/RouterTable";
import { HonestyNote } from "@/components/product/HonestyNote";
import { Panel } from "@/components/primitives/Panel";
import { Link } from "@/components/primitives/Link";
import {
  ABLATION_ROWS,
  ROUTING_FACTS,
  HONEST_NEGATIVE,
} from "@/lib/content/ablation";
import { SOAK_FIGURES, SOAK_SOURCE, STRESS } from "@/lib/content/reliability";
import { MULTIREPO_SUMMARY, REPOS } from "@/lib/content/repos";
import { site } from "@/lib/site";

export const metadata: Metadata = {
  title: "Benchmarks",
  description:
    "Every measured result behind vex, with the methodology and the caveats that qualify each number.",
};

/**
 * /benchmarks — the numbers, with the methodology that produced them.
 *
 * This page exists so the landing page's figures can be checked rather than
 * taken on trust. It therefore leads with METHOD, not results, and states the
 * caveats before the wins. Everything is read from the content layer, which
 * cites the repo file each figure came from.
 */

const METHOD = [
  {
    k: "Paired arms",
    v: "The same task set runs twice: once with every call pinned to the expensive model, once with adaptive routing on. Nothing else differs between arms.",
  },
  {
    k: "The full real stack",
    v: "Scheduler subprocesses, the real harness loop, real model calls, and Docker-sandboxed pytest verification. No mocks anywhere in the measured path.",
  },
  {
    k: "Per-call ledgers",
    v: "Every model call appends model, tokens, cost and the routing hint to a JSONL ledger, so the totals are summed from records rather than estimated.",
  },
  {
    k: "Verifier-gated success",
    v: "A task counts as a success only when the target test passes AND the full suite shows no regressions. There is no partial credit.",
  },
];

export default function BenchmarksPage() {
  const adaptive = ABLATION_ROWS.filter((r) => r.arm === "adaptive");
  const baseline = ABLATION_ROWS.filter((r) => r.arm === "always-expensive");
  const totalOff = baseline.reduce((s, r) => s + r.costUsd, 0);
  const totalOn = adaptive.reduce((s, r) => s + r.costUsd, 0);

  return (
    <>
      <PageHeader
        eyebrow="Benchmarks"
        title="Every number, and how it was measured."
        lead="Results are only worth the method behind them, so the method comes first. Each figure below links back to the file in the repository that records it."
      />

      <Section tone="ink">
        <div className="grid gap-10 lg:grid-cols-12 lg:gap-14">
          <div className="min-w-0 lg:col-span-5">
            <Reveal>
              <h2 className="mb-5 max-w-[16ch] text-h2 text-quench">
                How the ablation was run.
              </h2>
            </Reveal>
          </div>
          <div className="min-w-0 lg:col-span-7">
            <dl className="flex flex-col">
              {METHOD.map((m, i) => (
                <Reveal key={m.k} delay={i * 55}>
                  <div className="border-t border-rule py-5">
                    <dt className="mb-2 font-mono text-mono text-ox-bright">{m.k}</dt>
                    <dd className="max-w-[62ch] text-body text-ash">{m.v}</dd>
                  </div>
                </Reveal>
              ))}
            </dl>
          </div>
        </div>
      </Section>

      <Section tone="basalt" bleed>
        <Reveal>
          <h2 className="mb-8 max-w-[20ch] text-h2 text-quench">
            Adaptive routing versus always-expensive.
          </h2>
        </Reveal>

        <Reveal delay={60} className="mb-8">
          <RouterTable />
        </Reveal>

        <div className="grid gap-6 lg:grid-cols-12">
          <Reveal delay={80} className="lg:col-span-5">
            <Panel tone="char" className="h-full p-6">
              <div className="mb-4 font-mono text-mono text-soot">
                summed across all three task sets
              </div>
              <dl className="flex flex-col gap-4">
                <div className="flex items-baseline justify-between gap-4">
                  <dt className="text-small text-ash">always-expensive</dt>
                  <dd className="tnum font-mono text-monolg text-ash">
                    ${totalOff.toFixed(4)}
                  </dd>
                </div>
                <div className="flex items-baseline justify-between gap-4">
                  <dt className="text-small text-quench">adaptive</dt>
                  <dd className="tnum font-mono text-monolg text-ox-bright">
                    ${totalOn.toFixed(4)}
                  </dd>
                </div>
                <div className="flex items-baseline justify-between gap-4 border-t border-rule pt-4">
                  <dt className="text-small text-smoke">ratio</dt>
                  <dd className="tnum font-mono text-monolg text-ox-bright">
                    {(totalOff / totalOn).toFixed(2)}×
                  </dd>
                </div>
              </dl>
            </Panel>
          </Reveal>

          <Reveal delay={120} className="lg:col-span-7">
            <HonestyNote className="h-full" />
          </Reveal>
        </div>
      </Section>

      <Section tone="ink">
        <div className="grid gap-10 lg:grid-cols-12 lg:gap-14">
          <Reveal className="min-w-0 lg:col-span-6">
            <h2 className="mb-5 max-w-[18ch] text-h2 text-quench">
              {HONEST_NEGATIVE.title}
            </h2>
            <p className="max-w-[58ch] text-body text-ash">
              {HONEST_NEGATIVE.body}
            </p>
          </Reveal>

          <div className="min-w-0 lg:col-span-6">
            <ul className="flex flex-col gap-4">
              {ROUTING_FACTS.map((f, i) => (
                <Reveal key={f.source} delay={i * 60} as="li">
                  <div className="flex gap-4 border-t border-rule pt-4">
                    <span
                      aria-hidden="true"
                      className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-ox"
                    />
                    <div>
                      <p className="text-body text-ash">{f.text}</p>
                      <p className="mt-1 font-mono text-mono text-soot">
                        {f.source}
                      </p>
                    </div>
                  </div>
                </Reveal>
              ))}
            </ul>
          </div>
        </div>
      </Section>

      <Section tone="basalt">
        <Reveal>
          <h2 className="mb-8 max-w-[20ch] text-h2 text-quench">
            Reliability under deliberate abuse.
          </h2>
        </Reveal>

        <div className="grid gap-6 lg:grid-cols-12">
          <Reveal delay={60} className="lg:col-span-5">
            <Panel className="h-full p-6">
              <h3 className="mb-4 text-h3 text-quench">Stress</h3>
              <p className="mb-5 max-w-[46ch] text-small text-ash">
                {STRESS.tasks} tasks at concurrency {STRESS.concurrency}, with{" "}
                {STRESS.kills} killed simultaneously mid-run.
              </p>
              <dl className="flex flex-col gap-3">
                <div className="flex justify-between gap-4 border-t border-rule pt-3">
                  <dt className="font-mono text-mono text-soot">succeeded</dt>
                  <dd className="tnum font-mono text-mono text-verdant-bright">
                    {STRESS.tasks}/{STRESS.tasks}
                  </dd>
                </div>
                <div className="flex justify-between gap-4 border-t border-rule pt-3">
                  <dt className="font-mono text-mono text-soot">
                    genuine resumes
                  </dt>
                  <dd className="tnum font-mono text-mono text-verdant-bright">
                    {STRESS.resumes}/{STRESS.kills}
                  </dd>
                </div>
                <div className="flex justify-between gap-4 border-t border-rule pt-3">
                  <dt className="font-mono text-mono text-soot">
                    leaked containers
                  </dt>
                  <dd className="tnum font-mono text-mono text-verdant-bright">
                    {STRESS.leakedContainers}
                  </dd>
                </div>
              </dl>
            </Panel>
          </Reveal>

          <Reveal delay={110} className="lg:col-span-7">
            <Panel className="h-full p-6">
              <h3 className="mb-4 text-h3 text-quench">Soak</h3>
              <dl className="grid gap-x-8 gap-y-4 sm:grid-cols-3">
                {SOAK_FIGURES.map((s) => (
                  <div key={s.k}>
                    <dt className="font-mono text-mono text-soot">{s.k}</dt>
                    <dd className="tnum font-mono text-monolg text-quench">
                      {s.v}
                    </dd>
                  </div>
                ))}
              </dl>
              <p className="mt-5 border-t border-rule pt-4 font-mono text-mono text-soot">
                {SOAK_SOURCE}
              </p>
            </Panel>
          </Reveal>
        </div>
      </Section>

      <Section tone="ink">
        <Reveal>
          <h2 className="mb-6 max-w-[20ch] text-h2 text-quench">
            Beyond the fixture set.
          </h2>
        </Reveal>
        <Reveal delay={60}>
          <p className="mb-8 max-w-[64ch] text-lead text-ash">
            {MULTIREPO_SUMMARY.text}
          </p>
        </Reveal>

        <Reveal delay={100}>
          <div className="mb-8 rounded-lg border border-rule border-l-2 border-l-warn bg-char p-5">
            <h3 className="mb-2 font-mono text-mono text-warn">
              Where it fell short
            </h3>
            <p className="max-w-[64ch] text-small text-ash">
              {MULTIREPO_SUMMARY.caveat}
            </p>
          </div>
        </Reveal>

        <ul className="grid gap-px overflow-hidden rounded-lg border border-rule bg-rule sm:grid-cols-2 lg:grid-cols-3">
          {REPOS.map((r, i) => (
            <Reveal key={r.name} delay={i * 45} as="li">
              <div className="h-full bg-slab p-5">
                <code className="mb-2 block font-mono text-monolg text-quench">
                  {r.name}
                </code>
                <p className="font-mono text-mono text-soot">{r.source}</p>
              </div>
            </Reveal>
          ))}
        </ul>

        <Reveal delay={120}>
          <p className="mt-8 max-w-[64ch] text-small text-smoke">
            Repositories with runs still in flight are deliberately absent.
            Numbers appear here only once the repository records them. The full
            write-up lives in{" "}
            <Link href={site.repo + "/blob/main/RESULTS.md"} external>
              RESULTS.md
            </Link>
            .
          </p>
        </Reveal>
      </Section>
    </>
  );
}
