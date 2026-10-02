# Extensions: skills, plugins, and connectors

Neo supports three related extension surfaces. Keep their trust and lifecycle separate.

## Skills

A skill is a directory containing `SKILL.md` with frontmatter and instructions:

```markdown
---
name: pytest-conventions
description: Use when changing Python tests, fixtures, or pytest configuration.
---

Run the smallest relevant test first, then the configured suite. Keep test
changes behavior-preserving unless the request explicitly changes the contract.
```

Discovery order:

1. `<repo>/.neo/skills/<name>/SKILL.md`
2. global `~/.config/neo/skills/<name>/SKILL.md`
3. an installed plugin's `skills/` directory
4. roots named by `skills_roots`

A matching skill is scanned conservatively from the issue, retrieved terms, and repository path. The bounded body is injected into planning or the daily agent with a trace receipt. A malformed skill is skipped with a diagnostic; it cannot crash the task.

Manage standalone skills:

```bash
neo skills install ./skills/pytest-conventions
neo skills list
neo skills show pytest-conventions
neo skills disable pytest-conventions
neo skills enable pytest-conventions
neo skills remove pytest-conventions
```

Do not put credentials or executable payloads in a skill. A skill is instruction text, not a trusted plugin.

## Plugins

A plugin is a directory copied into the Neo-owned plugin directory. It can contain skills, commands, safe read-only tool verbs, and MCP server references.

Minimal implicit layout:

```text
my-plugin/
  skills/
    conventions/SKILL.md
  commands/
    review.md
```

Explicit manifest:

```json
{
  "name": "webapp-toolkit",
  "description": "Review and lint helpers",
  "version": "1.0.0",
  "skills": ["skills/django-style"],
  "commands": ["commands/review.md"],
  "tools": {"verbs": ["ruff", "ruff check"]},
  "mcp_servers": {"structure-memory": "python -m mcp_server"}
}
```

Install and manage it:

```bash
neo plugin install ./my-plugin
neo plugin list
neo plugin show webapp-toolkit
neo plugin disable webapp-toolkit
neo plugin enable webapp-toolkit
neo plugin remove webapp-toolkit
```

Disabling keeps the directory and creates a marker; discovery skips its skills, commands, tool verbs, and MCP references. There is no marketplace or registry.

Tool verbs extend only the read-only batch/diagnostic allowlist. Destructive first tokens, shell composition, redirects, and arbitrary interpreter verbs remain rejected. A manifest cannot widen the policy into arbitrary execution.

## MCP connectors

Connectors are launch commands for external stdio MCP servers. The registry is merged in this order, with later layers winning:

1. plugin manifest references;
2. global `settings.toml` `[mcp_servers]`;
3. `<repo>/.neo/connectors.toml` (committable, no secrets);
4. `<repo>/.neo/connectors.local.toml` (personal, ignored).

Project file example:

```toml
[mcp_servers]
linter = "python -m my_linter_mcp"
docs = "npx -y docs-mcp-server --root ."
```

Register a global label:

```bash
neo mcp add structure-memory -- python -m mcp_server
neo mcp list
neo mcp health
neo mcp list-tools structure-memory
neo mcp call structure-memory query_decisions --args '{"query":"pytest"}'
```

Connector commands are fixed argv, not shell strings. Secrets in launch commands are masked in output. Health checks are bounded and return structured failures rather than tracebacks. A connector that hangs is a failed connector, not evidence that the model understood the repository.

## Authoring checklist

Before enabling an extension:

1. Keep project configuration free of credentials.
2. Give skills precise descriptions so matching stays conservative.
3. Treat plugin manifests and MCP commands as executable code.
4. Prefer read-only tool verbs.
5. Test `list`, `health`, and failure output with a disposable environment.
6. Verify the extension through a real task and inspect its trace receipt.
7. Disable or remove it when it is no longer needed.

The shipped fixture `tests/fixtures/plugin-webapp-toolkit/` is a copyable starting point. The test fixture is not a marketplace or a production install.
