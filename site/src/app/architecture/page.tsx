import type { Metadata } from "next";
import { PageHeader } from "@/components/primitives/PageHeader";
import { Section } from "@/components/primitives/Section";
import { Reveal } from "@/components/motion/Reveal";
import { Panel } from "@/components/primitives/Panel";
import { LoopDiagram } from "@/components/product/LoopDiagram";
import { SANDBOX_FLAGS, SANDBOX_ADVERSARIAL } from "@/lib/content/sandbox";
import { MCP_TOOLS, MEMORY_LIMITS, MCP_CLIENT_NOTE } from "@/lib/content/mcp";

export const metadata: Metadata = {
  title: "Architecture",
  description:
    "The four layers of vex — harness, execution, runtime, and memory — and the contracts between them.",
};

/**
 * /architecture — the four layers in depth.
 *
 * Structured as one section per layer, each stating what it owns, what it
 * guarantees, and what it deliberately does not do. The "does not" parts are
 * as prominent as the rest: a reader deciding whether to adopt this needs the
 * limits more than the features.
 */

const LAYERS = [
  {
    n: "01",
    name: "Harness",
    dir: "harness/",
    owns: "The agent loop itself: planning, step execution, and the verifier gate.",
    detail:
      "Bash-only, with a fresh session per step and the task constraints re-injected each time. The original repository is never touched — the harness snapshots it, works on the copy, and diffs the two on the host. On a verified fix it writes a branch, a commit, a PR description and a rationale grounded in the actual trace.",
    guarantees: [
      "Success is claimed only when the target test passes and the suite shows no regressions",
      "Checkpoint and resume at step granularity, surviving hard kills",
      "Protected paths: the agent cannot reach .git or the test files",
      "RECALL pulls older detail back from the task trace on demand",
    ],
    limits: [
      "Failure classification is deliberately not implemented — the chosen novel mechanism was routing, not repair strategy",
      "The state machine in harness/state_machine.py is a designed contract, not wired into the running loop",
    ],
  },
  {
    n: "02",
    name: "Execution",
    dir: "execution/",
    owns: "The Docker sandbox and the stateless verifier.",
    detail:
      "Every command gets a fresh container. Verification is a stateless evaluation of one repository state: the target test runs, then the full suite, with a three-valued outcome so a timeout is distinguishable from a failure. Flake detection reruns the target and flags differing outcomes.",
    guarantees: SANDBOX_FLAGS.map((f) => `${f.label} — ${f.detail}`),
    limits: [
      "The read-write bind mount lets a container write host disk unquota'd. There is no Docker primitive for bind-mount quotas, so it is inherent to the contract that lets the harness diff on the host",
      "When Docker is unavailable the sandbox raises rather than silently running unsandboxed",
    ],
  },
  {
    n: "03",
    name: "Runtime",
    dir: "runtime/",
    owns: "Scheduling, checkpointing, the approval gate, and adaptive model routing.",
    detail:
      "One process per task, concurrency-capped, with wall-clock and hang supervision and a per-task crash budget. The router predicts difficulty per call from the issue text and from live struggle signals in the conversation tail, then routes to a cheap or expensive tier accordingly.",
    guarantees: [
      "Proven at 45 concurrent tasks with 8 simultaneous mid-run hard kills",
      "Two-authority checkpointing: the harness owns progress, the runtime owns whether a relaunch is a resume",
      "Every model call lands in a per-task JSONL cost ledger",
      "Escalation never sticks — routing drops back to cheap unless struggle persists",
    ],
    limits: [
      "Budget caps are enforced at attempt granularity, so a cap can overshoot by one attempt",
      "Costs are computed from published price rates, not from bills",
    ],
  },
  {
    n: "04",
    name: "Memory + MCP",
    dir: "memory/, mcp_server/",
    owns: "The code graph, the decision store, and the MCP surface.",
    detail:
      "A tree-sitter code graph and a SQLite decision store that auto-ingests every task's structured state. The planner queries the store for decisions recorded against the current repository before it plans, so a documented mistake is not repeated.",
    guarantees: MCP_TOOLS.map((t) => `${t.name} — ${t.summary}`),
    limits: MEMORY_LIMITS as unknown as string[],
  },
];

export default function ArchitecturePage() {
  return (
    <>
      <PageHeader
        eyebrow="Architecture"
        title="Four layers, wired deliberately."
        lead="Each layer exists elsewhere in some form. What does not exist elsewhere is the four of them agreeing before anything is called done."
      />

      <Section tone="ink">
        <Reveal>
          <h2 className="mb-8 max-w-[18ch] text-h2 text-quench">
            The loop, end to end.
          </h2>
        </Reveal>
        <Reveal delay={60}>
          <LoopDiagram />
        </Reveal>
      </Section>

      {LAYERS.map((l, idx) => (
        <Section
          key={l.name}
          id={l.name.toLowerCase().replace(/[^a-z]/g, "")}
          tone={idx % 2 === 0 ? "basalt" : "ink"}
        >
          <div className="grid gap-10 lg:grid-cols-12 lg:gap-14">
            <div className="min-w-0 lg:col-span-5">
              <Reveal>
                <div className="mb-4 flex items-baseline gap-3">
                  <span className="tnum font-mono text-mono text-ox-bright">
                    {l.n}
                  </span>
                  <code className="font-mono text-mono text-smoke">{l.dir}</code>
                </div>
                <h2 className="mb-5 text-h2 text-quench">{l.name}</h2>
                <p className="mb-4 max-w-[52ch] text-lead text-ash">{l.owns}</p>
                <p className="max-w-[54ch] text-body text-ash">{l.detail}</p>
              </Reveal>
            </div>

            <div className="flex min-w-0 flex-col gap-5 lg:col-span-7">
              <Reveal delay={80}>
                <Panel className="p-6">
                  <h3 className="mb-4 font-mono text-mono text-verdant">
                    What it guarantees
                  </h3>
                  <ul className="flex flex-col gap-3">
                    {l.guarantees.map((g) => (
                      <li key={g} className="flex gap-3 text-small text-ash">
                        <span
                          aria-hidden="true"
                          className="mt-2.5 h-px w-4 shrink-0 bg-verdant/50"
                        />
                        <span>{g}</span>
                      </li>
                    ))}
                  </ul>
                </Panel>
              </Reveal>

              <Reveal delay={130}>
                <div className="rounded-lg border border-rule border-l-2 border-l-smoke bg-char p-6">
                  <h3 className="mb-4 font-mono text-mono text-smoke">
                    What it does not do
                  </h3>
                  <ul className="flex flex-col gap-3">
                    {l.limits.map((g) => (
                      <li key={g} className="flex gap-3 text-small text-ash">
                        <span
                          aria-hidden="true"
                          className="mt-2.5 h-px w-4 shrink-0 bg-soot"
                        />
                        <span>{g}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              </Reveal>
            </div>
          </div>
        </Section>
      ))}

      <Section tone="basalt">
        <div className="grid gap-10 lg:grid-cols-12 lg:gap-14">
          <Reveal className="min-w-0 lg:col-span-6">
            <h2 className="mb-5 max-w-[18ch] text-h2 text-quench">
              A client as well as a server.
            </h2>
            <p className="max-w-[54ch] text-body text-ash">
              {MCP_CLIENT_NOTE.text}
            </p>
          </Reveal>
          <Reveal delay={80} className="min-w-0 lg:col-span-6">
            <Panel tone="char" className="p-6">
              <h3 className="mb-3 font-mono text-mono text-ox-bright">
                Adversarial result
              </h3>
              <div className="mb-3 flex flex-wrap gap-x-6 gap-y-1">
                <span className="font-mono text-mono text-verdant-bright">
                  {SANDBOX_ADVERSARIAL.sequential}
                </span>
                <span className="font-mono text-mono text-verdant-bright">
                  {SANDBOX_ADVERSARIAL.concurrent}
                </span>
              </div>
              <p className="max-w-[58ch] text-small text-ash">
                {SANDBOX_ADVERSARIAL.detail}
              </p>
            </Panel>
          </Reveal>
        </div>
      </Section>
    </>
  );
}
