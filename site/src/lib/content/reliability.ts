/**
 * Runtime reliability: the stress run and the soak run.
 *
 * Verified against README.md:124-127 (stress) and RESULTS.md:139-148 (soak)
 * before being written down. These were previously hardcoded in
 * Reliability.tsx, where nothing traced them - check-sources caught it.
 */

export const STRESS = {
  tasks: 45,
  concurrency: 45,
  kills: 8,
  resumes: 8,
  leakedContainers: 0,
  source: "README.md:124-127",
};

export const SOAK_FIGURES = [
  { k: "tasks", v: "3,600" },
  { k: "mid-run kills", v: "120" },
  { k: "checks passed", v: "15/15" },
  { k: "latency p95 drift", v: "1.02x" },
  { k: "artifacts per task", v: "7.06 -> 7.07" },
  { k: "leaked workers", v: "0" },
];

export const SOAK_SOURCE = "RESULTS.md:139-148";

export const SOAK_HOURS = "5.15 simulated task-hours";

/** The soak found a real bug. Kept on the page, not buried. */
export const SOAK_BUG = {
  text:
    "The soak also found a real bug: five of 3,600 workers died on a Windows PermissionError when a supervisor read a file a worker was atomically replacing. It was fixed with a bounded replace-retry, and the re-run showed zero occurrences.",
  source: "RESULTS.md:143-148",
};

/** Adversarial hardening. CHANGELOG.md:73-81; INTERFACES.md:472-507. */
export const ADVERSARIAL = {
  testCount: 101,
  leak: "a task-id path traversal in task_status and `vex status --task-id`",
  text:
    "Adversarial passes over the MCP server, the CLI, and the sandbox found one real data leak, which was fixed and pinned by 101 adversarial tests.",
  source: "CHANGELOG.md:73-81; INTERFACES.md:472-507",
};
