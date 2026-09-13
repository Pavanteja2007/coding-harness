---
name: Bug report
about: Something in Vex behaved incorrectly, crashed, or reported a wrong result
title: "[bug] "
labels: ["bug"]
assignees: []
---

**What happened**

A clear description of what Vex did, and how that differs from what you
expected. If the run completed, paste the final status line and (if it
exists) the one-line reason from `logs/<task_id>/state.json`.

**To reproduce**

Steps, or better: the exact `vex ...` / `python -m cli ...` command you
ran, plus the minimal `--issue` text and repo state that triggers it.

```
# command(s) here
```

**Environment**

- Vex version (`vex --version`):
- OS:
- Python version:
- Docker available? (relevant for sandbox/verify paths): yes / no
- Model provider (or "offline demo / scripted"):
- Which module is involved, if you can tell: harness / execution / runtime / memory+MCP / cli / dashboard

**Logs and trace**

Every run writes `logs/<task_id>/` (`state.json`, `trace.jsonl`, and
module artifacts). Paste the relevant `trace.jsonl` tail (it may contain
the failing step) or attach the directory as a zip. Redact API keys —
trace events redact them, but double-check anything you paste.

```
# trace tail / state.json excerpt here
```

**Additional context**

Anything else: what you already tried, whether it reproduces on a
fresh clone, whether it's a regression from a previous version.
