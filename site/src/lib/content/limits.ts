/**
 * Honest limitations, stated plainly.
 *
 * Each is verified against the repository rather than softened: these are
 * claims the repo does NOT support, and saying so is the same discipline the
 * product applies to its own results.
 */
export const LIMITS = [
  {
    k: "No SWE-bench numbers",
    v: "Not run. Benchmark-grade claims need paid tiers, multiple repetitions and confidence intervals, and that work is deferred. Anyone quoting a SWE-bench score for vex is quoting something that does not exist.",
    source: "CHANGELOG.md:103; RESULTS.md:160-163",
  },
  {
    k: "Python only",
    v: "The code graph is built with tree-sitter's Python grammar. Other languages are not parsed, so structural retrieval does not apply to them.",
    source: "pyproject.toml (tree-sitter-python is the only grammar)",
  },
  {
    k: "No semantic retrieval",
    v: "Retrieval is keyword and structural. There are no embeddings and no vector store.",
    source: "README.md:143-145",
  },
  {
    k: "No tokenizer",
    v: "Context is bounded by file and line counts rather than by token budget, so the bound is approximate.",
    source: "MASTER_BUILD_PROMPT.md §5.6",
  },
  {
    k: "Directional sample sizes",
    v: "The ablations run n=5 and n=16 at one repetition each. The cost ratios are large enough to be robust; success-rate differences at that n are noise.",
    source: "RESULTS.md:156-159",
  },
  {
    k: "Proxy pricing",
    v: "Neither endpoint bills for usage, so costs use published rates for comparable model classes. The delta between arms is a price-model delta, not an invoice.",
    source: "RESULTS.md:151-155",
  },

  {
    k: "One transport",
    v: "The MCP surface is stdio only, and there is no web UI beyond the read-only dashboard.",
    source: "CHANGELOG.md:103-104",
  },
] as const;
