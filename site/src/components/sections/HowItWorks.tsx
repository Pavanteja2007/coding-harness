import { Reveal } from "@/components/motion/Reveal";
import { Section } from "@/components/primitives/Section";
import { LoopDiagram } from "@/components/product/LoopDiagram";

/**
 * §5 How it works.
 *
 * Holds the page's ONE scroll-linked moment: the verifier gate opening. Every
 * other section uses plain enter-once reveals, because §4.3 treats a second
 * scroll-linked moment as a defect.
 */
const STAGES = [
  {
    k: "plan",
    t: "A planner decomposes the fix",
    d: "The issue text, retrieved repo context, and any decisions memory has recorded about this repo go in. A checklist of small verifiable steps comes out.",
  },
  {
    k: "step",
    t: "An agent executes with bash, sandboxed",
    d: "Each command runs in a fresh container with no network and a read-only rootfs. The repo is bind-mounted so edits persist to the host, where the harness diffs them.",
  },
  {
    k: "verify",
    t: "The gate",
    d: "The target test must pass AND the full suite must show no regressions. Not one or the other. A failure sends the loop back to the step agent with the real output.",
  },
  {
    k: "output",
    t: "Git-native output, only on success",
    d: "A branch, a commit, a PR description, and a rationale.md grounded in what actually happened — written only once the gate has opened.",
  },
];

export function HowItWorks() {
  return (
    <Section id="how-it-works" labelledBy="how-h" tone="basalt">
      <div className="mb-14 max-w-[62ch]">
        <Reveal>
          <h2 id="how-h" className="mb-5 text-h2 text-quench">
            Success is a test result, not a claim.
          </h2>
        </Reveal>
        <Reveal delay={60}>
          <p className="text-lead text-ash">
            Most agents finish when the model says it is finished. This one
            finishes when the suite says so. Everything below the gate only runs
            because the gate opened.
          </p>
        </Reveal>
      </div>

      <Reveal delay={80} className="mb-16">
        <LoopDiagram />
      </Reveal>

      <ol className="grid gap-px overflow-hidden rounded-lg border border-rule bg-rule sm:grid-cols-2 lg:grid-cols-4">
        {STAGES.map((s, i) => (
          <Reveal
            key={s.k}
            delay={i * 55}
            as="li"
            className="bg-slab p-6"
          >
            <div className="mb-3 flex items-center gap-2.5">
              <span className="tnum font-mono text-mono text-ox-bright">
                {String(i + 1).padStart(2, "0")}
              </span>
              <code className="font-mono text-mono text-soot">{s.k}</code>
            </div>
            <h3 className="mb-2.5 text-body font-medium text-quench">{s.t}</h3>
            <p className="text-small text-ash">{s.d}</p>
          </Reveal>
        ))}
      </ol>
    </Section>
  );
}
