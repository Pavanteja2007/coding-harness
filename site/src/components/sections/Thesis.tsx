import { Reveal } from "@/components/motion/Reveal";
import { Section } from "@/components/primitives/Section";

/**
 * §4 The thesis - "The integration is the point."
 *
 * The four layers as an asymmetric offset stack, explicitly NOT a bento grid of
 * four equal cards (DESIGN.md §5 ban list). Each layer is indented further than
 * the last and joined by a hairline, so the eye reads them as a stack that was
 * assembled rather than four features that were listed.
 *
 * Layer names and descriptions: README.md:37-42.
 */
const LAYERS = [
  {
    n: "01",
    name: "Harness",
    dir: "harness/",
    body: "Planner, step agent, and the verifier gate. Snapshots the repo, diffs pristine against work, and writes git-native output plus a grounded rationale on success.",
  },
  {
    n: "02",
    name: "Execution",
    dir: "execution/",
    body: "A fresh Docker container per command: read-only rootfs, no network, capabilities dropped, memory and pid limits. Stateless three-valued verification with flake detection.",
  },
  {
    n: "03",
    name: "Runtime",
    dir: "runtime/",
    body: "Process-per-task scheduler, checkpoint and resume across hard kills, an approval gate, and the adaptive model router with a per-call cost ledger.",
  },
  {
    n: "04",
    name: "Memory + MCP",
    dir: "memory/, mcp_server/",
    body: "A tree-sitter code graph and a SQLite decision store, exposed as five MCP tools over stdio — and consumed by the planner before it plans.",
  },
];

export function Thesis() {
  return (
    <Section id="thesis" labelledBy="thesis-h" tone="ink">
      <div className="grid gap-12 lg:grid-cols-12 lg:gap-16">
        <div className="min-w-0 lg:col-span-5">
          <Reveal>
            <h2 id="thesis-h" className="mb-6 max-w-[15ch] text-h2 text-quench">
              The integration is the point.
            </h2>
          </Reveal>
          <Reveal delay={60}>
            <p className="max-w-[52ch] text-lead text-ash">
              Any one of these layers exists elsewhere. What does not exist
              elsewhere is the four of them wired together so that a sandboxed
              edit, a real test suite, a crash-resumable scheduler, and a
              persistent memory all agree before anything is called done.
            </p>
          </Reveal>
        </div>

        <div className="min-w-0 lg:col-span-7">
          <ol className="relative">
            {LAYERS.map((l, i) => (
              <Reveal
                key={l.name}
                delay={i * 70}
                as="li"
                className="relative border-t border-rule pt-6 pb-8 last:pb-0"
                // The offset is what makes this a STACK rather than a grid.
                {...({ style: { marginLeft: `${i * 22}px` } } as object)}
              >
                <div className="flex items-baseline gap-4">
                  <span className="tnum font-mono text-mono text-ox-bright">
                    {l.n}
                  </span>
                  <h3 className="text-h3 text-quench">{l.name}</h3>
                  <code className="ml-auto font-mono text-mono text-soot">
                    {l.dir}
                  </code>
                </div>
                <p className="mt-3 max-w-[62ch] text-body text-ash">{l.body}</p>
              </Reveal>
            ))}
          </ol>
        </div>
      </div>
    </Section>
  );
}
