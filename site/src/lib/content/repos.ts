/**
 * Multi-repo validation.
 *
 * DELIBERATE OMISSION: bottle, click and parse are NOT listed. README.md:112-115
 * describes them as "in flight" with "honest failures" and no numbers yet.
 * Listing them as validated would be a fabricated claim. Do not add them until
 * the repo records a verified result.
 */
export type RepoResult = {
  name: string;
  kind: "full-dod" | "module-dod" | "ablation-set";
  attempts?: number;
  calls?: number;
  costUsd?: number;
  note?: string;
  source: string;
};

export const REPOS: RepoResult[] = [
  {
    name: "jaraco/path",
    kind: "full-dod",
    attempts: 1,
    calls: 6,
    costUsd: 0.053,
    note:
      "Verifier-gated success in one attempt, with git output, approval gate and memory ingestion. The first attempt failed honestly and exposed a real harness bug — a binary-artifact diff crash — which was then fixed.",
    source: "README.md:102-106; CHANGELOG.md:60-64",
  },
  {
    name: "python-semver",
    kind: "module-dod",
    note:
      "Pristine baseline → broken-state detection → in-sandbox fix → verified, with flake flagging and git output. 15/15 checks.",
    source: "README.md:108-110; CHANGELOG.md:65-66",
  },
  { name: "more-itertools", kind: "ablation-set", source: "README.md:94-95; RESULTS.md:74" },
  { name: "arrow",          kind: "ablation-set", source: "README.md:94-95; RESULTS.md:74" },
  { name: "inflect",        kind: "ablation-set", source: "README.md:94-95; RESULTS.md:74" },
  { name: "boltons",        kind: "ablation-set", source: "README.md:94-95; RESULTS.md:74" },
];

/** The Round-6 headline, stated with its honest failure mode. */
export const MULTIREPO_SUMMARY = {
  text:
    "Five real OSS repos at pinned SHAs, one genuine bug introduced in each and encoded as a failing regression test. The adaptive arm finished 3/5 against always-expensive's 2/5, at 24% of the cost.",
  caveat:
    "Absolute success drops on unfamiliar repos. All three failing agents located their bug but ran out of turns before applying the edit — a model-capability limit, not a machinery one.",
  source: "README.md:93-100; RESULTS.md:117-126",
} as const;
