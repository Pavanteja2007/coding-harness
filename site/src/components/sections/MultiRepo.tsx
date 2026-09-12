import { Reveal } from "@/components/motion/Reveal";
import { Section } from "@/components/primitives/Section";
import { Badge } from "@/components/primitives/Badge";
import { REPOS, MULTIREPO_SUMMARY } from "@/lib/content/repos";

/**
 * §12 Multi-repo validation.
 *
 * DECISION D7 is enforced by the DATA, not by prose discipline: bottle, click
 * and parse are absent from repos.ts entirely, because README.md:112-115
 * describes them as in-flight with no numbers. Listing them as validated would
 * be a fabricated claim.
 *
 * The honest failure on jaraco/path's first attempt is shown, not hidden.
 */
export function MultiRepo() {
  const full = REPOS.filter((r) => r.kind !== "ablation-set");
  const set = REPOS.filter((r) => r.kind === "ablation-set");

  return (
    <Section id="multirepo" labelledBy="mr-h" tone="basalt">
      <div className="mb-12 grid gap-10 lg:grid-cols-12 lg:gap-14">
        <div className="min-w-0 lg:col-span-6">
          <Reveal>
            <h2 id="mr-h" className="mb-5 max-w-[17ch] text-h2 text-quench">
              Run against code it had never seen.
            </h2>
          </Reveal>
          <Reveal delay={60}>
            <p className="max-w-[56ch] text-lead text-ash">
              {MULTIREPO_SUMMARY.text}
            </p>
          </Reveal>
        </div>
        <div className="min-w-0 lg:col-span-6 lg:pt-2">
          <Reveal delay={90}>
            <div className="rounded-lg border border-rule border-l-2 border-l-warn bg-char p-5">
              <h3 className="mb-2 font-mono text-mono text-warn">
                Where it fell short
              </h3>
              <p className="max-w-[58ch] text-small text-ash">
                {MULTIREPO_SUMMARY.caveat}
              </p>
            </div>
          </Reveal>
        </div>
      </div>

      <ul className="mb-8 grid gap-px overflow-hidden rounded-lg border border-rule bg-rule">
        {full.map((r, i) => (
          <Reveal key={r.name} delay={i * 60} as="li">
            <div className="flex flex-col gap-3 bg-slab p-5 sm:flex-row sm:items-start sm:gap-6">
              <div className="flex min-w-[180px] items-center gap-3">
                <Badge state="verified">verified</Badge>
                <code className="font-mono text-monolg text-quench">
                  {r.name}
                </code>
              </div>
              <p className="flex-1 text-small text-ash">{r.note}</p>
              {r.costUsd !== undefined ? (
                <div className="tnum shrink-0 font-mono text-mono text-soot">
                  {r.attempts} attempt · {r.calls} calls · ${r.costUsd}
                </div>
              ) : null}
            </div>
          </Reveal>
        ))}
      </ul>

      <Reveal delay={80}>
        <div className="rounded-lg border border-rule bg-char p-5">
          <div className="mb-3 font-mono text-mono text-soot">
            the five-repo routing ablation, pinned SHAs
          </div>
          <div className="flex flex-wrap gap-2">
            {set.map((r) => (
              <code
                key={r.name}
                className="rounded-sm border border-rule bg-slab px-3 py-1.5 font-mono text-mono text-ash"
              >
                {r.name}
              </code>
            ))}
          </div>
        </div>
      </Reveal>
    </Section>
  );
}
