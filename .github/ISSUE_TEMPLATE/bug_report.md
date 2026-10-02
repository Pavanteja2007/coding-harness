---
name: Bug report
about: Something in Neo behaved incorrectly, crashed, or reported a wrong result
title: "[bug] "
labels: ["bug"]
assignees: []
---

<!--
Please fill in every section. The three that matter most are Version, To
reproduce, and Logs and trace: without the version and the exact command plus
the trace, a report usually cannot be reproduced at all.

Before filing, please check docs/known-issues.md — open issues there already
carry a reproducer, and saying "this looks like SG-01" saves a round trip.
-->

**Version**

```
$ neo --version
```

<!--
State the version AND whether it came from PyPI or a source checkout. These
behave differently: the public 0.2.0 wheel does not contain agent_sdk, acp,
integrations, recipes, or extensions, and a 0.3.0 source checkout is not
published at all. "0.2.1" alone does not tell anyone which artifact you have.

If the line reads like "neo 0.2.1 (docs describe 0.3.0)", your installed
distribution is older than the docs you read. Say so — that mismatch is itself
often the bug.
-->

**What happened**

A clear description of what Neo did, and how that differs from what you
expected. If a run finished, paste the final status line and the one-line
reason from `logs/<task_id>/state.json`.

**Especially important if the status was wrong.** If Neo reported a run as
`success` (or "done", or "verified") and it should not have, say so at the top
and include the trace. A false verified claim is the most serious class of bug
in this project, and it is the one we most need reports about. Likewise: a run
that reported `success` where the target test was never actually collected or
never actually ran.

**To reproduce**

Steps, or better: the exact command you ran, plus the minimal `--issue` text
and the repository state that triggers it.

```
# exact command(s), including every flag
```

```
# minimal issue text
```

State the starting repository state too — a fresh clone, an existing project, a
dirty worktree — because the verifier composes its fix in private `pristine/`
and `work/` copies and the starting state changes what it sees.

**Environment**

- Neo version (`neo --version`):
- Installed from PyPI or a source checkout?:
- OS:
- Python version:
- Docker available? (`docker version`): yes / no
- Model provider and model (or "offline demo / scripted"):
- `neo doctor` output (attach `neo doctor --json` if you can):
- Which surface: TUI / REPL / headless / `--json` / `neo fix` / `neo serve` / `neo acp` / SDK

**Logs and trace**

Every run writes `logs/<task_id>/` (`state.json`, `trace.jsonl`, and module
artifacts). Attach the directory as a zip if you can — it is the fastest path
to a diagnosis.

```
# trace.jsonl tail, or state.json excerpt
```

You can summarise a run rather than pasting it raw:

```bash
neo status --task-id <task-id>
python -m shared.traceview <task-id> --logs-root logs --summary
```

**Redact before attaching.** Trace events redact common credential forms, but
`settings.toml`, `rationale.md`, and any model output you paste by hand may
not be. Check for keys, tokens, and private URLs. `neo support-bundle` gathers
version, environment, redacted config shape, and recent errors into one
archive with no secrets in it.

**Additional context**

What you already tried, whether it reproduces on a fresh clone, whether it is a
regression from a previous version, and whether it happens with a scripted
model or only with a live provider. That last distinction is often the whole
bug.
