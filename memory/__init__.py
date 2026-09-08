"""Terminal 4 — persistent memory layer: structural code graph + decision store.

Structural memory: tree-sitter based code knowledge graph (memory/code_graph.py).
Decision memory:    SQLite-backed store of learned facts (memory/decision_store.py).

Both are exposed to any MCP client via mcp_server/ (INTERFACES.md Boundary 5).
"""
