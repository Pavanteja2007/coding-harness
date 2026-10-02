/**
 * Release history.
 *
 * Transcribed from CHANGELOG.md rather than invented, and gated against
 * `pyproject.toml`: `python -m scripts.docs_truth` fails when the newest
 * version here disagrees with the release source of truth. The site
 * describing a version the wheel does not contain is the specific lie this
 * ordering prevents, so each entry states what the PUBLIC release does and
 * does not contain rather than blurring the two.
 */

export type Release = {
  version: string;
  date: string;
  tagged: boolean;
  summary: string;
  groups: { title: string; items: string[] }[];
  source: string;
};

export const RELEASES: Release[] = [
  {
    version: "v0.3.0",
    date: "unreleased",
    tagged: false,
    summary:
      "Current source candidate. Not on PyPI: everything below is in this checkout and may not be in an installed wheel.",
    groups: [
      {
        title: "What the candidate adds",
        items: [
          "Reachable product surfaces: `neo -p \"sentence\"` for one-shot headless agent work, `neo -` for piped context, `neo serve` for the local agent server, and `neo acp` for editor integration over ACP v1.",
          "A `neo capabilities` probe that compares the runtime registry against what the installed distribution actually provides, so a thinner wheel reports itself instead of failing silently.",
          "A documentation truth gate (`python -m scripts.docs_truth`) that fails on site/doc version drift and on a feature-matrix claim with no test or report reference.",
        ],
      },
      {
        title: "Honest limits of this candidate",
        items: [
          "No Docker-backed verification lane and no live-provider lane was run for these surfaces. The evidence is a scripted model through the real engine, plus the real server and ACP handshakes.",
          "The agent server and ACP surfaces are new; the optional ACP filesystem, terminal, and MCP proxy methods are not implemented, and permission callbacks are supported rather than enforced by a policy.",
        ],
      },
    ],
    source: "CHANGELOG.md:8",
  },
  {
    version: "v0.2.0",
    date: "2026-09-21",
    tagged: true,
    summary:
      "The current published release. This is what `pip install neo-agent-cli` gives you today.",
    groups: [
      {
        title: "What the public release contains",
        items: [
          "The four layers end to end: planner, Docker sandbox, verifier-gated completion, scheduler, memory, and MCP.",
          "The product shell: full-screen TUI with a rich REPL fallback, sessions, checkpoints, resume, undo, approval mode, skills, plugins, and connectors.",
          "CLI citizenship: `--help`/`--version`, NO_COLOR, dynamic shell completions, self-update, uninstall, and a documented exit-code contract.",
        ],
      },
      {
        title: "What the public release does NOT contain",
        items: [
          "The packaged `agent_sdk`, `acp`, `integrations`, `recipes`, and `extensions` payload landed after 0.2.0 and is source-only in the 0.2.1 candidate.",
          "The `neo serve`, `neo acp`, `neo capabilities`, and `neo -p` surfaces do not exist in 0.2.0.",
        ],
      },
    ],
    source: "CHANGELOG.md:116",
  },
  {
    version: "v0.1.0",
    date: "2026-09-09",
    tagged: true,
    summary:
      "Initial tagged release: the full four-layer system, built and validated end to end.",
    groups: [
      {
        title: "The four layers",
        items: [
          "Harness — planner, step agent, and verifier-gated completion, with checkpoint/resume at step granularity and git-native output on verified fixes.",
          "Execution — Docker sandbox with a fresh container per command, read-only rootfs, no network, dropped capabilities, and three-valued verification with flake detection.",
          "Runtime — process-per-task scheduler proven at 45 concurrent tasks with 8 simultaneous mid-run kills, plus checkpoint/resume, an approval gate, and a per-call cost ledger.",
          "Memory + MCP — tree-sitter code graph and SQLite decision store, exposed as five MCP tools over stdio, plus a client for consuming external servers.",
        ],
      },
      {
        title: "Adaptive model routing",
        items: [
          "5 fixture bugs: same 5/5 success at 45% of baseline cost.",
          "16-task expanded set: 16/16 in both arms at 39% of baseline cost, 3.3× faster wall-clock.",
          "5 real OSS repositories: adaptive 3/5 against always-expensive 2/5, at 24% of the cost.",
        ],
      },
      {
        title: "Validation beyond the fixture set",
        items: [
          "jaraco/path — full end-to-end run on an unfamiliar repository: verifier-gated success in one attempt, 6 calls, $0.053. The first attempt failed honestly and exposed a real harness bug, which was fixed.",
          "python-semver — module-level definition of done, 15/15 checks.",
          "Sandbox adversarially confirmed: 24/24 sequential plus concurrent attack suites held.",
        ],
      },
      {
        title: "Security hardening",
        items: [
          "One real data leak found live — a task-id path traversal in task_status and the status CLI — fixed via a shared semantic guard and pinned by 101 adversarial tests across the MCP and CLI boundaries.",
        ],
      },
      {
        title: "Known limitations, documented rather than hidden",
        items: [
          "Failure classification is deliberately not implemented; the chosen novel mechanism was routing, not repair strategy.",
          "Free-tier endpoints made some multi-repo runs flaky. The harness refused to claim success in every such case.",
          "SWE-bench Lite is deferred. Python-only. stdio-only MCP transport. No web UI beyond the read-only dashboard.",
        ],
      },
    ],
    source: "CHANGELOG.md:8-104",
  },
  {
    version: "Rounds 1–5",
    date: "pre-release",
    tagged: false,
    summary:
      "Build history before the tag: contracts and stubs, each module's first milestone, integration of the real stack, the resume contract, and the routing ablations at two scales.",
    groups: [
      {
        title: "What this covers",
        items: [
          "Initial cross-module contracts in INTERFACES.md, and the stub phase that let four workstreams build in parallel.",
          "Integration of the real stack: real Docker sandbox, real scheduler, real cloud models.",
          "The checkpoint and resume contract, verified with real process kills.",
          "Adaptive-routing ablations at two scales, including the honest negative that drove the v2 predictor redesign.",
        ],
      },
    ],
    source: "CHANGELOG.md:106-115",
  },
];
