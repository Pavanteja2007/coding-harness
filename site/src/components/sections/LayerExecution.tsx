import { Reveal } from "@/components/motion/Reveal";
import { Section } from "@/components/primitives/Section";
import { Panel } from "@/components/primitives/Panel";
import { SANDBOX_FLAGS, SANDBOX_ADVERSARIAL } from "@/lib/content/sandbox";

/**
 * §7 Layer 2 - Execution. Mirrors §6's direction: 5/7 instead of 7/5, so the
 * page alternates rather than repeating one rhythm (DESIGN.md §6).
 */
export function LayerExecution() {
  return (
    <Section id="execution" labelledBy="exec-h" tone="basalt">
      <div className="grid gap-12 lg:grid-cols-12 lg:gap-14">
        {/* Artefact first this time - the mirror of §6. */}
        <div className="order-2 flex min-w-0 flex-col gap-5 lg:order-1 lg:col-span-7">
          <Reveal>
            <div className="grid gap-px overflow-hidden rounded-lg border border-rule bg-rule sm:grid-cols-2">
              {SANDBOX_FLAGS.map((f) => (
                <div key={f.label} className="bg-char p-4">
                  <code className="mb-1.5 block font-mono text-mono text-gilt-bright">
                    {f.label}
                  </code>
                  <p className="text-small text-ash">{f.detail}</p>
                </div>
              ))}
            </div>
          </Reveal>

          <Reveal delay={90}>
            <Panel tone="char" className="p-5">
              <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1">
                <span className="font-mono text-mono text-verdant-bright">
                  {SANDBOX_ADVERSARIAL.sequential}
                </span>
                <span className="font-mono text-mono text-verdant-bright">
                  {SANDBOX_ADVERSARIAL.concurrent}
                </span>
              </div>
              <p className="max-w-[64ch] text-small text-ash">
                {SANDBOX_ADVERSARIAL.detail}
              </p>
            </Panel>
          </Reveal>
        </div>

        <div className="order-1 min-w-0 lg:order-2 lg:col-span-5">
          <Reveal>
            <div className="mb-4 flex items-baseline gap-3">
              <span className="tnum font-mono text-mono text-gilt">02</span>
              <code className="font-mono text-mono text-soot">execution/</code>
            </div>
            <h2 id="exec-h" className="mb-5 max-w-[17ch] text-h2 text-quench">
              The agent runs inside a box it cannot open.
            </h2>
          </Reveal>
          <Reveal delay={60}>
            <p className="mb-6 max-w-[54ch] text-lead text-ash">
              Every command gets a fresh container. The model can write to the
              repo — that is the job — and nothing else. When Docker is
              unavailable the sandbox raises rather than silently running
              unsandboxed.
            </p>
          </Reveal>
          <Reveal delay={110}>
            <p className="max-w-[54ch] text-small text-smoke">
              One honest design finding, recorded rather than buried: the
              read-write bind mount lets a container write host disk unquota&apos;d.
              There is no Docker primitive for bind-mount quotas, so it is
              inherent to the contract that lets the harness diff on the host.
            </p>
          </Reveal>
        </div>
      </div>
    </Section>
  );
}
