/**
 * Release history.
 *
 * Transcribed from CHANGELOG.md rather than invented. The repository currently
 * has exactly one tagged release; the rounds below it are the build history
 * that CHANGELOG.md records as pre-history, and they are labelled as such
 * rather than dressed up as releases.
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
