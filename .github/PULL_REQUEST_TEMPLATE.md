<!-- Thank you! Read CONTRIBUTING.md first if you haven't — module
     boundaries and the verifier-gate rule are load-bearing. -->

## What & why

<!-- One paragraph: what this changes, and the problem or use case it
     serves. Link the issue if one exists ("Closes #N"). -->

## What module owns this change

<!-- harness / execution / runtime / memory / mcp_server / cli /
     dashboard / shared / docs+site / repo-meta -->

## How it was verified

<!-- What you ran. For bug fixes: the regression test that would have
     caught the original bug (repo convention). For harness/execution
     changes touching real-model or Docker paths: say whether you ran
     a real e2e / Docker-gated test, not just offline ones. -->

- [ ] `make test` (or the module's selection) passes
- [ ] New/changed behavior has a regression test
- [ ] Docker-gated tests (if any) run or explicitly self-skip

## The two load-bearing gates

- [ ] This does NOT weaken the verifier gate — nothing here can mint
      `status="success"` outside `verify()` confirming target-test pass
      + no regressions.
- [ ] The original repo is never mutated — agent work stays inside
      `logs/{task_id}/work/`.

## Cross-module contracts

<!-- Delete this section if the change is entirely inside one module
     and touches no INTERFACES.md signatures. -->

- [ ] No cross-module signature changed, OR `INTERFACES.md` has a
      Change Log entry for it (added BEFORE coding against it).
- [ ] Module's `AGENTS.md` updated if this changes documented behavior.

## Hygiene

- [ ] `make lint` ratchet clean (no new violations; baselined files
      gained nothing)
- [ ] No secrets, keys, or personal data in the diff
- [ ] Commit message follows `[module] short description`
