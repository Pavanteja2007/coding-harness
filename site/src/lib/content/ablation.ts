/**
 * Adaptive model routing — the ablation.
 *
 * EVERY figure here was read directly out of the repo, not from memory.
 * Primary: README.md:58-63. Cross-checked against RESULTS.md:80-84.
 * If you change a number, re-verify BOTH and update `source`.
 */

export type Arm = "always-expensive" | "adaptive";

export type AblationRow = {
  set: string;
  n: number;
  arm: Arm;
  success: string;
  calls: number;
  tokens: number;
  costUsd: number;
  wallS: number;
};

export const ABLATION_SOURCE =
  "README.md:58-63; cross-checked RESULTS.md:80-84";

export const ABLATION_ROWS: AblationRow[] = [
  { set: "5 fixture bugs",    n: 5,  arm: "always-expensive", success: "5/5",   calls: 17, tokens: 38_680,  costUsd: 0.0528, wallS: 575  },
  { set: "5 fixture bugs",    n: 5,  arm: "adaptive",         success: "5/5",   calls: 31, tokens: 69_615,  costUsd: 0.0237, wallS: 300  },
  { set: "16-task set",       n: 16, arm: "always-expensive", success: "16/16", calls: 81, tokens: 138_526, costUsd: 0.1505, wallS: 2717 },
  { set: "16-task set",       n: 16, arm: "adaptive",         success: "16/16", calls: 71, tokens: 136_436, costUsd: 0.0581, wallS: 812  },
  { set: "5 real OSS repos",  n: 5,  arm: "always-expensive", success: "2/5",   calls: 71, tokens: 329_438, costUsd: 0.3059, wallS: 2992 },
  { set: "5 real OSS repos",  n: 5,  arm: "adaptive",         success: "3/5",   calls: 75, tokens: 302_801, costUsd: 0.0730, wallS: 581  },
];

/**
 * VERBATIM per MASTER_BUILD_PROMPT §5.4.
 * Ships WITH the numbers as a designed component, never as fine print.
 * Do not paraphrase, shorten, or relocate to a footer.
 */
export const HONESTY_NOTE =
  "Costs use proxy price rates for comparable model classes on free-tier BYO " +
  "endpoints (both report $0) — the delta is a price-model delta, not a bill. " +
  "n=5 and n=16 runs are directional, not benchmark-grade. SWE-bench numbers " +
  "are deferred to Phase 6.";

/** How the mechanism behaved. RESULTS.md:100-101, 117-120. */
export const ROUTING_FACTS = [
  {
    text: "96–97% of ON-arm calls ran on the cheap tier, and success never dropped because of routing.",
    source: "RESULTS.md:100-101",
  },
  {
    text: "The cheap tier's speed (p50 22s vs the expensive tier's p95 309s) meant more fix attempts fit the same wall-clock budget.",
    source: "RESULTS.md:117-120",
  },
  {
    text: "Escalation never sticks: after any expensive call, routing drops back to cheap unless struggle persists.",
    source: "RESULTS.md:112-114",
  },
] as const;

/**
 * The honest negative. v1 is kept, not hidden — this is the most on-brand
 * content in the repo and §8 of the brief treats omitting it as a failure.
 */
export const HONEST_NEGATIVE = {
  title: "The first version made things worse.",
  body:
    "The v1 predictor scored raw message text. Harness prompts are large by " +
    "construction — system templates plus injected file context — so every call " +
    "saturated at “hard” and the adaptive arm degenerated to always-expensive: " +
    "17 of 17 calls on the expensive tier, costing about twice the baseline. " +
    "That result drove the v2 redesign: score only the issue portion of the first " +
    "user message, add a struggle-based escalation signal, and add router-side " +
    "429 backoff.",
  offCost: 0.0241,
  onCost: 0.0467,
  source: "RESULTS.md:80, 86-97",
} as const;
