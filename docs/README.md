# Neo product guide

This directory is the current, source-backed user guide for Neo. It is written for the 0.3.0 source candidate. The public PyPI release can lag the checkout, so commands that depend on unreleased surfaces are marked as source-only.

## Start here

- [Onboarding](onboarding.md) — the guided path from install to a first verified fix, with a checkable success condition at every step
- [Quickstart](quickstart.md) — the same ground, compressed
- [Providers and routing](providers.md) — settings, secrets, profiles, and adaptive routing
- [Command reference](commands.md) — CLI subcommands and TUI/REPL slash commands
- [Daily-use workflows](workflows.md) — fix, plan, build, ask, explore, review, and debug
- [Permissions and sandbox](permissions-and-sandbox.md) — approval policy and Docker boundaries
- [Extensions](extensions.md) — skills, plugins, and MCP connectors
- [Sessions and recovery](sessions-and-recovery.md) — checkpoints, resume, cancellation, and recovery
- [Headless, CI, and SDK](headless-and-sdk.md) — machine-readable operation and library use
- [Troubleshooting](troubleshooting.md) — failure diagnosis and safe recovery
- [Architecture and event schema](architecture-and-events.md) — the four layers, trace rows, and replay
- [Feature matrix](feature-matrix.md) — implemented, source-only, blocked, and future work
- [Accessibility](accessibility.md) — measured terminal accessibility, verified on a real attached PTY
- [Demo guide](../demo/README.md) — reproducible offline and agent walkthroughs

## Evidence and release

Read these before trusting any number this project publishes.

- **[Release verdict](release-verdict.md) — START HERE.** A parallel gate ran
  both agent engines against the same 10 real bugs: the `daily` path that 0.3.0
  makes the default scores **0/10**, the legacy path it replaces scores
  **10/10**. Verdict: **BLOCKED, do not cut over yet.**
- [Benchmark](benchmark.md) — every measured result, its methodology, and its
  caveats. **Includes the failures.** No SWE-bench result is claimed.
- [Release evidence](release-evidence.md) — which verification lanes ran for
  0.3.0, which were blocked, and which were not run at all
- [Release runbook](release-runbook.md) — the owner's procedure for cutting a
  release. Publishing is owner-only.
- [Known issues](known-issues.md) — the bug corpus: open blockers with
  reproducers, and the closed ones kept for the record
- [Dogfood evidence](dogfood-report.md) — measured runs and the limits of the evidence

## Evidence labels

- **Real product run**: the actual CLI, harness, scheduler, Docker, or provider path was executed.
- **Deterministic fixture**: a scripted model or fixture verifies a contract, receipt, or safety path. It is useful evidence for machinery, not model quality.
- **Source-only**: implemented in this checkout but not guaranteed in the published wheel.
- **Blocked**: an environment or missing integration prevented the lane. A blocked lane is never counted as a pass.
- **Not run**: the lane was not attempted. Distinguished from *blocked*, because "we did not run it" and "we ran it and it could not complete" are different problems.

The product guide intentionally separates those labels. Historical numbers are kept only where the linked artifact and its caveats are explicit.
