/**
 * Docs content.
 *
 * Held as structured data rather than MDX: the docs are small enough that a
 * typed map is simpler to verify, and it keeps the CLI surface in one place
 * where it can be checked against pyproject.toml.
 *
 * EVERY command here reflects what the repository actually ships:
 *   - the console script is `neo`, with `harness` kept as a working alias
 *     (pyproject.toml [project.scripts])
 *   - the package is published as `neo-agent-cli`; `pip install neo` would
 *     install an unrelated project, so it is never written here
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
  "Guides",
  "Concepts",
  "Reference",
] as const;

export const DOCS: DocPage[] = [
  // ---------------------------------------------------------- installing --
  {
    slug: "install",
    section: "Getting started",
    title: "Install",
    summary: "One pip command, or an editable install from a clone if you are working on neo itself.",
    blocks: [
      {
        type: "note",
        tone: "note",
        title: "The package name is not the command name",
        text: "Install neo-agent-cli; the command you get is neo. The short name was already taken on PyPI by an unrelated project, so the distribution carries the longer name - the same relationship beautifulsoup4 has with bs4.",
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
        lines: ["pip install neo-agent-cli"],
      },
      {
        type: "p",
        text: "That puts a `neo` command on your PATH. A legacy `harness` alias is kept working and maps to the same entry point, so older notes and scripts continue to run.",
      },
      { type: "h", text: "From source" },
      {
        type: "p",
        text: "Install from a clone when you want to work on neo itself, or run a revision that has not been released.",
      },
      {
        type: "code",
        lang: "bash",
        lines: [
          "git clone https://github.com/Pavanteja2007/coding-harness",
          "cd coding-harness",
          "pip install -e .",
        ],
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
        text: "neo takes a repository and a bug report in plain words. It plans a fix, edits inside a Docker sandbox, and runs your real test suite. It reports success only when the target test passes and the full suite shows no regressions.",
      },
      { type: "h", text: "Run a fix" },
      {
        type: "code",
        lang: "bash",
        lines: [
          "export MY_KEY=...   # your OpenAI-compatible router key",
          "",
          "neo fix \\",
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
        text: "On a verified fix, neo writes several artefacts under the task's log directory. The rationale is the one worth reading first — it states what was wrong, what changed, and how it was verified, grounded in the actual trace rather than generated after the fact.",
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
          "neo status --task-id <task_id>",
          "neo dashboard --logs-dir logs/",
        ],
      },
      {
        type: "note",
        tone: "note",
        title: "If it fails, it says so",
        text: "A failed verification is reported as a failure, and no branch, commit or PR description is produced - an unverified diff never gets git output. A rationale IS still written, because the grounded account of what happened is worth as much on a failure as on a win.",
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
        text: "The console script is `neo`. A `harness` alias is kept working and maps to the same entry point. Both are declared in pyproject.toml under [project.scripts], and everything also runs as `python -m cli`.",
      },
      {
        type: "table",
        head: ["Command", "What it does"],
        rows: [
          ["neo fix", "Fix one bug in one repository, end to end"],
          ["neo run-benchmark", "Run a task subset across N supervised agents"],
          ["neo status --task-id <id>", "Structured progress for one task"],
          ["neo dashboard", "Read-only web view over existing logs"],
          ["neo memory query-decisions", "Query the persistent decision store"],
          ["neo mcp call", "Call a tool on any external MCP server"],
          ["neo mcp list-tools", "List tools exposed by an MCP server"],
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
          ["2", "Usage or configuration error"],
          ["3", "Environment error (for example, Docker unavailable)"],
          ["4", "Model or network error"],
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
      { type: "h", text: "neo as a client" },
      {
        type: "p",
        text: "The relationship runs both ways: neo consumes external stdio MCP servers too, so outside tooling and the memory layer meet on the same protocol.",
      },
      {
        type: "code",
        lang: "bash",
        lines: [
          "neo mcp list-tools \"python -m mcp_server\"",
          "neo mcp call \"python -m mcp_server\" query_decisions --args '{\"query\": \"pytest\"}'",
        ],
      },
      { type: "h", text: "Scope limits" },
      {
        type: "list",
        items: [
          "The code graph indexes Python, JavaScript, and TypeScript",
          "Retrieval is keyword and structural, not semantic or embedding-based",
          "Context is bounded by file and line counts — there is no tokenizer",
          "The transport is stdio only",
        ],
      },
    ],
  },

  // ------------------------------------------------------------ first fix --
  {
    slug: "first-fix",
    section: "Getting started",
    title: "Your first fix",
    summary: "Fix one bug against the bundled fixture repo, then read every artefact the run leaves behind.",
    blocks: [
      {
        type: "p",
        text: "The repository ships a fixture repo with one real bug in it: `mean()` in mathutil.py returns the sum instead of the arithmetic mean. It is the shortest honest path to a verified fix, because the bug, the test that catches it, and the suite around it all already exist.",
      },
      { type: "h", text: "Run one fix" },
      {
        type: "code",
        lang: "bash",
        lines: [
          "export MY_KEY=...   # your OpenAI-compatible router key",
          "",
          "neo fix \\",
          "  --repo cli/fixtures/smoke_repo \\",
          "  --issue \"mean() in mathutil.py returns the sum, not the average. Fix it so tests/test_mathutil.py::test_mean passes.\" \\",
          "  --provider openai \\",
          "  --model <model> \\",
          "  --api-key $MY_KEY \\",
          "  --api-base <base-url>",
        ],
      },
      {
        type: "p",
        text: "The repository is snapshotted first, so the copy you pointed at is never edited. Artefacts land under `logs/<task_id>/`, with the runtime's own bookkeeping in the sibling `logs/<task_id>.runtime/`. Pass `--log-root` to put them somewhere else.",
      },
      {
        type: "table",
        head: ["Artefact", "What it holds"],
        rows: [
          ["rationale.md", "One paragraph: what was wrong, what changed, how it ended"],
          ["git.json", "branch, commit_sha, commit_message, pr_description"],
          ["state.json", "Plan, completed steps, files touched, decisions"],
          ["trace.jsonl", "One JSON object per event: prompts, responses, tool calls, verify results"],
          ["model_ledger.jsonl", "Per call: model, tokens, cost, difficulty hint"],
        ],
      },
      { type: "h", text: "How to read rationale.md" },
      {
        type: "p",
        text: "The rationale is assembled from the trace, not written by a second model call. That is the point: it cannot drift from what happened, because there is no generation step between the trace and the paragraph. Nothing the trace does not support appears in it.",
      },
      {
        type: "list",
        items: [
          "The failing test the baseline run found, with a short excerpt of its error",
          "The files the fix touched, read from state.json",
          "The decisions the harness recorded while working",
          "A closing verdict, and how many attempts it took",
        ],
      },
      {
        type: "note",
        tone: "note",
        title: "Failed runs get a rationale too",
        text: "A failed or timed-out task still writes rationale.md, because the account of why a run ended is worth as much as the account of why it succeeded. What a failed run does not get is git output: an unverified diff never earns a branch, a commit, or a PR description.",
      },
    ],
  },

  // ---------------------------------------------------------- a real bug --
  {
    slug: "real-bug",
    section: "Guides",
    title: "Fixing a real bug",
    summary: "Writing an issue that carries evidence, choosing the target test, and what to do with a failure.",
    blocks: [
      {
        type: "p",
        text: "A fixture run proves the plumbing works. A real repository adds two inputs that decide the outcome: how well the bug is described, and which test is treated as the target.",
      },
      { type: "h", text: "Write the issue like a bug report" },
      {
        type: "p",
        text: "The issue text is evidence, not instructions. Say what is observably wrong and where; the planner reads it to decompose the fix. It is also the source of the intrinsic difficulty signal, so padding it with urgency or a long stack trace over a one-line bug changes routing without helping the fix. If the report is long, pass a file instead: `--issue @bug.txt`.",
      },
      {
        type: "list",
        items: [
          "The symptom, in terms of what the code returns or raises",
          "Where it shows up — the module or function, if you know it",
          "What the correct behaviour would be",
          "The test that demonstrates it",
        ],
      },
      { type: "h", text: "Choose the target test" },
      {
        type: "p",
        text: "`--target-test` takes a pytest node id, and it is what the gate measures. The suite command is autodetected; `--test-command` overrides it when the project needs something specific.",
      },
      {
        type: "code",
        lang: "bash",
        lines: [
          "neo fix \\",
          "  --repo ../your-project \\",
          "  --issue @bug.txt \\",
          "  --target-test \"tests/test_parser.py::test_trailing_comma\" \\",
          "  --test-command \"python -m pytest -q\" \\",
          "  --budget 2.0",
        ],
      },
      {
        type: "note",
        tone: "note",
        title: "The baseline runs first",
        text: "Before any edit, the target test runs against a pristine copy. If it already passes there, the run ends rather than manufacturing a change — which also catches a mistyped node id early, before any model spend.",
      },
      { type: "h", text: "When it fails" },
      {
        type: "list",
        items: [
          "Read rationale.md first — it states how the run ended and what it touched",
          "`neo status --task-id <id>` for the plan checklist and recorded decisions",
          "trace.jsonl for the verify output the gate actually rejected",
          "The exit code is 1, and no branch or commit exists; a refused claim is the gate working, not a bug",
        ],
      },
    ],
  },

  // -------------------------------------------------------- byo provider --
  {
    slug: "byo-provider",
    section: "Guides",
    title: "Bring your own provider",
    summary: "Any OpenAI-compatible endpoint, your own key, and the settings file that saves you retyping both.",
    blocks: [
      {
        type: "p",
        text: "There is no bundled model and no hosted endpoint. Model calls go through litellm, so any OpenAI-compatible endpoint works with any model name that endpoint serves.",
      },
      { type: "h", text: "Point it at an endpoint" },
      {
        type: "code",
        lang: "bash",
        lines: [
          "neo fix \\",
          "  --repo . \\",
          "  --issue \"...\" \\",
          "  --provider openai \\",
          "  --model <model> \\",
          "  --api-key $MY_KEY \\",
          "  --api-base https://my-router.example.com/v1",
        ],
      },
      {
        type: "p",
        text: "`--base-url` is accepted as an alias of `--api-base`. When an endpoint is set and no provider is given, the provider defaults to `openai`, which is the right dialect for an OpenAI-compatible router; an explicit `--provider` always wins.",
      },
      { type: "h", text: "The settings file" },
      {
        type: "table",
        head: ["Key", "What it sets"],
        rows: [
          ["model", "Model name to send to the provider"],
          ["provider", "litellm provider name"],
          ["budget_cap_usd", "Hard cap on model spend per task (default 2.0)"],
          ["max_retries", "Full attempts at the whole task (default 3)"],
          ["plan_preview", "Show the plan before it runs, in interactive mode"],
          ["log_verbosity", "\"quiet\" suppresses the interactive session's chatter"],
          ["log_root", "Where task logs and sessions are written (default ./logs)"],
        ],
      },
      {
        type: "code",
        lang: "bash",
        lines: [
          "neo config set base_url https://my-router.example.com/v1",
          "neo config set model <model>",
          "neo config list     # effective values, and which tier each came from",
          "neo config path     # where the files live on this machine",
        ],
      },
      { type: "h", text: "Precedence" },
      {
        type: "list",
        items: [
          "Explicit CLI flags",
          "Environment: NEO_MODEL, NEO_PROVIDER, NEO_BASE_URL, NEO_API_BASE, NEO_API_KEY",
          "Project `<repo>/.neo/settings.local.toml`, then `<repo>/.neo/settings.toml`",
          "The global settings.toml — %APPDATA%\\neo\\ on Windows, ~/.config/neo/ elsewhere",
          "The legacy ~/.neo/config.toml, read only when the global file is missing",
          "Built-in defaults",
        ],
      },
      {
        type: "note",
        tone: "warn",
        title: "Keep keys out of the project file",
        text: "`.neo/settings.toml` is meant to be committed, so it is the wrong place for a key. Use an environment variable, the global file, or `.neo/settings.local.toml` — which neo adds to the repo's .gitignore when it creates it. A broken settings file is reported once on stderr and then ignored, rather than crashing the CLI.",
      },
    ],
  },

  // ----------------------------------------------------------- multi repo --
  {
    slug: "multi-repo",
    section: "Guides",
    title: "Running across repositories",
    summary: "Fan a task set out through the scheduler, one supervised agent per bug.",
    blocks: [
      {
        type: "p",
        text: "`neo run-benchmark` takes a set of tasks and runs them through the scheduler, one process per task. Each task is a separate repository, a separate sandbox, and a separate log directory; the gate applies to each one independently.",
      },
      { type: "h", text: "Run a subset" },
      {
        type: "code",
        lang: "bash",
        lines: [
          "neo run-benchmark --subset tasks.json --concurrency 4",
          "",
          "# plumbing check: one bundled fixture task, no key needed",
          "neo run-benchmark --subset smoke",
        ],
      },
      {
        type: "p",
        text: "`--subset` is either the literal `smoke` or a path to a JSON file. `--concurrency` caps how many run at once and defaults to 10. The run ends with per-task outcomes and a summary line of success, failed, and error/timeout counts with total cost.",
      },
      { type: "h", text: "The subset file" },
      {
        type: "code",
        lang: "json",
        lines: [
          "[",
          "  {",
          "    \"repo\": \"../more-itertools\",",
          "    \"issue\": \"chunked() drops the final partial chunk.\",",
          "    \"target_test\": \"tests/test_more.py::ChunkedTests\"",
          "  },",
          "  {",
          "    \"repo\": \"../python-semver\",",
          "    \"issue\": \"compare() orders prerelease versions incorrectly.\",",
          "    \"target_test\": \"tests/test_semver.py::test_compare\",",
          "    \"task_id\": \"semver-compare\"",
          "  }",
          "]",
        ],
      },
      {
        type: "p",
        text: "The file must be a JSON list of objects. `repo` and `issue` are required and must be non-empty strings; `target_test`, `test_command`, `task_id` and a `config` object are optional. A malformed entry is a usage error — exit code 2, naming the entry index — rather than a traceback from somewhere inside the scheduler. `--model` and `--provider` on the command line apply to every task in the set.",
      },
      { type: "h", text: "Where the logs go" },
      {
        type: "code",
        lines: [
          "logs/",
          "  <task_id>/",
          "    state.json       plan, completed steps, files touched, decisions",
          "    trace.jsonl      every prompt, response, tool call, verify result",
          "    rationale.md     the grounded paragraph",
          "    git.json         branch + commit + PR description (verified fixes only)",
          "  <task_id>.runtime/",
          "    model_ledger.jsonl   per call: model, tokens, cost, hint",
          "    checkpoint.json      the resume point",
        ],
      },
      {
        type: "note",
        tone: "note",
        title: "Interruptions resume",
        text: "The scheduler and its workers share one log tree, and each task checkpoints as it completes steps. A task killed mid-run resumes from its completed steps rather than starting over, with every artefact under the `--log-root` you asked for.",
      },
    ],
  },

  // ------------------------------------------------------------ dashboard --
  {
    slug: "dashboard",
    section: "Guides",
    title: "The dashboard",
    summary: "A read-only web view over logs a run already wrote.",
    blocks: [
      {
        type: "p",
        text: "The dashboard reads existing logs and renders them. It is not a control plane: it starts nothing, changes nothing, and knows nothing a log directory does not already contain. Point it at a finished run, or watch one in progress.",
      },
      { type: "h", text: "Serve it" },
      {
        type: "code",
        lang: "bash",
        lines: [
          "neo dashboard --logs-dir logs/",
          "# then open http://127.0.0.1:8765",
        ],
      },
      {
        type: "table",
        head: ["Flag", "Default"],
        rows: [
          ["--logs-dir", "./logs"],
          ["--host", "127.0.0.1"],
          ["--port", "8765"],
          ["--refresh-s", "5.0"],
          ["--no-browser", "Do not open a browser window"],
        ],
      },
      { type: "h", text: "What it shows" },
      {
        type: "list",
        items: [
          "Task status, attempts, and elapsed time",
          "Per-task cost, model calls, and which models were used",
          "The distribution of difficulty hints the router assigned",
          "Tasks grouped by run, so benchmark and ablation trees read as runs rather than a flat list",
        ],
      },
      { type: "h", text: "Read-only by construction" },
      {
        type: "list",
        items: [
          "The handler implements GET only: / for the page, /api/tasks for the JSON behind it",
          "POST, PUT and DELETE are answered 405",
          "Any other path is a 404 — there is no file serving",
          "It is the standard library's HTTP server; the dashboard adds no dependency",
        ],
      },
      {
        type: "note",
        tone: "note",
        title: "It tolerates half-written files",
        text: "Scanning happens while runs are still writing, so a missing or malformed file degrades to a placeholder — status unknown, cost zero — instead of a traceback. A dashboard that crashed on a partial log would be useless exactly when you want it.",
      },
    ],
  },

  // --------------------------------------------------------- four layers --
  {
    slug: "four-layers",
    section: "Concepts",
    title: "The four layers",
    summary: "Harness, execution, runtime and memory — what each owns, and the contracts that hold them together.",
    blocks: [
      {
        type: "p",
        text: "neo is four modules built against fixed contracts rather than one program. The harness runs the agent loop, execution owns the sandbox and the verifier, the runtime schedules and routes, and memory records what was learned and exposes it over MCP. The integration is the point: no single layer fixes a bug on its own.",
      },
      {
        type: "table",
        head: ["Layer", "What it owns", "Where"],
        rows: [
          ["Harness", "Planner, step agent, verifier gate, repo snapshot and diff, git output, rationale, resume", "harness/"],
          ["Execution", "Docker sandbox — a fresh container per command — plus stateless verify and flake detection", "execution/"],
          ["Runtime", "Process-per-task scheduler, checkpoint and resume, approval gate, adaptive router, cost ledger", "runtime/"],
          ["Memory + MCP", "tree-sitter code graph, SQLite decision store, the MCP server and the MCP client", "memory/, mcp_server/"],
        ],
      },
      {
        type: "p",
        text: "Each layer was built as a separate workstream against boundaries recorded in INTERFACES.md, so a layer depends on its neighbour's documented signature rather than on its internals.",
      },
      { type: "h", text: "The contracts between them" },
      {
        type: "table",
        head: ["Boundary", "The contract", "What crosses it"],
        rows: [
          ["Harness to execution", "execute_sandboxed(repo_path, command, timeout_s)", "One command in one fresh container; an ExecutionResult back"],
          ["Harness to execution", "verify(repo_path, target_test, rerun_for_flake_check)", "A VerificationResult for one repo state"],
          ["Harness to runtime", "call_model(messages, difficulty_hint, provider, model, api_key)", "One model call, with an optional difficulty hint"],
          ["Runtime to harness", "run_task(task) -> TaskResult", "One whole task, called concurrently with checkpoints around it"],
          ["Harness to memory", "logs/<task_id>/state.json", "Plan, completed steps, files touched, decisions, repo path"],
          ["Memory to anyone", "Five MCP tools over stdio", "Code-graph and decision queries, for neo or any MCP client"],
        ],
      },
      { type: "h", text: "Why four and not one" },
      {
        type: "p",
        text: "Each layer exists because the verifier gate demands it. The gate needs a test result the agent cannot influence, which is what the sandbox provides. It needs a pristine baseline to compare against, which is why the harness snapshots and diffs instead of editing in place. Running many gated tasks at once and surviving a hard kill mid-run is what the runtime's checkpoints are for. And a gate that refuses a plausible-looking fix produces information worth keeping, which is what the decision store holds.",
      },
      {
        type: "list",
        items: [
          "verify() is stateless — it evaluates one repo state and knows nothing about attempts",
          "baseline_passed is always false from verify(); only the harness knows the pristine outcome, so it fills that field itself",
          "run_task is the contract the scheduler is built around — everything else in the runtime exists to call it safely",
          "The sandbox raises when Docker is down rather than falling back to running unsandboxed",
        ],
      },
      {
        type: "note",
        tone: "note",
        title: "The boundaries are additive",
        text: "state.json's six-key prefix is the stable part of the contract; later keys such as repo_path and change_groups were added after it, so every consumer is required to treat unknown keys as ignorable. That rule is what let four layers keep moving without breaking each other.",
      },
    ],
  },

  // ------------------------------------------------------ decision memory --
  {
    slug: "decision-memory",
    section: "Concepts",
    title: "Decision memory",
    summary: "A decision store and a code graph the planner reads before it plans, and the ablation that measured what that changed.",
    blocks: [
      {
        type: "p",
        text: "The memory layer is two stores: a SQLite decision store holding what previous tasks concluded, and a tree-sitter code graph holding a repository's structure. Both are scoped per repository, and both are read before planning rather than after — which is the only way they can change a plan.",
      },
      {
        type: "p",
        text: "Before the planner call, run_task asks the store what was recorded against this repository and injects the matches as a `## Relevant past decisions` section of the planner prompt. It sits after `## Retrieved context` and before `## Constraints`, deliberately past the point where the difficulty predictor stops reading, so recalled decisions never shift routing.",
      },
      {
        type: "list",
        items: [
          "The query runs in-process against the store, not over the MCP wire — same process tree, no server round-trip",
          "Search is scoped by repo path, so decisions recorded against other repositories are not injected",
          "The store auto-ingests each task's state.json decisions, so the record accumulates without being written by hand",
          "It is best-effort: a missing module or an unreadable store degrades to \"none recorded yet\" plus a trace event, never a failed plan",
        ],
      },
      { type: "h", text: "The code graph" },
      {
        type: "p",
        text: "The graph indexes functions, classes, methods, imports and call relationships, and persists to disk, so structural context rides into prompts without re-reading files. It is also what coordinated-change detection walks to find the callers and importers a signature change would break.",
      },
      { type: "h", text: "What the ablation measured" },
      {
        type: "p",
        text: "Five fixture tasks were seeded with decisions genuinely mined from earlier runs, then run twice against the same pinned model with adaptive routing off, so the arms differed only in plan_with_memory. Both arms succeeded on every task in one attempt: memory did not change whether these tasks were solvable. It changed what they cost.",
      },
      {
        type: "table",
        head: ["Measure", "Memory off", "Memory on"],
        rows: [
          ["Harness model calls", "35", "27"],
          ["Tokens", "147,916", "112,390"],
          ["Repeated past mistakes", "3", "0"],
        ],
      },
      {
        type: "note",
        tone: "warn",
        title: "Directional, not benchmark-grade",
        text: "n=5, one repetition. The numbers point one way and the mechanism is visible in the traces — all three off-arm recurrences were the same documented error class, re-tripped on separate tasks — but a five-task run is not a benchmark and is not quoted as one.",
      },
      { type: "h", text: "Scope limits" },
      {
        type: "list",
        items: [
          "The code graph indexes Python, JavaScript, and TypeScript",
          "Retrieval is keyword and structural, not semantic or embedding-based",
          "Context is bounded by file and line counts — there is no tokenizer",
          "Call edges are best-effort: Python is dynamic, so a bare call resolves to any known symbol of that name",
        ],
      },
    ],
  },

  // ------------------------------------------------------------- config --
  {
    slug: "config-keys",
    section: "Reference",
    title: "Configuration",
    summary: "The two-tier settings layout, the real precedence chain, and the task config keys the harness and runtime read.",
    blocks: [
      {
        type: "p",
        text: "Settings are TOML in two tiers: one global file per machine, and up to two files per project. Every file is optional — with none of them present, built-in defaults apply and flags still work.",
      },
      { type: "h", text: "Where settings live" },
      {
        type: "table",
        head: ["Tier", "Path"],
        rows: [
          ["Global (Windows)", "%APPDATA%\\neo\\settings.toml"],
          ["Global (POSIX)", "~/.config/neo/settings.toml, honouring XDG_CONFIG_HOME"],
          ["Global override", "The file named by $NEO_CONFIG always wins for this tier"],
          ["Project", "<repo>/.neo/settings.toml — meant to be committed"],
          ["Project-local", "<repo>/.neo/settings.local.toml — personal, added to the repo's .gitignore when neo creates it"],
          ["Legacy", "~/.neo/config.toml — read only when the global file is missing"],
        ],
      },
      {
        type: "p",
        text: "The project tier is found by walking up from the working directory for a `.neo/` directory. `neo config path` shows the resolved locations on this machine, and `neo config list` shows effective values with the tier each came from.",
      },
      { type: "h", text: "Precedence" },
      {
        type: "list",
        items: [
          "Explicit CLI flags, and the interactive session's own state",
          "Environment: NEO_MODEL, NEO_PROVIDER, NEO_BASE_URL, NEO_API_BASE, NEO_API_KEY",
          "Project `.neo/settings.local.toml`",
          "Project `.neo/settings.toml`",
          "The global settings.toml",
          "The legacy ~/.neo/config.toml, only when the global file is absent",
          "Built-in defaults",
        ],
      },
      { type: "h", text: "Task configuration keys" },
      {
        type: "p",
        text: "These are read per task by the harness and the runtime. Unknown keys pass through the settings layer untouched, so a key one module owns can still be set in any tier.",
      },
      {
        type: "table",
        head: ["Key", "What it does", "Default"],
        rows: [
          ["max_retries", "Full attempts at the whole task, inside run_task", "3"],
          ["budget_cap_usd", "Hard cap on model spend per task", "2.0"],
          ["adaptive_routing", "Enable the per-call difficulty router", "false"],
          ["model_tiers", "Per-tier {provider, model, api_key?, api_base?} the router targets", "Built-in tiers"],
          ["crash_retries", "Scheduler attempts after a worker crash or kill — not max_retries", "1"],
          ["resume", "Continue from a checkpoint when one exists", "true"],
          ["approval", "\"require\" parks the worker at the diff-approval gate", "Unset"],
          ["approval_timeout_s", "How long that gate may block; unset blocks forever", "Unset"],
          ["hang_heartbeat_stale_s", "Heartbeat age the scheduler treats as a hang", "30.0"],
          ["log_root", "Where task logs and sessions are written", "./logs"],
          ["plan_with_memory", "Query decision memory before planning", "true"],
          ["lint_gate", "Static pass over changed files before a verify cycle", "true"],
          ["docs_lookup_enabled", "Allow a step session's DOCS lookups", "true"],
          ["coordination_detect", "Detect multi-file fan-out at planning time", "true"],
        ],
      },
      {
        type: "note",
        tone: "warn",
        title: "Structured keys are hand-edited",
        text: "`neo config set` refuses structured values such as model_tiers — editing the file by hand is the supported path. A broken or BOM'd file is reported once on stderr and then ignored rather than crashing the CLI, and api_key is masked in `neo config list` output.",
      },
    ],
  },

  // -------------------------------------------------------- trace events --
  {
    slug: "trace-events",
    section: "Reference",
    title: "Trace events",
    summary: "The per-task trace.jsonl — every prompt, response, tool call and result, one JSON object per line.",
    blocks: [
      {
        type: "p",
        text: "Every task writes `logs/<task_id>/trace.jsonl`: one JSON object per line, appended and flushed as the run proceeds, so a task that crashes still leaves a readable trace. Each line carries a timestamp, a kind, and a data object.",
      },
      {
        type: "code",
        lang: "json",
        lines: [
          "{\"ts\": 1757740001.412, \"kind\": \"task_start\", \"data\": {\"task_id\": \"...\", \"repo_path\": \"...\", \"resumed\": false}}",
          "{\"ts\": 1757740009.880, \"kind\": \"plan\", \"data\": {\"plan\": [], \"resumed\": false}}",
          "{\"ts\": 1757740211.004, \"kind\": \"task_end\", \"data\": {\"status\": \"success\", \"attempt\": 1}}",
        ],
      },
      {
        type: "p",
        text: "The base stream is the model conversation and the tools it drove — model_request, model_response, tool_call and tool_result — bracketed by task_start and task_end. Each mechanism in the loop adds its own kinds on top of that.",
      },
      {
        type: "note",
        tone: "note",
        title: "Full record, compacted view, and the hook between them",
        text: "trace.jsonl is the permanent full-fidelity store; state.json is the compacted view — plan, completed steps, files touched, decisions. RECALL is the retrieval hook between them: a step session asks for older detail by terms, and the matching trace entries are re-injected into the live session. That is what makes the compaction reversible.",
      },
      { type: "h", text: "Planning and memory" },
      {
        type: "table",
        head: ["Event", "Recorded when"],
        rows: [
          ["task_start", "The run begins — task id, repo, issue text, resume flag, and the config with the key stripped"],
          ["baseline_verify", "The target test has run against the pristine copy, before any edit"],
          ["decision_memory", "Decision memory was queried for planning — query, matches, section size, or that it was skipped"],
          ["plan", "The parsed plan, and whether it was reused from a resume"],
          ["change_groups", "The plan declared atomic multi-file change groups"],
          ["coordination", "Structural fan-out detection ran over the code graph"],
        ],
      },
      { type: "h", text: "Inside a step session" },
      {
        type: "table",
        head: ["Event", "Recorded when"],
        rows: [
          ["recall", "A RECALL pulled older trace detail back into the session — query and match count"],
          ["batch_call", "A BATCH of read-only commands ran concurrently"],
          ["batch_rejected", "A BATCH was refused by the allowlist, naming the offending entry"],
          ["tool_error", "A failing tool result was classified into one of the stable error kinds"],
          ["lint_failed", "The static pass rejected changed files before a verify cycle was spent on them"],
          ["docs_lookup", "A DOCS lookup resolved documentation into the session"],
        ],
      },
      { type: "h", text: "Gates and outcome" },
      {
        type: "table",
        head: ["Event", "Recorded when"],
        rows: [
          ["coordination_gate_rejected", "A declared group had some but not all of its files changed"],
          ["coordination_rollback", "A group was rolled back together"],
          ["rationale", "The grounded paragraph was assembled from this trace"],
          ["git_output", "Branch, commit and PR description were produced — verified fixes only"],
          ["task_end", "The run ended, with its final status"],
        ],
      },
      {
        type: "note",
        tone: "note",
        title: "Kinds are additive",
        text: "New event kinds are added as mechanisms land, so anything reading a trace has to ignore kinds it does not recognise and skip malformed lines rather than fail. The readers in the repo work that way, which is why the dashboard renders a partial trace instead of crashing on one.",
      },
    ],
  },
];

export function docBySlug(slug: string) {
  return DOCS.find((d) => d.slug === slug);
}
