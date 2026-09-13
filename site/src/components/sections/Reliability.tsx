import { Reveal } from "@/components/motion/Reveal";
import { Section } from "@/components/primitives/Section";
import { Badge } from "@/components/primitives/Badge";
import {
  STRESS,
  SOAK_FIGURES,
  SOAK_HOURS,
  SOAK_BUG,
} from "@/lib/content/reliability";

/**
 * §9 Reliability - the SessionBoard.
 *
 * Shows the 45-at-45 stress run as a board: tasks running, eight killed
 * mid-flight, all eight resumed, all 45 passed. The resumed column carries
 * patina badges because those tasks genuinely completed after a hard kill.
 *
 * Figures: README.md:124-127 (stress) and RESULTS.md:139-148 (soak).
 */
// Counts come from STRESS so they cannot drift from README.md:124-127.
const COLUMNS = [
  { key: "running", label: "running", count: STRESS.tasks, tone: "neutral" as const },
  { key: "killed", label: "hard-killed mid-run", count: STRESS.kills, tone: "fail" as const },
  { key: "resumed", label: "resumed from checkpoint", count: STRESS.resumes, tone: "verified" as const },
  { key: "passed", label: "verified success", count: STRESS.tasks, tone: "verified" as const },
];

export function Reliability() {
  return (
    <Section id="reliability" labelledBy="rel-h" tone="basalt">
      <div className="mb-12 max-w-[62ch]">
        <Reveal>
          <h2 id="rel-h" className="mb-5 text-h2 text-quench">
            Killed mid-run, and it still finished.
          </h2>
        </Reveal>
        <Reveal delay={60}>
          <p className="text-lead text-ash">
            Forty-five tasks at concurrency forty-five, with eight killed
            simultaneously partway through. Every one resumed from its
            checkpoint and completed. No task was lost and no container leaked.
          </p>
        </Reveal>
      </div>

      <div className="mb-12 grid gap-px overflow-hidden rounded-lg border border-rule bg-rule sm:grid-cols-2 lg:grid-cols-4">
        {COLUMNS.map((c, i) => (
          <Reveal key={c.key} delay={i * 60} className="bg-slab p-6">
            <div className="mb-4 flex items-center justify-between">
              <code className="font-mono text-mono text-smoke">{c.label}</code>
              <Badge state={c.tone}>{c.count}</Badge>
            </div>
            {/* Each unit is a task. Colour is paired with position, so the
                board still reads without hue. */}
            <div className="flex flex-wrap gap-1" aria-hidden="true">
              {Array.from({ length: Math.min(c.count, 45) }).map((_, j) => (
                <span
                  key={j}
                  className={[
                    "h-1.5 w-1.5 rounded-[1px]",
                    c.tone === "verified"
                      ? "bg-verdant"
                      : c.tone === "fail"
                        ? "bg-fail"
                        : "bg-soot",
                  ].join(" ")}
                />
              ))}
            </div>
          </Reveal>
        ))}
      </div>

      <Reveal delay={80}>
        <div className="rounded-lg border border-rule bg-char p-6 etch">
          <div className="mb-5 flex flex-wrap items-baseline justify-between gap-3">
            <h3 className="text-h3 text-quench">
              Then the same scheduler, for {SOAK_HOURS}
            </h3>
            <span className="font-mono text-mono text-smoke">
              soak run, one long-lived scheduler
            </span>
          </div>
          <div className="grid gap-x-8 gap-y-4 sm:grid-cols-3">
            {SOAK_FIGURES.map((s) => (
              <div key={s.k}>
                <div className="font-mono text-mono text-smoke">{s.k}</div>
                <div className="tnum font-mono text-monolg text-quench">{s.v}</div>
              </div>
            ))}
          </div>
          <p className="mt-5 max-w-[68ch] border-t border-rule pt-4 text-small text-smoke">
            {SOAK_BUG.text}
          </p>
        </div>
      </Reveal>
    </Section>
  );
}
