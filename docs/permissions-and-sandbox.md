# Permissions and sandbox

Neo has different execution boundaries for different workflows. The boundary is part of the product contract, not an implementation detail.

## Verified fix sandbox

`neo fix` and the verifier-backed runtime path use Docker. Each command runs in a fresh container with:

- a private repository copy mounted read-write at `/workspace`;
- a read-only container root filesystem and bounded `/tmp` tmpfs;
- dropped capabilities and `no-new-privileges`;
- CPU, memory, PID, timeout, and output bounds;
- no network unless the call explicitly opts in;
- a non-root user and fixed-argv Docker invocation.

If Docker is unavailable, Neo fails loudly with `SandboxUnavailableError` and the CLI reports an environment error. It does not silently run the command on the host. The local subprocess sandbox used by `demo/run_demo.py` is an explicitly injected demo fallback, not the production fix path.

The bind mount is intentionally writable so edits persist to the host-side `work/` tree. Docker has no portable per-bind disk quota on this platform; a malicious command can therefore consume host disk up to the task's time and host free-space limits. This is a documented design limitation, not hidden isolation.

## Live interactive workspace

Bare `neo` uses the live repository agent for daily work. Its default compatibility path is not the same as `neo fix`:

- edits are made in the current repository and recorded with pristine/original references for diff and undo;
- local Bash is available to the agent path;
- `agent_approval="auto"` is the compatibility default;
- an explicit target test or test command is needed before completion can be verifier-gated.

Use `neo fix`, or select `/mode build` with its policy, when you need a Docker-isolated verified change.

## Policy actions

The typed policy engine uses three actions:

| Action | Meaning |
|---|---|
| `allow` | The call may proceed if the tool and path policy otherwise permit it |
| `ask` | The call parks for a human decision and a scope-limited approval receipt |
| `deny` | The call is refused and the refusal is recorded |

Deny wins over ask, and ask wins over allow. Protected VCS metadata, secret-like paths, traversal escapes, and configured protected paths remain denied even if a broad allow rule is present.

Approval scopes include:

- once;
- the exact call/effect;
- the current session and path;
- the current session and command prefix;
- a global scope when the policy explicitly permits it.

An approval is bound to the redacted effect and context. It is not a blanket promise that every future command is safe.

## Mode boundaries

The six explicit mode profiles are defined in `cli.commands`:

- **Plan** is read-only.
- **Build** asks before workspace, process, network, and external effects.
- **Explore** is read-only but permits bounded network reference material.
- **Review** denies workspace, process, and external effects.
- **Debug** can run diagnostics but denies workspace writes, network, and external effects.
- **Ask** is read-only.

The profiles are a starting policy, not a substitute for repository-specific rules. Add explicit permission rules through task/session configuration when a repository needs tighter boundaries.

## Dirty repositories and stale edits

The harness snapshots a pristine tree before a verified run and does not treat the user's current index or branch as disposable. The live workspace path records original file content and checks file revisions before a mutation. If the file changed after the model read it, the write is refused as stale; user changes are preserved.

Do not use `/diff undo all` as a substitute for reviewing a dirty repository. It only applies to edits attributed to the current agent run.

## Secrets and network

Provider keys belong in the environment, global/local settings, or an approved secret store, never in a project settings file. The CLI redacts common key forms, but redaction is a last line of defense. Review custom skills, plugins, connector commands, and MCP servers as code with the trust level of the repository they are attached to.

Web fetches are GET-only, bounded, SSRF-checked, and audited. Network access in a mode or connector does not make an endpoint trustworthy.

## Cancellation and cleanup

`/cancel`, Ctrl+C, and a cancellation token stop the active operation at a supported boundary. Docker commands are killed by name, local child processes are bounded and cleaned up where supported, and checkpoints remain available for resume. A hard process kill is different: the next run must validate the durable checkpoint and workspace identity before continuing.
