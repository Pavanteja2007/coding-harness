/**
 * The MCP surface. EXACTLY FIVE TOOLS — do not add a sixth.
 * README.md:137-141; INTERFACES.md:963-975.
 */
export type McpTool = { name: string; summary: string };

export const MCP_TOOLS: McpTool[] = [
  { name: "query_structure",  summary: "tree-sitter code graph — functions, classes, calls, imports" },
  { name: "query_decisions",  summary: "decision and pattern memory across tasks" },
  { name: "record_decision",  summary: "write a decision back into the store" },
  { name: "task_status",      summary: "structured state for one task" },
  { name: "list_repos",       summary: "repos the graph has indexed" },
];

export const MCP_SOURCE = "README.md:137-141; INTERFACES.md:963-975";

/** Vex is a client as well as a server. INTERFACES.md:647-657. */
export const MCP_CLIENT_NOTE = {
  text:
    "Vex is also an MCP client. It consumes any external stdio MCP server, so the memory layer and outside tooling meet on the same protocol.",
  source: "INTERFACES.md:647-657",
} as const;

/**
 * Scope limits stated plainly per MASTER_BUILD_PROMPT §5.6.
 * These are NOT marketing softeners — they are claims the repo does not support.
 */
export const MEMORY_LIMITS = [
  "The code graph is Python-only.",
  "Retrieval is keyword and structural, not semantic or embedding-based.",
  "Context is bounded by file and line counts — there is no tokenizer.",
] as const;

/** Memory-informed planning ablation. INTERFACES.md:262-272. */
export const MEMORY_ABLATION = {
  calls: { off: 35, on: 27 },
  tokens: { off: 147_916, on: 112_390 },
  recurrences: { off: 3, on: 0 },
  note:
    "Both arms solved every task. Memory did not change task-solving power on this set; it changed efficiency, and stopped three repeats of a documented past mistake.",
  caveat: "n=5 × 1 rep — directional only.",
  source: "INTERFACES.md:262-272",
} as const;
