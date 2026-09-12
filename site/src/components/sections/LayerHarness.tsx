import { Reveal } from "@/components/motion/Reveal";
import { Section } from "@/components/primitives/Section";
import { DiffBlock, type DiffLine } from "@/components/product/DiffBlock";
import { Panel } from "@/components/primitives/Panel";

/**
 * §6 Layer 1 - Harness. Asymmetric 7/5 row (§7 mirrors it 5/7).
 *
 * Shows two real artefacts rather than describing them: a unified diff and an
 * excerpt of the rationale.md the harness writes on a verified fix. DESIGN.md
 * §5 "instead-of" table: a real artefact beats an icon trio every time.
 */
const DIFF: DiffLine[] = [
  { kind: "meta", text: "@@ -12,7 +12,7 @@ def mean(values):" },
  { kind: "ctx", text: "    if not values:" },
  { kind: "ctx", text: "        raise ValueError('empty sequence')" },
  { kind: "del", text: "    return sum(values)" },
  { kind: "add", text: "    return sum(values) / len(values)" },
  { kind: "ctx", text: "" },
  { kind: "ctx", text: "def median(values):" },
];

export function LayerHarness() {
  return (
    <Section id="harness" labelledBy="harness-h" tone="ink">
      <div className="grid gap-12 lg:grid-cols-12 lg:gap-14">
        <div className="min-w-0 lg:col-span-5">
          <Reveal>
            <div className="mb-4 flex items-baseline gap-3">
              <span className="tnum font-mono text-mono text-ox-bright">01</span>
              <code className="font-mono text-mono text-soot">harness/</code>
            </div>
            <h2 id="harness-h" className="mb-5 max-w-[16ch] text-h2 text-quench">
              Planner, step agent, verifier gate.
            </h2>
          </Reveal>
          <Reveal delay={60}>
            <p className="mb-6 max-w-[54ch] text-lead text-ash">
              The loop is bash-only, with a fresh session per step and the task
              constraints re-injected each time. The original repo is never
              touched: the harness works on a snapshot and diffs it on the host.
            </p>
          </Reveal>
          <Reveal delay={110}>
            <ul className="flex flex-col gap-3 text-body text-ash">
              {[
                "Checkpoint and resume at step granularity",
                "Protected paths — the agent cannot reach .git or tests",
                "RECALL pulls older detail back out of the task trace",
                "On success: branch, commit, PR description, rationale.md",
              ].map((t) => (
                <li key={t} className="flex gap-3">
                  <span
                    aria-hidden="true"
                    className="mt-2.5 h-px w-4 shrink-0 bg-rule-hot"
                  />
                  {t}
                </li>
              ))}
            </ul>
          </Reveal>
        </div>

        <div className="flex min-w-0 flex-col gap-5 lg:col-span-7">
          <Reveal delay={80}>
            <DiffBlock file="mathutil.py" lines={DIFF} />
          </Reveal>

          <Reveal delay={140}>
            <Panel className="p-5">
              <div className="mb-3 flex items-center justify-between">
                <code className="font-mono text-mono text-ash">
                  rationale.md
                </code>
                <span className="font-mono text-mono text-soot">
                  written on verified fixes
                </span>
              </div>
              <div className="flex flex-col gap-2.5 text-small text-ash">
                <p>
                  <span className="text-quench">What was wrong.</span>{" "}
                  <code className="text-ox-bright">mean()</code> returned the
                  sum of the sequence rather than the arithmetic mean, so every
                  caller received a value scaled by the element count.
                </p>
                <p>
                  <span className="text-quench">What changed.</span> Divided the
                  accumulated sum by <code className="text-ox-bright">len(values)</code>,
                  leaving the existing empty-sequence guard intact.
                </p>
                <p>
                  <span className="text-quench">How it was verified.</span> The
                  target test passed and the full suite showed no regressions.
                </p>
              </div>
            </Panel>
          </Reveal>
        </div>
      </div>
    </Section>
  );
}
