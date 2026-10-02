# Daily-use workflows

Neo has two execution paths. Choosing the right one prevents a common source of confusion: a live interactive edit is not the same thing as a verifier-gated fix.

## `neo fix`: explicit verified path

Use this for a bug with a known target test or a test command:

```bash
neo fix --repo . \
  --issue "The parser drops the final record when the input is empty" \
  --target-test tests/test_parser.py::test_empty_input
```

The run:

1. Snapshots `pristine/` and `work/`.
2. Verifies the target on the pristine tree.
3. Retrieves context and plans independently checkable steps.
4. Executes commands through Docker and validates edits.
5. Runs the target, flake check, and full regression suite.
6. Mints success only when all declared evidence is clean.
7. Writes `state.json`, `trace.jsonl`, `rationale.md`, and, for a verified fix, `git.json` in the task log.

The original checkout is not the agent's edit target. If the target already passes before any edit, the harness reports that state honestly instead of inventing a fix.

## Bare `neo`: live repository agent

Run this from a repository when you want an interactive coding session:

```bash
cd /path/to/repo
neo
```

Plain sentences can explain, inspect, run commands, refactor, or edit the live repository. The compatibility agent defaults to `agent_approval="auto"` and live local Bash. Without a declared target test or test command, a finished agent response is `completed_unverified`, not a verified fix.

Use `/plan <text>` before a work-shaped change when you want to inspect the proposed steps. The plan is guidance, not a completion contract.

## Explicit modes

Select a mode with `/mode`:

| Mode | Intended use | Mutation policy | Verification posture |
|---|---|---|---|
| `plan` | Decompose work without changing files | Read-only | Planning result; no success claim from model text |
| `build` | Implement and verify a feature | Workspace/process/network effects ask for approval | Acceptance evidence when configured |
| `explore` | Investigate code and bounded reference material | Read-only; bounded network allowed | Research answer, not a fix verdict |
| `review` | Inspect a change and report findings | Workspace/process/external effects denied | Read-only evidence |
| `debug` | Run diagnostics and tests | Workspace writes and network/external effects denied | Process/test evidence only |
| `ask` | Answer a repository question | Read-only | Answer only, normally unverified |

Examples:

```text
/mode plan
Break the parser refactor into independently testable steps.

/mode build
Add CSV export and verify the existing suite.

/mode ask
Which callers depend on parse_record?

/mode review
Review the current diff and list risks.
```

Aliases include `question` for `ask`, `research` for `explore`, and `fix`/`agent` for `build`.

## Question and research paths

For a repository question, the agent reads relevant files, structural memory, and decision memory without editing the workspace. For a research request, the agent can use bounded `FETCH`/`DOCS` operations. External results are bounded and audited; a failed fetch is reported rather than silently replaced with an invented answer.

For strict SDK or kernel use, a question result without a verifier is `completed_unverified`.

## Build and project work

The single-session build path authors acceptance tests first, confirms they fail on the pristine tree, then runs the fix loop against those tests. A large project can checkpoint between sub-tasks through the project/build configuration. The exact project and session-budget keys are runtime configuration; use a distinct log root and a stable project ID when resuming.

## Scan to fix

Start with a read-only scan:

```bash
neo scan --repo . --json
```

Review the ranked findings, then hand one to the verified path:

```bash
neo fix --repo . --finding <scan-id>#<n>
```

A scan never edits the repository, runs a shell, or calls a model. Its output is a ranked recommendation, not a permission to mutate.

## Mid-run control

While a run is live:

- Plain text or `/steer` guides the task.
- `replan: ...` replaces the current plan at a safe boundary while retaining work.
- `abort: ...` stops at a clean resumable boundary.
- `/cancel` requests cancellation and preserves checkpoints.
- `/status`, `/diff`, `/trace`, `/feed`, and `/cost` remain useful read-only views.

A steered run cannot mint success until the final gate has re-verified the incorporated work.
