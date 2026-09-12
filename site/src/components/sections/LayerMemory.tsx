import { Reveal } from "@/components/motion/Reveal";
import { Section } from "@/components/primitives/Section";
import { Panel } from "@/components/primitives/Panel";
import {
  MCP_TOOLS,
  MCP_CLIENT_NOTE,
  MEMORY_LIMITS,
  MEMORY_ABLATION,
} from "@/lib/content/mcp";

/**
 * §11 Layer 4 - Memory + MCP.
 *
 * States the scope limits PLAINLY (MASTER_BUILD_PROMPT §5.6): the code graph is
 * Python-only, retrieval is keyword and structural rather than semantic, and
 * there is no tokenizer. Those are not softened - claiming otherwise would be
 * a fabrication, and the limits are on the page as prominently as the results.
 */
export function LayerMemory() {
  const pctCalls = Math.round(
    (1 - MEMORY_ABLATION.calls.on / MEMORY_ABLATION.calls.off) * 100
  );
  const pctTokens = Math.round(
    (1 - MEMORY_ABLATION.tokens.on / MEMORY_ABLATION.tokens.off) * 100
  );

  return (
    <Section id="memory" labelledBy="mem-h" tone="ink">
      <div className="grid gap-12 lg:grid-cols-12 lg:gap-14">
        <div className="min-w-0 lg:col-span-5">
          <Reveal>
            <div className="mb-4 flex items-baseline gap-3">
              <span className="tnum font-mono text-mono text-gilt">04</span>
              <code className="font-mono text-mono text-soot">
                memory/, mcp_server/
              </code>
            </div>
            <h2 id="mem-h" className="mb-5 max-w-[16ch] text-h2 text-quench">
              It remembers what went wrong last time.
            </h2>
          </Reveal>
          <Reveal delay={60}>
            <p className="mb-6 max-w-[54ch] text-lead text-ash">
              A tree-sitter code graph and a SQLite decision store. The planner
              queries the store for decisions recorded against this repo before
              it plans, so a mistake made once does not have to be made again.
            </p>
          </Reveal>

          <Reveal delay={110}>
            <Panel tone="char" className="p-5">
              <div className="mb-4 font-mono text-mono text-soot">
                measured, n=5 × 1 rep — directional only
              </div>
              <dl className="grid grid-cols-3 gap-4">
                <div>
                  <dt className="font-mono text-mono text-soot">calls</dt>
                  <dd className="tnum font-mono text-monolg text-quench">
                    {MEMORY_ABLATION.calls.off} → {MEMORY_ABLATION.calls.on}
                  </dd>
                  <dd className="font-mono text-mono text-gilt">
                    −{pctCalls}%
                  </dd>
                </div>
                <div>
                  <dt className="font-mono text-mono text-soot">tokens</dt>
                  <dd className="tnum font-mono text-monolg text-quench">
                    −{pctTokens}%
                  </dd>
                  <dd className="font-mono text-mono text-soot">
                    {MEMORY_ABLATION.tokens.off.toLocaleString()} →{" "}
                    {MEMORY_ABLATION.tokens.on.toLocaleString()}
                  </dd>
                </div>
                <div>
                  <dt className="font-mono text-mono text-soot">
                    repeated mistakes
                  </dt>
                  <dd className="tnum font-mono text-monolg text-verdant-bright">
                    {MEMORY_ABLATION.recurrences.off} →{" "}
                    {MEMORY_ABLATION.recurrences.on}
                  </dd>
                </div>
              </dl>
              <p className="mt-4 border-t border-rule pt-3 text-small text-smoke">
                {MEMORY_ABLATION.note}
              </p>
            </Panel>
          </Reveal>
        </div>

        <div className="min-w-0 lg:col-span-7">
          <Reveal>
            <div className="mb-3 flex items-baseline justify-between">
              <h3 className="text-h3 text-quench">Five MCP tools, over stdio</h3>
              <span className="font-mono text-mono text-soot">
                any MCP client
              </span>
            </div>
          </Reveal>

          <ul className="mb-6 grid gap-px overflow-hidden rounded-lg border border-rule bg-rule">
            {MCP_TOOLS.map((t, i) => (
              <Reveal key={t.name} delay={i * 45} as="li">
                <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 bg-slab px-5 py-3.5">
                  <code className="font-mono text-monolg text-gilt-bright">
                    {t.name}
                  </code>
                  <span className="text-small text-ash">{t.summary}</span>
                </div>
              </Reveal>
            ))}
          </ul>

          <Reveal delay={80}>
            <p className="mb-6 max-w-[62ch] text-body text-ash">
              {MCP_CLIENT_NOTE.text}
            </p>
          </Reveal>

          <Reveal delay={120}>
            <div className="rounded-lg border border-rule border-l-2 border-l-smoke bg-basalt p-5">
              <h4 className="mb-3 font-mono text-mono text-smoke">
                What it does not do
              </h4>
              <ul className="flex flex-col gap-2">
                {MEMORY_LIMITS.map((l) => (
                  <li key={l} className="flex gap-3 text-small text-ash">
                    <span
                      aria-hidden="true"
                      className="mt-2.5 h-px w-3 shrink-0 bg-soot"
                    />
                    {l}
                  </li>
                ))}
              </ul>
            </div>
          </Reveal>
        </div>
      </div>
    </Section>
  );
}
