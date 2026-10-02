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
    v: "Not run. Benchmark-grade claims need paid tiers, multiple repetitions and confidence intervals, and that work is deferred. Anyone quoting a SWE-bench score for neo is quoting something that does not exist.",
    source: "CHANGELOG.md:103; RESULTS.md:160-163",
  },
  {
    k: "Structural retrieval is Python-complete, JS/TS-conservative",
    v: "The code graph parses Python fully and scans .js/.jsx/.mjs/.cjs/.ts/.tsx conservatively. Symbol extraction is exact for Python functions, classes, and methods; for JavaScript and TypeScript it is name-based and intentionally over-approximates, and a file in any other language can only be claimed whole-file. This entry previously claimed a single-language code graph, which stopped being true when the JS/TS grammars were added.",
    source: "pyproject.toml (tree-sitter-javascript, tree-sitter-typescript); memory/code_graph.py",
  },
  {
    k: "No semantic retrieval",
    v: "Retrieval is keyword and structural. There are no embeddings and no vector store.",
    source: "README.md:143-145",
  },
  {
    k: "No real tokenizer",
    v: "Context is bounded by a token budget, but the budget is metered with a conservative heuristic (a configurable characters-per-token estimate, default 4) rather than a real tokenizer for the target model. The bound is therefore approximate. This entry used to claim there was no token budget at all, which stopped being true when the context compiler landed.",
    source: "harness/context_compiler.py (estimate_tokens); harness/config.py (context_token_budget)",
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
