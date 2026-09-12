/**
 * Docs content.
 *
 * Held as structured data rather than MDX: the docs are small enough that a
 * typed map is simpler to verify, and it keeps the CLI surface in one place
 * where it can be checked against pyproject.toml.
 *
 * EVERY command here reflects what the repository actually ships:
 *   - the console script is `vex`, with `harness` kept as a working alias
 *     (pyproject.toml [project.scripts])
 *   - there is NO PyPI package, so the only install is a clone plus an
 *     editable install
 */

export type DocBlock =
  | { type: "p"; text: string }
  | { type: "h"; text: string }
  | { type: "code"; lang?: string; lines: string[] }
  | { type: "list"; items: string[] }
  | { type: "note"; tone: "note" | "warn"; title: string; text: string }
  | { type: "table"; head: string[]; rows: string[][] };

export type DocPage = {
  slug: string;
  section: string;
  title: string;
  summary: string;
  blocks: DocBlock[];
};

export const DOC_SECTIONS = [
  "Getting started",
  "Concepts",
  "Reference",
] as const;

export const DOCS: DocPage[] = [
  // ---------------------------------------------------------- installing --
  {
    slug: "install",
    section: "Getting started",
    title: "Install",
    summary: "Clone the repository and install it editable. There is no package to pip install.",
    blocks: [
      {
        type: "note",
        tone: "warn",
        title: "There is no published package",
        text: "vex is not on PyPI, so `pip install vex` will not work and is not a typo for anything. Clone the repository and install it in editable mode.",
      },
      { type: "h", text: "Requirements" },
      {
        type: "list",
        items: [
          "Python 3.10 or newer",
          "Docker, for the sandbox and for verification",
          "An OpenAI-compatible endpoint and key, for real model runs",
        ],
      },
      { type: "h", text: "Install" },
      {
        type: "code",
        lang: "bash",
        lines: [
          "git clone https://github.com/Pavanteja2007/coding-harness",
          "cd coding-harness",
          "pip install -e .",
        ],
      },
      {
        type: "p",
        text: "That puts a `vex` command on your PATH. A legacy `harness` alias is kept working and maps to the same entry point, so older notes and scripts continue to run.",
      },
      { type: "h", text: "Without installing" },
      {
        type: "p",
        text: "Every command also works as a module, which is useful in CI or when you would rather not modify the environment.",
      },
      { type: "code", lang: "bash", lines: ["python -m cli fix --repo . --issue \"...\""] },
      { type: "h", text: "Check it works" },
      {
        type: "p",
        text: "The offline demo needs no key and no network. It runs the real loop, the real verifier gate, and the real git output against a scripted model.",
      },
      { type: "code", lang: "bash", lines: ["python demo/run_demo.py"] },
    ],
  },

  // ----------------------------------------------------------- quickstart --
  {
    slug: "quickstart",
    section: "Getting started",
    title: "Quickstart",
    summary: "Fix your first bug end to end, and read the artefacts it leaves behind.",
    blocks: [
      {
        type: "p",
        text: "vex takes a repository and a bug report in plain words. It plans a fix, edits inside a Docker sandbox, and runs your real test suite. It reports success only when the target test passes and the full suite shows no regressions.",
      },
      { type: "h", text: "Run a fix" },
      {
        type: "code",
        lang: "bash",
        lines: [
          "export MY_KEY=...   # your OpenAI-compatible router key",
          "",
          "vex fix \\",
          "  --repo ./your-project \\",
          "  --issue \"test_mean fails: mean() returns the sum, not the average\" \\",
          "  --provider openai \\",
          "  --model <model> \\",
          "  --api-key $MY_KEY \\",
          "  --api-base <base-url>",
        ],
      },
      { type: "h", text: "Read what it produced" },
      {
        type: "p",
        text: "On a verified fix, vex writes several artefacts under the task's log directory. The rationale is the one worth reading first — it states what was wrong, what changed, and how it was verified, grounded in the actual trace rather than generated after the fact.",
      },
      {
        type: "table",
        head: ["Artefact", "What it holds"],
        rows: [
          ["rationale.md", "What was wrong, what changed, how it was verified"],
          ["git.json", "Branch name, commit SHA, and a PR description"],
          ["state.json", "Plan, completed steps, files touched, decisions"],
          ["trace.jsonl", "Every prompt, response, tool call and result"],
        ],
      },
      { type: "h", text: "Inspect progress" },
      {
        type: "code",
        lang: "bash",
        lines: [
          "vex status --task-id <task_id>",
          "vex dashboard --logs-dir logs/",
        ],
      },
      {
        type: "note",
        tone: "note",
        title: "If it fails, it says so",
        text: "A failed verification is reported as a failure. vex does not produce a branch, a commit, or a rationale for work that did not pass the gate.",
      },
    ],
  },

  // ------------------------------------------------------- verifier gate --
  {
    slug: "verifier-gate",
    section: "Concepts",
    title: "The verifier gate",
    summary: "Why success is a test result rather than a claim.",
    blocks: [
      {
        type: "p",
        text: "Most coding agents finish when the model decides it has finished. That is a claim about the work, produced by the same process that did the work. The verifier gate replaces it with an independent test.",
      },
      { type: "h", text: "Both conditions, not either" },
      {
        type: "p",
        text: "A task reaches success only when the target test passes AND the full suite shows no regressions. Passing the target test while breaking something else is a failure, which is the case that matters: it is exactly what a plausible-looking but wrong fix produces.",
      },
      { type: "h", text: "Three-valued verification" },
      {
        type: "p",
        text: "A test outcome is pass, fail, or timeout — not a boolean. Collapsing a timeout into a failure hides the worst case: a test that passed once and then hung would otherwise read as a stable pass. The target test is also rerun to detect flakiness, and differing outcomes across reruns are flagged.",
      },
      { type: "h", text: "Baseline first" },
      {
        type: "p",
        text: "Before any edit, the target test runs against a pristine copy. If it already passes there is nothing to fix, and the run ends rather than manufacturing a change.",
      },
      {
        type: "note",
        tone: "note",
        title: "The gate is why the numbers mean anything",
        text: "Every success rate quoted anywhere on this site is gate-passed. There is no partial credit and no self-assessment in the measurement path.",
      },
    ],
  },

  // ----------------------------------------------------- adaptive routing --
  {
    slug: "adaptive-routing",
    section: "Concepts",
    title: "Adaptive routing",
    summary: "Predicting per-call difficulty, and paying for the expensive model only when it is needed.",
    blocks: [
      {
        type: "p",
        text: "Most calls in an agent loop are not hard. Reading a file, running a test, applying a small edit — these do not need a frontier model. Adaptive routing predicts the difficulty of each call and sends easy work to a cheaper tier.",
      },
      { type: "h", text: "Two signals" },
      {
        type: "list",
        items: [
          "Intrinsic — extracted from the issue text itself, with the prompt scaffolding stripped out",
          "Struggle — read from the conversation tail: failing test output, turns burned without progress",
        ],
      },
      {
        type: "p",
        text: "The scaffolding-stripping matters more than it sounds. The first version of the predictor scored the whole message, but harness prompts are large by construction, so every call saturated at \"hard\" and the mechanism degenerated into always-expensive — costing about twice the baseline. That failure is what produced the current design.",
      },
      { type: "h", text: "Escalation does not stick" },
      {
        type: "p",
        text: "After an expensive call, routing drops back to the cheap tier unless the struggle signal persists. A single hard step does not condemn the rest of the task to the expensive tier.",
      },
      {
        type: "note",
        tone: "warn",
        title: "Costs are proxy rates, not bills",
        text: "Both endpoints used in the ablation report $0, so costs are computed from published price rates for comparable model classes. The delta between arms is a price-model delta, not an invoice.",
      },
    ],
  },

  // ------------------------------------------------------------- sandbox --
  {
    slug: "sandbox",
    section: "Concepts",
    title: "The sandbox",
    summary: "What the agent can reach, and what it cannot.",
    blocks: [
      {
        type: "p",
        text: "Every command the agent runs gets a fresh container. The repository is bind-mounted read-write, because the harness needs the edits to persist so it can diff them on the host. Everything else is closed.",
      },
      {
        type: "table",
        head: ["Setting", "Effect"],
        rows: [
          ["read-only rootfs", "The image layer cannot be modified"],
          ["--network none", "No egress unless a call explicitly allows it"],
          ["--cap-drop ALL", "Every Linux capability is dropped"],
          ["mem-limit", "Memory bombs are OOM-killed rather than starving the host"],
          ["pids-limit", "Fork bombs collapse at the cap"],
          ["fresh container", "One --rm container per command; no state carries over"],
        ],
      },
      { type: "h", text: "Verified adversarially" },
      {
        type: "p",
        text: "The sandbox was probed with deliberate attacks rather than assumed secure: escape attempts against host mounts, the PID namespace, the Docker socket and cross-container networking, plus resource exhaustion. All 24 sequential attacks held, as did 78 concurrent hostile runs.",
      },
      {
        type: "note",
        tone: "warn",
        title: "One honest limit",
        text: "The read-write bind mount lets a container write host disk without a quota. No Docker primitive exists for bind-mount quotas, so this is inherent to the contract that lets the harness diff on the host. It is recorded rather than hidden.",
      },
    ],
  },

  // --------------------------------------------------------- cli reference --
  {
    slug: "cli",
    section: "Reference",
    title: "CLI reference",
    summary: "Every command, with the entry point resolved against pyproject.toml.",
    blocks: [
      {
        type: "p",
        text: "The console script is `vex`. A `harness` alias is kept working and maps to the same entry point. Both are declared in pyproject.toml under [project.scripts], and everything also runs as `python -m cli`.",
      },
      {
        type: "table",
        head: ["Command", "What it does"],
        rows: [
          ["vex fix", "Fix one bug in one repository, end to end"],
          ["vex run-benchmark", "Run a task subset across N supervised agents"],
          ["vex status --task-id <id>", "Structured progress for one task"],
          ["vex dashboard", "Read-only web view over existing logs"],
          ["vex memory query-decisions", "Query the persistent decision store"],
          ["vex mcp call", "Call a tool on any external MCP server"],
          ["vex mcp list-tools", "List tools exposed by an MCP server"],
        ],
      },
      { type: "h", text: "Session control" },
      {
        type: "table",
        head: ["Flag", "Effect"],
        rows: [
          ["--continue", "Resume the most recent resumable session"],
          ["--resume <task_id>", "Resume a specific task"],
          ["--list-sessions", "Show resumable sessions"],
          ["--adaptive-routing", "Enable the adaptive router for this run"],
          ["--api-base <url>", "Point at a custom OpenAI-compatible endpoint"],
          ["--log-root <path>", "Write all artefacts under a custom root"],
        ],
      },
      { type: "h", text: "Exit codes" },
      {
        type: "table",
        head: ["Code", "Meaning"],
        rows: [
          ["0", "Success — the gate passed"],
          ["1", "Failure — the gate did not pass, or the run errored"],
          ["2", "Usage error"],
          ["130", "Interrupted (Ctrl+C); checkpoints are kept"],
        ],
      },
    ],
  },

  // --------------------------------------------------------- mcp reference --
  {
    slug: "mcp",
    section: "Reference",
    title: "MCP tools",
    summary: "Five tools over stdio, plus a client for consuming external servers.",
    blocks: [
      {
        type: "p",
        text: "The memory layer is exposed over MCP, so any MCP client can query the code graph and the decision store. Run the server with `python -m mcp_server` and connect over stdio.",
      },
      {
        type: "table",
        head: ["Tool", "What it returns"],
        rows: [
          ["query_structure", "Code graph lookups — functions, classes, calls, imports"],
          ["query_decisions", "Decision and pattern memory across tasks"],
          ["record_decision", "Writes a decision into the store"],
          ["task_status", "Structured state for one task"],
          ["list_repos", "Repositories the graph has indexed"],
        ],
      },
      {
        type: "note",
        tone: "note",
        title: "Exactly five",
        text: "This is the complete surface. If you find a sixth documented anywhere, it is out of date.",
      },
      { type: "h", text: "vex as a client" },
      {
        type: "p",
        text: "The relationship runs both ways: vex consumes external stdio MCP servers too, so outside tooling and the memory layer meet on the same protocol.",
      },
      {
        type: "code",
        lang: "bash",
        lines: [
          "vex mcp list-tools \"python -m mcp_server\"",
          "vex mcp call \"python -m mcp_server\" query_decisions --args '{\"query\": \"pytest\"}'",
        ],
      },
      { type: "h", text: "Scope limits" },
      {
        type: "list",
        items: [
          "The code graph is Python-only",
          "Retrieval is keyword and structural, not semantic or embedding-based",
          "Context is bounded by file and line counts — there is no tokenizer",
          "The transport is stdio only",
        ],
      },
    ],
  },
];

export function docBySlug(slug: string) {
  return DOCS.find((d) => d.slug === slug);
}
