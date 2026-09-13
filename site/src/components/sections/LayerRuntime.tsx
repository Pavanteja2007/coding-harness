import { Reveal } from "@/components/motion/Reveal";
import { Section } from "@/components/primitives/Section";
import { Panel } from "@/components/primitives/Panel";
import { RouterTable } from "@/components/product/RouterTable";
import { HonestyNote } from "@/components/product/HonestyNote";
import { ROUTING_FACTS, HONEST_NEGATIVE } from "@/lib/content/ablation";

/**
 * §8 Layer 3 - Runtime. The novel mechanism, so it gets the most room and the
 * full ablation table.
 *
 * The honest negative (v1 made things WORSE) is given real estate rather than
 * omitted. MASTER_BUILD_PROMPT §9: "Honesty is the brand." A result that only
 * ever reports wins is the thing a reader discounts.
 */
export function LayerRuntime() {
  return (
    <Section id="routing" labelledBy="routing-h" tone="ink" bleed>
      <div className="mb-12 grid gap-10 lg:grid-cols-12 lg:gap-14">
        <div className="min-w-0 lg:col-span-6">
          <Reveal>
            <div className="mb-4 flex items-baseline gap-3">
              <span className="tnum font-mono text-mono text-ox-bright">03</span>
              <code className="font-mono text-mono text-smoke">runtime/</code>
            </div>
            <h2 id="routing-h" className="mb-5 max-w-[18ch] text-h2 text-quench">
              Most calls do not need the expensive model.
            </h2>
          </Reveal>
          <Reveal delay={60}>
            <p className="max-w-[56ch] text-lead text-ash">
              Per call, the runtime predicts difficulty from two signals — the
              issue text itself, and whether the agent is visibly struggling in
              the conversation tail — then routes easy work to a cheap tier and
              escalates only when it must. Every call lands in a JSONL ledger, so
              the mechanism is measurable rather than asserted.
            </p>
          </Reveal>
        </div>

        <div className="min-w-0 lg:col-span-6">
          <ul className="flex flex-col gap-4">
            {ROUTING_FACTS.map((f, i) => (
              <Reveal key={f.source} delay={i * 60} as="li">
                <div className="flex gap-4 border-t border-rule pt-4">
                  <span
                    aria-hidden="true"
                    className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-ox"
                  />
                  <p className="text-body text-ash">{f.text}</p>
                </div>
              </Reveal>
            ))}
          </ul>
        </div>
      </div>

      <Reveal delay={40} className="mb-8">
        <RouterTable />
      </Reveal>

      <div className="grid gap-6 lg:grid-cols-12">
        <Reveal delay={60} className="lg:col-span-7">
          <Panel tone="char" className="h-full p-6">
            <h3 className="mb-3 text-h3 text-quench">
              {HONEST_NEGATIVE.title}
            </h3>
            <p className="mb-4 max-w-[64ch] text-small text-ash">
              {HONEST_NEGATIVE.body}
            </p>
            <div className="flex flex-wrap gap-x-8 gap-y-2 border-t border-rule pt-4">
              <div>
                <div className="font-mono text-mono text-smoke">baseline</div>
                <div className="tnum font-mono text-monolg text-ash">
                  ${HONEST_NEGATIVE.offCost.toFixed(4)}
                </div>
              </div>
              <div>
                <div className="font-mono text-mono text-smoke">
                  adaptive, v1
                </div>
                <div className="tnum font-mono text-monolg text-fail">
                  ${HONEST_NEGATIVE.onCost.toFixed(4)}
                </div>
              </div>
              <div className="flex items-end">
                <span className="font-mono text-mono text-fail">
                  worse, not better
                </span>
              </div>
            </div>
          </Panel>
        </Reveal>

        <Reveal delay={110} className="lg:col-span-5">
          <HonestyNote className="h-full" />
        </Reveal>
      </div>
    </Section>
  );
}
