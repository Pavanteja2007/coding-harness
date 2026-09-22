---
name: pytest-conventions
description: Use when fixing bugs in Python repos whose tests run under pytest. Covers suite invocation (python -m pytest from the repo root, never bare pytest), test-file protection rules, and node-id targeting.
---

# pytest conventions

- Run the suite as `python -m pytest` from the repo root — the bare
  `pytest` command relies on PATH and can import a different pytest or
  miss the repo's conftest, producing misleading ImportError/collect
  errors that look like the bug.
- Reference one test as a node id:
  `python -m pytest tests/test_mathutil.py::test_mean -x`.
- Fix the CODE, not the tests. A failing test is the specification of
  the bug — never edit, weaken, or defuse a test file to make it pass.
- Prefer the smallest reproducing invocation first (the target node),
  then the full suite to check for regressions.
